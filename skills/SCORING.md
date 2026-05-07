# スコアリングスキル定義

## 目的
収集した生成AI記事を5つの観点で評価し、
重み付きスコアを計算してコンテンツ化・配信の優先順位を決める。

## スコア項目（各1〜5の整数）

| 項目 | 説明 |
|------|------|
| importance | AI業界全体への影響度。業界標準が変わるなら5、個人ブログ程度なら1 |
| novelty | 新規性・革新性。初報告・初公開なら5、既知情報の焼き直しなら1 |
| business_value | 中小企業・個人事業主が今すぐ活用できる可能性。具体的なツールや事例なら高スコア |
| learning_value | 技術者・非技術者の学習価値。用語や概念の解説が丁寧なら高スコア |
| primary_source_score | 情報源の信頼性。公式発表・査読済み論文なら5、まとめ記事・転載なら1〜2 |

## 最終スコア計算式

```
raw_score = Σ(score_item × weight_item)
final_score = min(5.0, raw_score × category_boost)
```

重みと category_boost は `config/scoring_weights.json` から読み込む。

## フラグ判定

| フラグ | 条件 | 用途 |
|--------|------|------|
| is_weekly_candidate | final_score >= 3.5 | 週次レポートに掲載 |
| is_monthly_candidate | final_score >= 4.0 | 月次レポートに掲載 |
| is_content_candidate | final_score >= 4.0 | コンテンツ生成対象 |
| is_x_post_candidate | final_score >= 4.5 | X自動投稿対象 |

## AIへのプロンプト（システムプロンプト）

```
あなたは生成AI分野の記事評価専門家です。
以下の記事を5つの観点で評価し、JSONのみで返してください。
説明文や前置きは一切不要です。

評価項目（各1〜5の整数）:
- importance: AI業界全体への影響度
- novelty: 新規性・革新性
- business_value: 中小企業・個人事業主の活用可能性
- learning_value: 学習・教育的価値
- primary_source_score: 情報源の一次情報度

追加情報:
- summary_ja: 日本語での3行以内の要約
- tags: 記事に関連するキーワード3〜7個（英語・日本語混在可）
- key_terms: 記事に登場する新出AI用語のみ（既知用語は除く）
- target_clients: 活用できそうな業種（例: 美容院, 不動産, 飲食店）
- is_primary: 一次情報かどうか（true/false）
- primary_reason: 一次情報と判断した理由（または非一次情報の理由）
- category: 記事のカテゴリ（LLM・基盤モデル/AIエージェント/ツール・サービス/論文・研究/
             ビジネス活用事例/規制・倫理・政策/日本国内AI動向/ニュース・動向/動画・チュートリアル/
             コミュニティ・議論 のいずれか）
- content_potential:
    blog: ブログ記事化できるか（true/false）
    instagram: Instagram投稿にできるか（true/false）
    youtube: YouTube台本にできるか（true/false）
    client_report: クライアントレポートに使えるか（true/false）

出力形式（このJSONのみを返すこと）:
{
  "importance": 4,
  "novelty": 5,
  "business_value": 3,
  "learning_value": 4,
  "primary_source_score": 5,
  "summary_ja": "...",
  "tags": ["GPT-5", "OpenAI", "マルチモーダル"],
  "key_terms": ["MCP", "vibe coding"],
  "target_clients": ["美容院", "不動産"],
  "is_primary": true,
  "primary_reason": "OpenAI公式ブログからの発表記事",
  "category": "LLM・基盤モデル",
  "content_potential": {
    "blog": true,
    "instagram": true,
    "youtube": false,
    "client_report": true
  }
}
```

## ユーザープロンプトのテンプレート

```
タイトル: {title}
ソース: {source_name}
URL: {url}
言語: {language}
公開日: {published_at}
要約:
{summary_raw}
```

## レート制限対策

- API呼び出し間隔：各スコアリング後に `time.sleep(0.5)` を実行
- APIエラー時：1回リトライ後にスキップ、デフォルトスコア（全項目3）を使用

## モックスコアの挙動（APIキー未設定時）

- 各スコア項目：`random.uniform(2.5, 4.0)` で生成
- `summary_ja`：「（APIキー未設定のためモックスコアを使用）」
- `tags`：`["mock", "test"]`
- `key_terms`：空リスト
- 動作継続のためエラーにしない
