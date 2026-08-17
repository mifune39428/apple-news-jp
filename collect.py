#!/usr/bin/env python3
"""Apple関連ニュースをRSSで集め、日本語の見出しと要約を付けて docs/articles.json に書き出す。

海外メディアの本文はそのまま載せない。日本語の見出し・3行程度の独自要約・
出典名・原文へのリンクだけを持たせる（引用の範囲に収めるため）。

GitHub Actions から6時間ごとに実行され、差分がコミットされると
GitHub Pages 側のサイトが更新される。ローカルでも同じスクリプトが動く。
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from difflib import SequenceMatcher

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import llm_providers  # noqa: E402  （.env 読み込みより前でよい。キーは呼び出し時に参照される）

FEEDS_PATH = os.path.join(BASE_DIR, "feeds.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "articles.json")
DETAIL_DIR = os.path.join(BASE_DIR, "docs", "articles")

USER_AGENT = "apple-news-jp/1.0 (+https://github.com/mifune39428)"
FETCH_TIMEOUT = 25

# 新しく取り込む記事の対象期間。これより古い記事は拾わない。
INTAKE_DAYS = 3
# サイトに残す期間と件数の上限。
KEEP_DAYS = 30
KEEP_MAX = 400
# 1回のLLM呼び出しでまとめて処理する記事数。
# Groqの無料枠は分あたりトークン数（TPM 6000）が厳しいので、大きくし過ぎない。
BATCH_SIZE = 5
# 1回の実行で日本語化する上限。無料枠の1日あたり回数を使い切らないための蓋。
# 溢れた分は次の実行（6時間後）に回る。
MAX_NEW_PER_RUN = 40
# 詳細ページ（解説本文）を作る上限。1記事につきLLMを1回使うので新着より少なめ。
# 新着で余った枠は、まだ詳細のない古い記事の埋め合わせに回す。
MAX_DETAILS_PER_RUN = 25
# 詳細ページの生成を並行させる本数。無料枠の分あたり回数制限に当たらない程度に。
DETAIL_WORKERS = 3

CATEGORIES = [
    "iPhone",
    "Mac",
    "iPad",
    "Apple Watch・ウェアラブル",
    "ソフトウェア・OS",
    "サービス・アプリ",
    "AI",
    "ビジネス・業績",
    "噂・リーク",
    "その他",
]

JST = dt.timezone(dt.timedelta(hours=9))

ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
RSS10 = "{http://purl.org/rss/1.0/}"


# --------------------------------------------------------------------------
# 下ごしらえ
# --------------------------------------------------------------------------

def load_env() -> None:
    """.env があれば読む（GitHub Actions では Secrets が環境変数で入るので不要）。"""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text or "")
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_url(url: str) -> str:
    """トラッキング用のクエリを落として、同じ記事が別URLに見えないようにする。"""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(parts.query)
        if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref", "at_"))
    ]
    # Google ニュース経由のリンクだけはクエリに記事IDが載るので触らない。
    if "news.google.com" in parts.netloc:
        query = urllib.parse.parse_qsl(parts.query)
    cleaned = parts._replace(query=urllib.parse.urlencode(query), fragment="")
    return urllib.parse.urlunsplit(cleaned).rstrip("/")


def article_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode("utf-8")).hexdigest()[:16]


def parse_date(raw: str) -> dt.datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(raw)
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# RSS / Atom / RDF の取得
# --------------------------------------------------------------------------

def _text(node, *paths: str) -> str:
    for path in paths:
        found = node.find(path)
        if found is not None:
            if found.text:
                return found.text
            # Atom の <link href="..."> のように属性側に入っている場合。
            href = found.get("href")
            if href:
                return href
    return ""


# 記事のサムネイルとして使わない画像（配信計測用の透明画像やアイコンなど）。
IMAGE_BLOCKLIST = ("feedburner", "gravatar", "/pixel", "1x1", "blank.gif", "spacer", "doubleclick")
IMG_TAG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.I)
OG_IMAGE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\']'
    r'[^>]+content=["\']([^"\']+)["\']|'
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+'
    r'(?:property|name)=["\'](?:og:image(?::url)?|twitter:image(?::src)?)["\']',
    re.I,
)


def usable_image(url: str, base: str) -> str:
    url = html.unescape((url or "").strip())
    if not url:
        return ""
    url = urllib.parse.urljoin(base, url)
    if not url.startswith(("http://", "https://")):
        return ""
    if any(word in url.lower() for word in IMAGE_BLOCKLIST):
        return ""
    return url


def image_from_entry(entry, base: str) -> str:
    """RSSの中に入っている画像を探す。媒体ごとに置き場所が違うので順に当たる。"""
    for node in entry.findall(f"{MEDIA}thumbnail") + entry.findall(f"{MEDIA}content"):
        medium = (node.get("medium") or node.get("type") or "").lower()
        if medium and "image" not in medium:
            continue
        found = usable_image(node.get("url", ""), base)
        if found:
            return found

    for node in entry.findall("enclosure") + entry.findall(f"{ATOM}link"):
        if "image" in (node.get("type") or "").lower():
            found = usable_image(node.get("url") or node.get("href") or "", base)
            if found:
                return found

    # 本文HTMLの最初の <img>。多くの媒体はここにアイキャッチが入っている。
    raw_body = " ".join(
        node.text or ""
        for tag in ("description", f"{CONTENT}encoded", f"{RSS10}description",
                    f"{ATOM}summary", f"{ATOM}content")
        for node in entry.findall(tag)
    )
    for candidate in IMG_TAG_RE.findall(raw_body):
        found = usable_image(candidate, base)
        if found:
            return found
    return ""


def fetch_og_image(url: str) -> str:
    """RSSに画像が無い記事は、元ページの og:image を見に行く。"""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=12) as response:
            head = response.read(200_000).decode("utf-8", errors="ignore")
            final_url = response.geturl()
    except Exception:  # noqa: BLE001  取れなくても記事自体は載せる
        return ""
    match = OG_IMAGE_RE.search(head)
    if not match:
        return ""
    return usable_image(match.group(1) or match.group(2) or "", final_url)


def fill_missing_images(items: list[dict]) -> None:
    targets = [item for item in items if not item["image"]]
    if not targets:
        return
    print(f"  サムネイル未取得 {len(targets)}件をページから取得中 …")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for item, image in zip(targets, pool.map(lambda i: fetch_og_image(i["url"]), targets)):
            item["image"] = image
    print(f"  取得できたもの {sum(1 for item in targets if item['image'])}件")


def fetch_feed(feed: dict) -> list[dict]:
    request = urllib.request.Request(feed["url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        raw = response.read()

    root = ET.fromstring(raw)
    entries = (
        root.findall(".//item")
        or root.findall(f".//{RSS10}item")
        or root.findall(f".//{ATOM}entry")
    )

    items = []
    for entry in entries:
        title = strip_html(_text(entry, "title", f"{ATOM}title", f"{RSS10}title"))
        link = _text(entry, "link", f"{RSS10}link", f"{ATOM}link").strip()
        if not link:
            # Atom は複数の <link> を持つので rel="alternate" を拾う。
            for candidate in entry.findall(f"{ATOM}link"):
                if candidate.get("rel", "alternate") == "alternate":
                    link = (candidate.get("href") or "").strip()
                    break
        if not title or not link:
            continue

        published = parse_date(
            _text(
                entry,
                "pubDate",
                f"{DC}date",
                f"{ATOM}published",
                f"{ATOM}updated",
                "date",
            )
        )
        body = strip_html(
            _text(
                entry,
                "description",
                f"{CONTENT}encoded",
                f"{RSS10}description",
                f"{ATOM}summary",
                f"{ATOM}content",
            )
        )

        items.append(
            {
                "id": article_id(link),
                "url": canonical_url(link),
                "title_original": title,
                "excerpt": body[:800],
                "source": feed["name"],
                "lang": feed.get("lang", "en"),
                "region": feed.get("region", "global"),
                "image": image_from_entry(entry, link),
                "official": bool(feed.get("official")),
                "mirror_group": feed.get("mirror_group", ""),
                "published": (published or dt.datetime.now(dt.timezone.utc)).isoformat(),
            }
        )
    return items


def collect_feed_items(feeds: list[dict]) -> list[dict]:
    collected: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_feed, feed): feed for feed in feeds}
        for future in concurrent.futures.as_completed(futures):
            feed = futures[future]
            try:
                items = future.result()
            except Exception as exc:  # 1本落ちても全体は続ける
                print(f"  × {feed['name']}: {type(exc).__name__}: {exc}")
                continue
            print(f"  ○ {feed['name']}: {len(items)}件")
            collected.extend(items)
    return collected


# --------------------------------------------------------------------------
# 重複の除去
# --------------------------------------------------------------------------

def normalize_title(title: str) -> str:
    title = re.sub(r"[\s　]+", "", title.lower())
    return re.sub(r"[!-/:-@\[-`{-~、。「」・…—–\-]", "", title)


TOKEN_RE = re.compile(r"[A-Za-z]{2,}|[0-9]+|[ァ-ヶー]{2,}|[一-龥]{2,}")


def title_tokens(title: str) -> set[str]:
    return {t.lower() for t in TOKEN_RE.findall(title)}


def same_story(a: dict, b: dict) -> bool:
    """日本語化したあとの見出しで、同じ出来事かどうかを見る。

    海外版と日本版（例: Apple Newsroom の英語版と日本語版）は原題が別物なので
    URLや原題では重ならない。日本語の見出しになって初めて比較できる。
    """
    published_a, published_b = parse_date(a["published"]), parse_date(b["published"])
    if published_a and published_b and abs((published_a - published_b).total_seconds()) > 36 * 3600:
        return False

    # Apple Newsroom の英語版と日本語版のように、同じ発表を各国語で出す媒体同士は
    # 必ず1対1で対応するので、判定をゆるめてよい。
    mirrored = bool(a.get("mirror_group")) and a.get("mirror_group") == b.get("mirror_group")
    ratio_limit, token_limit = (0.5, 0.4) if mirrored else (0.75, 0.7)

    left, right = normalize_title(a["title_ja"]), normalize_title(b["title_ja"])
    if SequenceMatcher(None, left, right).ratio() >= ratio_limit:
        return True

    tokens_a, tokens_b = title_tokens(a["title_ja"]), title_tokens(b["title_ja"])
    if len(tokens_a) >= 3 and len(tokens_b) >= 3:
        overlap = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
        if overlap >= token_limit:
            return True
    return False


def dedupe_stories(
    new_items: list[dict], existing_items: list[dict]
) -> tuple[list[dict], set[str]]:
    """同じ出来事を伝える記事は1本に絞る。日本語媒体・Apple公式を優先して残す。

    掲載する新着と、入れ替えで取り下げる既存記事のIDを返す。
    """
    recent = existing_items[:120]
    # 日本語の記事とApple公式を先に見て、後から来た重複を落とす。
    ordered = sorted(
        new_items,
        key=lambda item: (0 if item["official"] else 1, 0 if item["lang"] == "ja" else 1),
    )
    kept: list[dict] = []
    replaced: set[str] = set()
    for item in ordered:
        older = next(
            (o for o in recent if o["id"] not in replaced and same_story(item, o)), None
        )
        if older is not None:
            # 英語版を先に載せたあとに日本語版が届いた場合だけ、日本語版に差し替える。
            if item["lang"] == "ja" and older.get("lang") != "ja":
                print(f"  ・日本語版に差し替え: {item['title_ja']}（{item['source']}）")
                replaced.add(older["id"])
                kept.append(item)
            else:
                print(f"  ・既出のため除外: {item['title_ja']}（{item['source']}）")
            continue
        if any(same_story(item, other) for other in kept):
            print(f"  ・重複のため除外: {item['title_ja']}（{item['source']}）")
            continue
        kept.append(item)
    return kept, replaced


def is_duplicate(title: str, known_titles: list[str]) -> bool:
    target = normalize_title(title)
    if not target:
        return False
    for known in known_titles:
        if not known:
            continue
        if target == known:
            return True
        if abs(len(target) - len(known)) <= max(6, len(target) * 0.3):
            if SequenceMatcher(None, target, known).ratio() >= 0.86:
                return True
    return False


# --------------------------------------------------------------------------
# 日本語化（見出しの翻訳と独自要約）
# --------------------------------------------------------------------------

PROMPT_TEMPLATE = """あなたはApple専門のニュースメディアの日本語編集者です。
海外・国内のApple関連記事の見出しと抜粋を渡すので、日本の読者向けに
「日本語の見出し」と「短い要約」を作ってください。

