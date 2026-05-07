# ai-research — 生成AI情報収集・スコアリング・配信システム

RSSフィード・YouTube・Redditから毎日生成AI記事を自動収集し、AIでスコアリングして日次Markdownレポートを生成するシステムです。

## Phase 実装状況

- [ ] **Phase 1**: RSS収集 + YouTube/Reddit + スコアリング基盤
- [ ] **Phase 2**: フィードバック学習システム
- [ ] **Phase 3**: 集約システム（週次・月次）
- [ ] **Phase 4**: コンテンツ生成システム
- [ ] **Phase 5**: LINE通知 + GitHub Pages + X自動投稿
- [ ] **Phase 6**: Gemini → Claude API 切り替え
- [ ] **Phase 7**: 総合知識ベース・インテリジェンス統合

## クイックスタート

```bash
# 1. リポジトリをクローン
git clone https://github.com/your-username/ai-research.git
cd ai-research

# 2. 依存パッケージをインストール
pip install -r requirements.txt

# 3. 環境変数を設定
cp .env.example .env
# .env を編集して GEMINI_API_KEY または CLAUDE_API_KEY を設定

# 4. 実行
python src/main.py
```

## 実行コマンド

```bash
python src/main.py                    # 通常実行（Gemini優先）
python src/main.py --claude           # Claude API 優先
python src/main.py --dry-run          # 収集・スコアリングのみ（出力なし）
python src/main.py --date 2026-05-07  # 特定日付で出力（テスト用）
```

## 収集ソース

| ソース | 方式 | 言語 | 備考 |
|--------|------|------|------|
| Anthropic Blog | RSS | EN | 一次情報 |
| OpenAI Blog | RSS | EN | 一次情報 |
| Google DeepMind Blog | RSS | EN | 一次情報 |
| Hugging Face Blog | RSS | EN | 一次情報 |
| Zenn AI トピック | RSS | JA | 日本語 |
| ITmedia AI | RSS | JA | 日本語 |
| arXiv cs.AI | RSS | EN | 論文 |
| TechCrunch AI | RSS | EN | ニュース |
| Papers With Code | RSS | EN | 論文 |
| デジタル庁 | RSS | JA | 政策 |
| YouTube | Data API v3 | EN | APIキー必要 |
| Reddit | JSON API | EN | 認証不要 |

## スコアリング項目

各記事を以下の5項目（各1〜5点）で評価し、重み付き合計スコアを算出します：

| 項目 | 重み | 説明 |
|------|------|------|
| 重要度（importance） | 30% | AI業界全体への影響度 |
| 新規性（novelty） | 25% | 新規性・革新性 |
| ビジネス価値（business_value） | 20% | 中小企業の活用可能性 |
| 学習価値（learning_value） | 15% | 教育的価値 |
| 一次情報度（primary_source_score） | 10% | 情報源の信頼性 |

## 出力ファイル

```
articles/YYYY/MM/DD.md    日次Markdownレポート
scores/YYYY/MM/DD.json    スコアリング結果JSON
terms/glossary.md         AI用語集（自動追記）
```

## 環境変数

| 変数名 | 用途 | 必須 |
|--------|------|------|
| `GEMINI_API_KEY` | Gemini APIスコアリング | Phase1推奨 |
| `CLAUDE_API_KEY` | Claude APIスコアリング | Phase2以降 |
| `YOUTUBE_API_KEY` | YouTube Data API v3 | 任意 |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE通知 | Phase5 |
| `LINE_USER_ID` | LINE通知先ユーザーID | Phase5 |
| `X_API_KEY` | X自動投稿 | Phase5（有料 $100/月） |

## GitHub Actions 自動実行

- **daily.yml**: 毎朝6時JST（UTC 21:00）に自動実行
- **weekly.yml**: 毎週日曜6時30分JST（Phase3実装後）
- **knowledge.yml**: 毎週日曜7時JST（Phase7実装後）

### Secrets 設定方法

GitHubリポジトリの `Settings > Secrets and variables > Actions` で以下を設定：
- `GEMINI_API_KEY`
- `YOUTUBE_API_KEY`（任意）

## コスト試算（Phase6 Claude API切り替え後）

| 用途 | モデル | 月コスト目安 |
|------|--------|------------|
| スコアリング（月900記事） | claude-haiku-4-5 | $1.5〜2 |
| 週次・月次まとめ生成 | claude-sonnet-4-5 | $1〜2 |
| コンテンツ生成 | claude-sonnet-4-5 | $2〜3 |
| **合計** | | **$5〜8/月** |

## LINE Messaging API セットアップ（Phase5）

1. [LINE Developers](https://developers.line.biz/) でチャネルを作成
2. `Messaging API` チャネルを選択
3. チャネルアクセストークンを発行
4. 友だち追加用QRコードを生成して自分のアカウントと連携
5. ユーザーIDを取得（Webhook受信で確認）
6. GitHub Secrets に `LINE_CHANNEL_ACCESS_TOKEN`・`LINE_USER_ID` を設定

## ライセンス

MIT
