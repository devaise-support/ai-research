# RSS・プラットフォーム収集スキル定義

## 目的
生成AI分野の最新情報をRSSフィード・YouTube・Redditから収集し、
重複なく品質の高い記事セットを毎日構築する。

## 収集基準

### 共通ルール
- 過去 `max_age_days`（デフォルト14日）以内の記事のみ対象
- `seen_articles.json` に記録済みのURLはスキップ（重複防止）
- URL正規化：`scheme + netloc + path` のみでIDを生成（クエリパラメータ除外）

### RSS収集ルール
- 1フィードあたり最大 `max_per_feed`（デフォルト3件）
- 全RSS上限：`total_limit`（デフォルト10件）
- `published_at` 降順でソート後に上位件数を選択
- HTMLタグを除去してから `summary_raw`（最大500字）として保存

### YouTube収集ルール
- `YOUTUBE_API_KEY` 未設定時はスキップ（エラーにしない）
- チャンネルIDでの検索 + キーワード検索クエリの両方を実行
- `snippet.description` の先頭500字を `summary_raw` として使用

### Reddit収集ルール
- 認証不要のJSON API（`https://www.reddit.com/r/{sub}/hot.json`）
- `User-Agent` ヘッダーを必ず設定（ないと403エラー）
- `score`（upvote数）が `min_score` 以上のみ収集
- タイトルにAIキーワードが含まれること

## 一次情報の判定（2段階）

1段階：URLドメインが `primary_domains.yaml` に含まれる → `is_primary_source=True`

2段階：タイトルまたは要約に `primary_keywords_ja` または `primary_keywords_en` が含まれる
→ `is_primary_source=True`

いずれかを満たせばOK（OR条件）

## エラー処理方針

- 1フィード失敗 → ログ出力してスキップ（全体は継続）
- タイムアウト（10秒）→ `requests.exceptions.Timeout` をキャッチしてスキップ
- feedparserのパースエラー → スキップ
- YouTube API quota超過 → スキップ（翌日まで待機）
- Reddit API 429エラー → 5秒待機後リトライ1回

## 収集サマリーログ形式（必須）

```
収集完了: RSS 8件 / YouTube 2件 / Reddit 2件 = 合計 12件
スキップ: 重複 3件, 期限切れ 2件, エラー 1件（ITmedia AI: タイムアウト）
```
