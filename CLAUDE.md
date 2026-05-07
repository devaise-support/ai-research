# ai-research プロジェクト

## 概要
生成AI分野の最新情報を毎日自動収集・スコアリングし、日次/週次/月次でMarkdownレポートを生成するシステム。

## 技術スタック
- 言語：Python 3.11+
- AIスコアリング：Gemini API（Phase1）→ Claude API（Phase2以降）
- 情報蓄積：Markdown + Git
- 自動実行：GitHub Actions（毎朝6時JST）
- 通知：LINE Messaging API（Phase5）
- 公開：GitHub Pages（Phase5）

## 実行方法

```bash
# 通常実行（Gemini API）
python src/main.py

# Claude API使用
python src/main.py --claude

# ドライラン（スコアリングのみ、ファイル出力なし）
python src/main.py --dry-run

# 特定日付で実行（テスト用）
python src/main.py --date 2026-05-07

# フィードバック記録
python src/feedback.py --id [記事ID] --type like

# 週次集約
python src/aggregator.py --weekly

# 月次集約
python src/aggregator.py --monthly

# 知識ベース更新
python src/knowledge_builder.py --all

# トレンド分析
python src/trend_analyzer.py
```

## 環境変数（.env ファイルまたは GitHub Secrets）

| 変数名 | 用途 | 必須 |
|--------|------|------|
| GEMINI_API_KEY | Gemini APIスコアリング | Phase1推奨 |
| CLAUDE_API_KEY | Claude APIスコアリング・生成 | Phase2以降 |
| YOUTUBE_API_KEY | YouTube Data API v3 | 任意 |
| LINE_CHANNEL_ACCESS_TOKEN | LINE通知 | Phase5 |
| LINE_USER_ID | LINE通知先ユーザーID | Phase5 |
| X_API_KEY | X自動投稿 | Phase5（有料） |
| X_API_SECRET | X自動投稿 | Phase5（有料） |
| X_ACCESS_TOKEN | X自動投稿 | Phase5（有料） |
| X_ACCESS_TOKEN_SECRET | X自動投稿 | Phase5（有料） |

## ディレクトリ構造

```
ai-research/
├── src/                    # ソースコード
│   ├── fetcher.py          # RSS + YouTube + Reddit 収集
│   ├── scorer.py           # AIスコアリング（Gemini/Claude/Mock）
│   ├── writer.py           # 日次Markdown生成
│   ├── main.py             # 全体実行エントリーポイント
│   ├── feedback.py         # フィードバック学習（Phase2）
│   ├── aggregator.py       # 週次・月次集約（Phase3）
│   ├── comparator.py       # トレンド比較（Phase3）
│   ├── content_generator.py # コンテンツ生成（Phase4）
│   ├── publisher.py        # LINE通知・GitHub Pages（Phase5）
│   ├── knowledge_builder.py # 知識ベース構築（Phase7）
│   └── trend_analyzer.py   # トレンド分析（Phase7）
├── config/                 # 設定ファイル
│   ├── rss_feeds.yaml      # RSSソース設定
│   ├── platform_sources.yaml # YouTube・Reddit設定
│   ├── primary_domains.yaml  # 一次情報ドメインリスト
│   ├── scoring_weights.json  # スコアリング重み
│   ├── client_profiles.yaml  # クライアント業種設定（Phase4）
│   └── topic_mapping.yaml    # トピック分類設定（Phase7）
├── skills/                 # スキル定義（Claude Code参照用）
│   ├── RESEARCH.md         # 収集スキル定義
│   ├── SCORING.md          # スコアリングプロンプト定義
│   ├── CONTENT_GEN.md      # コンテンツ生成プロンプト（Phase4）
│   └── CLIENT_REPORT.md    # クライアントレポート定義（Phase4）
├── articles/               # 日次Markdownレポート（articles/YYYY/MM/DD.md）
├── scores/                 # スコアリング結果JSON（scores/YYYY/MM/DD.json）
├── terms/
│   └── glossary.md         # AI用語集
├── feedback/               # フィードバックデータ（Phase2）
├── weekly/                 # 週次レポート（Phase3）
├── monthly/                # 月次レポート（Phase3）
├── trends/                 # トレンドデータ（Phase3）
├── content/                # 生成コンテンツ（Phase4）
│   ├── blog/
│   ├── instagram/
│   ├── youtube/
│   ├── client_reports/
│   └── x_posts/
├── docs/                   # GitHub Pages（Phase5）
│   ├── index.html
│   └── daily/
├── knowledge/              # 総合知識ベース（Phase7）
│   ├── topics/
│   ├── comparisons/
│   ├── industry-impact/
│   ├── trends/
│   └── index.md
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
└── .github/workflows/
    ├── daily.yml           # 毎朝6時JST実行
    ├── weekly.yml          # 毎週日曜6時JST実行（Phase3）
    └── knowledge.yml       # 毎週日曜7時JST実行（Phase7）
```

## コーディング規則

1. **単一責任の原則**: 各ファイルは1つの機能に集中する
2. **エラーハンドリング必須**: 1ソース失敗で全体停止しない。各ソースを `try/except` で囲む
3. **ログは日本語出力**: `logging` を使用し、収集件数・スキップ件数・エラー件数をサマリーに含める
4. **設定値はすべてconfigに外部化**: URLもAPIモデル名もハードコード禁止
5. **APIキーは環境変数から読む**: `os.environ.get()` または `python-dotenv` 使用

## Phase 実装状況

- [x] Phase 1: RSS収集 + YouTube/Reddit + スコアリング基盤
- [x] Phase 2: フィードバック学習システム
- [x] Phase 3: 集約システム（週次・月次）
- [x] Phase 4: コンテンツ生成システム
- [x] Phase 5: LINE通知 + GitHub Pages + X自動投稿
- [x] Phase 6: Gemini → Claude API 切り替え
- [ ] Phase 7: 総合知識ベース・インテリジェンス統合