厳守すること:
- 原文の本文をそのまま翻訳して並べない。事実を踏まえて自分の言葉で短くまとめる。
- 事実を足さない。抜粋に書かれていないスペックや日付を創作しない。
- 見出し(title_ja)は日本語で40文字以内。煽らず、内容が分かる形にする。
- 要約(summary_ja)は日本語で100〜160文字。2〜3文。「〜とのこと」で締めない。
- category は次から必ず1つ選ぶ: {categories}
- importance は1〜5の整数。5=Appleの公式発表や新製品発表級、3=普通のニュース、1=雑談・小ネタ。
- Appleおよびその製品・サービス・業績とほぼ無関係な記事は apple を false にする。
- 出力はJSON配列のみ。前置き・説明・コードフェンスを付けない。

出力形式（要素数は入力と同じ{count}件、iは入力の番号）:
[{{"i":1,"apple":true,"title_ja":"...","summary_ja":"...","category":"iPhone","importance":3}}]

入力記事:
{articles}
"""


def build_prompt(batch: list[dict]) -> str:
    lines = []
    for index, item in enumerate(batch, start=1):
        lines.append(
            f"[{index}] 出典: {item['source']} / 言語: {item['lang']}\n"
            f"見出し: {item['title_original']}\n"
            f"抜粋: {item['excerpt'][:600] or '(抜粋なし)'}\n"
        )
    return PROMPT_TEMPLATE.format(
        categories=" / ".join(CATEGORIES),
        count=len(batch),
        articles="\n".join(lines),
    )


def parse_llm_json(text: str, expected: int) -> list[dict]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end == -1:
        raise llm_providers.ResponseInvalid("JSON配列が見つかりません")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise llm_providers.ResponseInvalid(f"JSONとして読めません: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise llm_providers.ResponseInvalid("空の配列です")
    if len(data) != expected:
        raise llm_providers.ResponseInvalid(f"{expected}件のはずが{len(data)}件です")
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("title_ja") or not entry.get("summary_ja"):
            raise llm_providers.ResponseInvalid("title_ja / summary_ja が欠けています")
    return data


def enrich(items: list[dict]) -> list[dict]:
    """LLMで日本語見出しと要約を付ける。失敗した分は原文のまま残して次回に回す。"""
    results: list[dict] = []
    for offset in range(0, len(items), BATCH_SIZE):
        batch = items[offset : offset + BATCH_SIZE]
        print(f"  日本語化 {offset + 1}〜{offset + len(batch)}件目 …")
        try:
            text = llm_providers.generate_text(
                build_prompt(batch),
                validate=lambda t, n=len(batch): parse_llm_json(t, n),
            )
            entries = parse_llm_json(text, len(batch))
        except llm_providers.LLMError as exc:
            # 生煮えの記事をサイトに出すより、今回は見送って次の実行で拾い直す。
            # RSSには数日分残っているので、枠が空けば自然に再挑戦される。
            print(f"  × 日本語化に失敗（この{len(batch)}件は次回に回します）: {exc}")
            continue

        by_index = {}
        for entry in entries:
            try:
                by_index[int(entry.get("i", 0))] = entry
            except (TypeError, ValueError):
                continue

        for index, item in enumerate(batch, start=1):
            entry = by_index.get(index) or entries[index - 1]
            if entry.get("apple") is False:
                continue
            category = str(entry.get("category", "")).strip()
            item["title_ja"] = str(entry["title_ja"]).strip()
            item["summary_ja"] = str(entry["summary_ja"]).strip()
            item["category"] = category if category in CATEGORIES else "その他"
            try:
                item["importance"] = max(1, min(5, int(entry.get("importance", 3))))
            except (TypeError, ValueError):
                item["importance"] = 3
            results.append(item)
    return results


def to_public(item: dict) -> dict:
    """サイトに出す形に整える。原文の抜粋は公開データに残さない。"""
    return {
        key: value
        for key, value in item.items()
        if key not in ("excerpt", "translated")
    }


# --------------------------------------------------------------------------
# 詳細ページ（原文を読まなくても分かる日本語の解説）
# --------------------------------------------------------------------------

BLOCK_TAGS_RE = re.compile(
    r"(?is)<(script|style|noscript|svg|iframe|form|nav|aside|header|footer|figcaption)[^>]*>.*?</\1>"
)
ARTICLE_BLOCK_RE = re.compile(r"(?is)<article[^>]*>(.*?)</article>")
PARAGRAPH_RE = re.compile(r"(?is)<p[^>]*>(.*?)</p>")


def fetch_article_text(url: str) -> str:
    """記事ページから本文の段落だけを取り出す。解説を書くための材料にする。"""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(600_000).decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""

    body = BLOCK_TAGS_RE.sub(" ", raw)
    # <article> があれば本文はその中。無ければページ全体から拾う。
    block = ARTICLE_BLOCK_RE.search(body)
    scope = block.group(1) if block else body

    paragraphs = []
    for chunk in PARAGRAPH_RE.findall(scope):
        text = strip_html(chunk)
        # ナビゲーションや広告の短い断片を落とす。
        if len(text) >= 40:
            paragraphs.append(text)
    return "\n".join(paragraphs)[:6000]


DETAIL_PROMPT = """あなたはApple専門メディアの日本語編集者です。
海外（または国内）のApple関連記事の原文を渡すので、日本の読者が
原文を読まなくても内容が分かる日本語の解説を書いてください。

