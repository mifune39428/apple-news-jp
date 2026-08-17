# Apple News 日本語まとめ

海外・国内のApple関連メディアのRSSを自動で集め、日本語の見出しと3行程度の要約を付けて
静的サイトとして公開するツール。GitHub Actions が6時間ごとに走り、GitHub Pages が更新される。

- サイト: https://mifune39428.github.io/apple-news-jp/
- 更新: 日本時間の 3時 / 9時 / 15時 / 21時（GitHub Actions の cron）

## しくみ

```
RSS 14媒体 ──> collect.py ──> Gemini(→Groq→Claude→OpenAI) ──> docs/articles.json ──> GitHub Pages
              重複除去・期間絞り込み   見出し翻訳＋独自要約＋分類        静的サイトが読む
```

- 原文の本文は保存も掲載もしない。持つのは**日本語の見出し・独自要約・解説・出典名・原文リンク・サムネイル**。
- 記事ごとに詳細ページ（`docs/a.html?id=...`）があり、原文を読まなくても分かる
  **要点3〜5個＋800〜1200字の日本語解説**を載せる。全訳ではなく、原文をもとに書き起こしたもの。
- 原文そのものを日本語で読みたい場合は、詳細ページの「原文を日本語で全文読む」から
  Google翻訳を通した原文ページが開く（こちらの著作物としては保持しない）。
- サムネイルはRSS内の画像（`media:thumbnail` / `enclosure` / 本文の最初の `<img>`）を使い、
  見つからない記事だけ元ページの `og:image` を取りに行く（実際に載せる記事のみ）。
- LLMがApple無関係と判断した記事（Daring Fireball の雑記など）は自動で落とす。
- どのLLMも使えなかった分は保存せず、次の実行で拾い直す（生煮えの記事を出さないため）。

## ファイル

| ファイル | 役割 |
| --- | --- |
| `collect.py` | 収集・重複除去・日本語化・`docs/articles.json` の書き出し |
| `feeds.json` | 収集元のRSS一覧（`enabled: false` で一時停止できる） |
| `llm_providers.py` | LLMの多段フォールバック（dual_draft_poster からのコピー） |
| `docs/index.html` | サイト本体（依存なしの1ファイル、PWA対応） |
| `docs/articles.json` | 一覧用の生成データ。Actions が自動コミットする |
| `docs/a.html` | 記事の詳細ページ（`?id=` で `docs/articles/<id>.json` を読む） |
| `docs/articles/` | 記事ごとの解説データ。一覧から消えた記事のファイルは自動削除される |
| `.github/workflows/update.yml` | 6時間ごとの自動実行 |
| `更新.command` | Mac から手動で即更新（ダブルクリック） |

## 設定

- **GitHub Secrets**: `GEMINI_API_KEY` は必須。`GROQ_API_KEY` を入れておくとGeminiの無料枠が切れても止まらない。
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` は任意。
- **ローカル実行**: このフォルダの `.env` に同じキーを書く（gitignore 済み）。

## 調整しどころ（`collect.py` の定数）

- `INTAKE_DAYS = 3` — 何日前までの記事を取り込むか
- `MAX_NEW_PER_RUN = 40` — 1回の実行で日本語化する上限（無料枠を使い切らないための蓋）
- `BATCH_SIZE = 5` — 見出し・要約をまとめて作るときの記事数（Groqの分間トークン制限に合わせてある）
- `MAX_DETAILS_PER_RUN = 25` — 1回の実行で作る解説の本数（1記事につきLLMを1回使う）
- `DETAIL_WORKERS = 3` — 解説生成の並行数
- `KEEP_DAYS = 30` / `KEEP_MAX = 400` — サイトに残す期間と件数

収集元を増やすときは `feeds.json` に足すだけでよい。`lang` が `ja` の媒体は翻訳せず要約だけ作る。

## 著作権について

各記事の権利は出典元にある。このサイトが持つのは自動生成した日本語の見出し・要約・解説、
出典名、原文へのリンクだけで、本文の全訳や転載はしない。
解説は原文を読んで書き起こした要約であり、逐語訳にならないようプロンプトで縛っている。掲載停止の依頼があれば
`feeds.json` から該当媒体を外す。
