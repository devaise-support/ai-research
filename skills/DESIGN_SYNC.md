# DESIGN_SYNC スキル — Stitch × DESIGN.md 連携

## 概要

Google Stitch で UI を更新した際に、`~/.claude/design-md-jp/` の DESIGN.md から
日本語最適化ルールを自動適用し、`publisher.py` → `docs/` を再生成するスキル。

**このファイルを読めば毎回同じ手順・同じ品質で連携できる。**

---

## トリガー条件

以下のような指示があった場合にこのスキルを実行する：

- 「Stitchを更新したので再取得してください」
- 「UIを更新してほしい」
- 「デザインを同期してください」
- 「Stitch × DESIGN.md 連携を実行して」

---

## 必要な情報

| 情報 | 入手方法 |
|------|---------|
| Stitch API キー | ユーザーから提供（`AQ.xxx` 形式） |
| プロジェクト ID | `config/stitch_config.yaml` に記録済み（`15799005587672629940`） |
| DESIGN.md ルール | `config/stitch_config.yaml` の `design_rules` セクションにキャッシュ済み |

API キーがない場合は `「Stitch API キーを教えてください」` とユーザーに確認する。

---

## 実行手順（5ステップ）

### Step 1: Stitch スクリーン一覧取得

`list_screens` MCP ツールを呼び出す：

```
list_screens({ projectId: "15799005587672629940" })
```

戻り値の `screens[]` 配列から各スクリーンの `name` と `title` を記録する。

### Step 2: 各スクリーンの HTML 取得

ダッシュボード（title に "dashboard" または "ダッシュボード" を含む）と
日次レポート（title に "daily" または "日次" を含む）を優先して取得する。

各スクリーンに対して `get_screen` MCP ツールを呼び出す：

```
get_screen({
  name: "projects/15799005587672629940/screens/{screenId}",
  projectId: "15799005587672629940",
  screenId: "{screenId}"
})
```

`htmlCode.downloadUrl` の URL を `requests.get()` でダウンロードして HTML を取得する。
（`htmlCode.downloadUrl` は直接 HTML 文字列ではなく、ダウンロード用 URL であることに注意）

### Step 3: DESIGN.md ルールの確認

`config/stitch_config.yaml` の `design_rules` を読み込む（キャッシュ済みの値を使用）。

適用する3つのルール：

| ルール | 値 | 参照元 DESIGN.md |
|-------|-----|-----------------|
| 日本語フォントスタック | `Inter, Hiragino Kaku Gothic ProN, Hiragino Sans, Meiryo, sans-serif` | Zenn section 3.3 |
| 本文行間 | `line-height: 1.8` | Zenn section 1 |
| ページ背景色 | `#f8f7f6`（SmartHR Stone 01） | SmartHR section 2 |

ルールを更新したい場合は `~/.claude/design-md-jp/zenn/DESIGN.md` と
`~/.claude/design-md-jp/smarthr/DESIGN.md` を直接参照して値を再確認する。

### Step 4: publisher.py の `_stitch_head()` を更新

`src/publisher.py` の `_stitch_head()` 関数内の以下3箇所を修正する：

**A. fontFamily — 日本語フォントスタック追加**

テキスト系フォント（`body-md`, `body-lg`, `h1`, `h2`, `h3`, `label-sm`）を以下に更新：
```python
["Inter", "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Meiryo", "sans-serif"]
```
コード系フォント（`data-mono`）は英語フォントのみ維持：
```python
["Inter", "sans-serif"]
```

**B. background カラートークン — ウォームグレーに変更**

`"background": "#f9f9ff"` → `"background": "#f8f7f6"`

**C. `<style>` タグに行間 CSS を追加**

既存の `.material-symbols-outlined` スタイルの行に続けて以下を追加：
```html
<style>
  .material-symbols-outlined { font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24; }
  body { line-height: 1.8; }
  p, li, td, th { line-height: 1.8; }
</style>
```

> **注意**: Stitch から取得した HTML の Tailwind 設定（カラートークン・spacing・borderRadius）は
> そのまま保持する。上記3箇所だけを変更する。

### Step 5: docs/ 再生成 → git commit & push

```bash
# HTML 再生成
python src/publisher.py --pages-only --date {今日の日付}

# 確認
python src/stitch_sync.py --check

# コミット
git add src/publisher.py docs/ config/stitch_config.yaml
git commit -m "feat: sync Stitch UI with DESIGN.md Japanese optimizations"
git push
```

---

## DESIGN.md ルール更新手順

Stitch デザインが大幅に変わった場合や DESIGN.md のルールを見直す場合：

1. `~/.claude/design-md-jp/zenn/DESIGN.md` を読む
   - `section 3.3 font-family 指定` からフォントスタックを確認
   - `section 1` から `line-height` 値を確認

2. `~/.claude/design-md-jp/smarthr/DESIGN.md` を読む
   - `section 2 Neutral — Stone Scale` から `Stone 01` の値を確認

3. `config/stitch_config.yaml` の `design_rules` を更新する

---

## Stitch MCP API 仕様（参考）

| ツール | パラメータ | 戻り値 |
|--------|----------|--------|
| `list_screens` | `{ projectId: "..." }` | `{ screens: [{ name, title, screenshot, htmlCode }] }` |
| `get_screen` | `{ name, projectId, screenId }` | Screen オブジェクト（htmlCode.downloadUrl を含む） |

- `htmlCode.downloadUrl` は FIFE ベース URL（直接 `requests.get()` でダウンロード可能）
- スクリーン `name` の形式: `projects/{projectId}/screens/{screenId}`
- `screenId` は `name` の最後のセグメント（`/` 以降）

---

## 自動化スクリプト

`src/stitch_sync.py` を使用すると DESIGN.md パッチの適用状態を確認・再適用できる：

```bash
python src/stitch_sync.py --check      # 適用状態確認
python src/stitch_sync.py --dry-run    # 変更内容のプレビュー
python src/stitch_sync.py              # パッチ適用
```

---

## 品質チェックリスト（完了基準）

- [ ] `docs/index.html` を開いて日本語テキストが適切なフォントで表示されること
- [ ] 背景色が温かみのあるグレー（`#f8f7f6`）であること（純白の `#f9f9ff` でないこと）
- [ ] 記事本文・テーブルの行間が広め（1.8）であること
- [ ] Stitch のカラートークン・コンポーネントデザインが維持されていること
- [ ] GitHub Pages URL で正しく表示されること