厳守すること:
- 原文を逐語訳しない。事実を自分の言葉で整理し直して書く。
- 原文に書かれていない事実・数値・日付・仕様を足さない。分からないことは書かない。
- points は3〜5個の箇条書き。1つ40文字以内。記事の要点だけを並べる。
- body は800〜1200文字。2〜4段落に分け、段落の区切りは改行2つ。見出しや箇条書きは入れない。
- 噂や観測記事は「〜と報じられている」「〜とされる」と伝聞であることを必ず示す。
- 「この記事では」「筆者は」のようなメタな言い回しを使わない。
- 出力はJSONオブジェクトのみ。前置き・説明・コードフェンスを付けない。

出力形式:
{{"points": ["...", "..."], "body": "..."}}

記事の見出し: {title}
出典: {source}
原文:
{text}
"""


def parse_detail_json(text: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise llm_providers.ResponseInvalid("JSONオブジェクトが見つかりません")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise llm_providers.ResponseInvalid(f"JSONとして読めません: {exc}") from exc
    points = data.get("points")
    body = str(data.get("body", "")).strip()
    if not isinstance(points, list) or not 2 <= len(points) <= 6:
        raise llm_providers.ResponseInvalid("points が3〜5個ではありません")
    if len(body) < 300:
        raise llm_providers.ResponseInvalid(f"body が短すぎます（{len(body)}文字）")
    return {"points": [str(p).strip() for p in points if str(p).strip()], "body": body}


def build_detail(item: dict) -> dict | None:
    """1記事ぶんの解説を作る。原文が取れなければ作らない（要約だけ表示する）。"""
    text = fetch_article_text(item["url"])
    if len(text) < 400:
        return None
    prompt = DETAIL_PROMPT.format(
        title=item["title_original"], source=item["source"], text=text
    )
    try:
        raw = llm_providers.generate_text(prompt, validate=parse_detail_json)
    except llm_providers.LLMError as exc:
        print(f"  × 解説の生成に失敗（次回に回します）: {item['title_ja']} / {exc}")
        return None
    detail = parse_detail_json(raw)
    detail["chars"] = len(detail["body"])
    return detail


def write_detail_file(item: dict, detail: dict) -> None:
    os.makedirs(DETAIL_DIR, exist_ok=True)
    payload = {
        "id": item["id"],
        "title_ja": item["title_ja"],
        "summary_ja": item["summary_ja"],
        "points": detail["points"],
        "body": detail["body"],
        "source": item["source"],
        "url": item["url"],
        "lang": item["lang"],
        "region": item["region"],
        "category": item["category"],
        "importance": item["importance"],
        "image": item.get("image", ""),
        "published": item["published"],
    }
    path = os.path.join(DETAIL_DIR, f"{item['id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")


def generate_details(new_items: list[dict], existing_items: list[dict]) -> None:
    """新着から順に解説を作る。枠が余ったら、まだ解説のない古い記事を埋める。"""
    targets = [item for item in new_items if not item.get("has_detail")]
    if len(targets) < MAX_DETAILS_PER_RUN:
        # existing_items には new_items も含まれるので、二重に処理しないよう除く。
        queued = {item["id"] for item in targets}
        backlog = [
            item
            for item in existing_items
            if item["id"] not in queued
            and not item.get("has_detail")
            and not os.path.exists(os.path.join(DETAIL_DIR, f"{item['id']}.json"))
        ]
        targets += backlog[: MAX_DETAILS_PER_RUN - len(targets)]
    targets = targets[:MAX_DETAILS_PER_RUN]
    if not targets:
        return

    print(f"■ 解説を生成（{len(targets)}件）")
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        for item, detail in zip(targets, pool.map(build_detail, targets)):
            if detail is None:
                continue
            write_detail_file(item, detail)
            item["has_detail"] = True
            done += 1
            print(f"  ○ {detail['chars']}字 {item['title_ja']}")
    print(f"  {done}/{len(targets)}件を作成")


def cleanup_details(kept_ids: set[str]) -> None:
    """一覧から消えた記事の解説ファイルを片付ける。"""
    if not os.path.isdir(DETAIL_DIR):
        return
    removed = 0
    for name in os.listdir(DETAIL_DIR):
        if name.endswith(".json") and name[:-5] not in kept_ids:
            os.remove(os.path.join(DETAIL_DIR, name))
            removed += 1
    if removed:
        print(f"  古い解説ファイルを{removed}件削除")


# --------------------------------------------------------------------------
# 保存
# --------------------------------------------------------------------------

def load_existing() -> dict:
    if not os.path.exists(OUTPUT_PATH):
        return {"updated_at": None, "items": []}
    try:
        with open(OUTPUT_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"updated_at": None, "items": []}
    data.setdefault("items", [])
    return data


def main() -> int:
    load_env()

    with open(FEEDS_PATH, encoding="utf-8") as f:
        feeds = [feed for feed in json.load(f)["feeds"] if feed.get("enabled", True)]

    print(f"■ RSS取得（{len(feeds)}媒体）")
    fetched = collect_feed_items(feeds)
    print(f"  合計 {len(fetched)}件")

    existing = load_existing()
    existing_items = existing["items"]
    known_ids = {item["id"] for item in existing_items}
    known_urls = {canonical_url(item["url"]) for item in existing_items}
    known_titles = [normalize_title(item.get("title_original", "")) for item in existing_items]

    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=INTAKE_DAYS)

    fetched.sort(key=lambda item: item["published"], reverse=True)

    new_items: list[dict] = []
    for item in fetched:
        published = parse_date(item["published"])
        if published is None or published < cutoff or published > now + dt.timedelta(hours=12):
            continue
        if item["id"] in known_ids or item["url"] in known_urls:
            continue
        if is_duplicate(item["title_original"], known_titles):
            continue
        known_ids.add(item["id"])
        known_urls.add(item["url"])
        known_titles.append(normalize_title(item["title_original"]))
        new_items.append(item)

    print(f"■ 新着 {len(new_items)}件（重複と期間外を除外）")
    if len(new_items) > MAX_NEW_PER_RUN:
        print(f"  うち{MAX_NEW_PER_RUN}件を今回処理（残りは次回）")
        # 上限で切るときも Apple 公式（Newsroom）の発表だけは取りこぼさない。
        official = [item for item in new_items if item["official"]]
        others = [item for item in new_items if not item["official"]]
        new_items = (official + others)[:MAX_NEW_PER_RUN]
        new_items.sort(key=lambda item: item["published"], reverse=True)

    enriched: list[dict] = []
    replaced: set[str] = set()
    if new_items:
        enriched = enrich(new_items)
        print(f"  日本語化 {len(enriched)}件（Apple無関係と判定された分は除外）")
        enriched, replaced = dedupe_stories(enriched, existing_items)
        print(f"  掲載対象 {len(enriched)}件")
        # 実際に載せる記事だけページを取りに行く（無駄なアクセスを増やさないため）。
        fill_missing_images(enriched)

    merged = enriched + [item for item in existing_items if item["id"] not in replaced]

    # 掲載が決まった記事に、原文を読まなくても分かる日本語の解説を用意する。
    generate_details(enriched, merged)
    keep_after = now - dt.timedelta(days=KEEP_DAYS)
    merged = [
        item
        for item in merged
        if (parse_date(item.get("published", "")) or now) >= keep_after
    ]
    merged.sort(key=lambda item: item["published"], reverse=True)
    merged = [to_public(item) for item in merged[:KEEP_MAX]]
    cleanup_details({item["id"] for item in merged})

    payload = {
        "updated_at": now.astimezone(JST).isoformat(),
        "categories": CATEGORIES,
        "sources": sorted({item["source"] for item in merged}),
        "count": len(merged),
        "detail_count": sum(1 for item in merged if item.get("has_detail")),
        "items": merged,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
        f.write("\n")

    print(f"■ 書き出し: {OUTPUT_PATH}（掲載 {len(merged)}件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
