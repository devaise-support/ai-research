"""
publisher.py - 配信モジュール

機能:
  1. LINE通知: トップ3記事を Flex Message カード形式で送信
  2. GitHub Pages: docs/index.html（最新10日分）と docs/daily/YYYY-MM-DD.html を生成
  3. X（Twitter）自動投稿: スコア4.5以上（x_post_candidate）の記事を1日1〜3件投稿
     ※ X_API_KEY 未設定時は content/x_posts/ へのファイル生成のみ行い投稿はスキップ

単一責任: 配信処理のみ。収集・スコアリング・コンテンツ生成は別モジュールが担当。
"""

import argparse
import json
import logging
import os
import re
import sys
import io
from datetime import date, datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# ログ設定（Windows cp932対策）
# ---------------------------------------------------------------------------

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
SCORES_DIR = ROOT / "scores"
ARTICLES_DIR = ROOT / "articles"
DOCS_DIR = ROOT / "docs"
X_POSTS_DIR = ROOT / "content" / "x_posts"

# GitHub Pages のベースURL（リポジトリに合わせて .env で上書き可）
GITHUB_PAGES_BASE = os.environ.get("GITHUB_PAGES_URL", "https://example.github.io/ai-research")


# ---------------------------------------------------------------------------
# スコアJSONからの記事読み込み
# ---------------------------------------------------------------------------

def load_articles_for_date(target_date: date) -> list[dict]:
    """指定日のスコアJSONを読み込む"""
    json_path = SCORES_DIR / str(target_date.year) / f"{target_date.month:02d}" / f"{target_date.day:02d}.json"
    if not json_path.exists():
        return []
    try:
        with open(json_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"スコアJSON読み込みエラー [{json_path}]: {e}")
        return []


def load_recent_articles(days: int = 10) -> list[tuple[date, list[dict]]]:
    """直近 N 日分の記事を日付降順で読み込む"""
    results = []
    for json_path in sorted(SCORES_DIR.rglob("*.json"), reverse=True):
        if json_path.name == "seen_articles.json":
            continue
        try:
            # パスから日付を復元: scores/YYYY/MM/DD.json
            parts = json_path.relative_to(SCORES_DIR).parts
            if len(parts) == 3:
                d = date(int(parts[0]), int(parts[1]), int(parts[2].replace(".json", "")))
                with open(json_path, encoding="utf-8") as f:
                    articles = json.load(f)
                results.append((d, articles))
                if len(results) >= days:
                    break
        except Exception:
            continue
    return results


# ---------------------------------------------------------------------------
# 1. LINE通知
# ---------------------------------------------------------------------------

def _build_flex_message(articles: list[dict], target_date: date) -> dict:
    """LINE Flex Message（カルーセル形式）を構築する"""
    bubbles = []
    for article in articles[:3]:
        title = article.get("title", "（タイトルなし）")[:40]
        source = article.get("source_name", "")
        score = article.get("score", 0.0)
        stars = "★" * min(int(score), 5) + "☆" * max(0, 5 - int(score))
        summary = (article.get("summary_raw") or "")[:80]
        url = article.get("url", "")
        category = article.get("category", "")

        bubble = {
            "type": "bubble",
            "size": "kilo",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": category,
                        "size": "xs",
                        "color": "#888888",
                    }
                ],
                "paddingBottom": "4px",
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "text",
                        "text": title,
                        "weight": "bold",
                        "size": "sm",
                        "wrap": True,
                        "maxLines": 3,
                    },
                    {
                        "type": "text",
                        "text": stars + f"  {score:.1f}",
                        "size": "xs",
                        "color": "#f5a623",
                    },
                    {
                        "type": "text",
                        "text": summary or "（要約なし）",
                        "size": "xs",
                        "color": "#555555",
                        "wrap": True,
                        "maxLines": 4,
                    },
                ],
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "button",
                        "action": {
                            "type": "uri",
                            "label": "記事を読む",
                            "uri": url or "https://example.com",
                        },
                        "style": "primary",
                        "height": "sm",
                        "color": "#0066CC",
                    },
                ],
            },
        }
        bubbles.append(bubble)

    # GitHub Pages へのリンクボタンを最後のバブルに追加
    pages_url = f"{GITHUB_PAGES_BASE}/daily/{target_date.isoformat()}.html"
    bubbles.append({
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "justifyContent": "center",
            "contents": [
                {
                    "type": "text",
                    "text": f"{target_date.isoformat()} の全レポートはこちら",
                    "size": "sm",
                    "wrap": True,
                    "align": "center",
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "action": {
                        "type": "uri",
                        "label": "全レポートを見る",
                        "uri": pages_url,
                    },
                    "style": "secondary",
                    "height": "sm",
                },
            ],
        },
    })

    return {
        "type": "flex",
        "altText": f"[AI情報] {target_date.isoformat()} トップ{min(len(articles), 3)}記事",
        "contents": {
            "type": "carousel",
            "contents": bubbles,
        },
    }


def send_line_notification(articles: list[dict], target_date: date, dry_run: bool = False) -> bool:
    """LINE Messaging API でトップ3記事を通知する"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")

    if not token or not user_id:
        logger.info("LINE_CHANNEL_ACCESS_TOKEN または LINE_USER_ID が未設定 → LINE通知をスキップ")
        return False

    sorted_articles = sorted(articles, key=lambda a: a.get("score", 0), reverse=True)
    flex_msg = _build_flex_message(sorted_articles, target_date)

    payload = {
        "to": user_id,
        "messages": [flex_msg],
    }

    if dry_run:
        logger.info(f"[dry-run] LINE通知をスキップ: {len(sorted_articles[:3])} 件のメッセージを送信予定")
        return True

    try:
        resp = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            logger.info(f"LINE通知送信完了: トップ{min(len(sorted_articles), 3)}記事")
            return True
        else:
            logger.warning(f"LINE通知失敗: HTTP {resp.status_code} - {resp.text[:200]}")
            return False
    except requests.RequestException as e:
        logger.error(f"LINE通知エラー: {e}")
        return False


# ---------------------------------------------------------------------------
# 2. GitHub Pages 生成（Google Stitch デザイン）
# ---------------------------------------------------------------------------

def _escape_html(text: str) -> str:
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _inline_md(text: str) -> str:
    """インライン Markdown（**bold**・[link](url)・`code`）を HTML に変換する"""
    # Bold
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong class="font-bold text-on-surface">\1</strong>', text)
    # Italic
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    # Code
    text = re.sub(r'`(.+?)`', r'<code class="bg-surface-container-high px-1 rounded font-data-mono text-data-mono text-sm">\1</code>', text)
    # Link
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank" rel="noopener" class="text-primary hover:underline">\1</a>', text)
    return text


def _md_section_to_html(md_text: str) -> str:
    """Markdown テキストを Stitch スタイルの簡易 HTML に変換する"""
    lines = md_text.split("\n")
    html_parts: list[str] = []
    in_table = False
    header_done = False
    in_list = False

    for raw in lines:
        line = raw.rstrip()

        # テーブル行
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not in_table:
                in_table = True
                header_done = False
                html_parts.append('<div class="overflow-x-auto mb-md"><table class="w-full text-left border-collapse">')
                html_parts.append('<thead><tr class="bg-surface-container-low border-b border-outline-variant">')
                for c in cells:
                    html_parts.append(f'<th class="p-sm font-label-sm text-label-sm text-on-surface-variant font-medium">{_inline_md(c)}</th>')
                html_parts.append('</tr></thead>')
                continue
            # セパレータ行 (|---|---|)
            if all(set(c) <= set("-:|") for c in cells if c):
                if not header_done:
                    html_parts.append('<tbody class="divide-y divide-outline-variant">')
                    header_done = True
                continue
            # データ行
            row = '<tr class="hover:bg-surface-container-lowest transition-colors">'
            for c in cells:
                row += f'<td class="p-sm font-body-md text-body-md text-on-surface">{_inline_md(c)}</td>'
            row += '</tr>'
            html_parts.append(row)
            continue
        else:
            if in_table:
                html_parts.append('</tbody></table></div>')
                in_table = False
                header_done = False

        # リスト終端チェック
        if in_list and not (line.startswith("- ") or line.startswith("* ")):
            html_parts.append('</ul>')
            in_list = False

        # 見出し
        if line.startswith("### "):
            html_parts.append(f'<h3 class="font-h3 text-h3 text-on-surface mt-lg mb-sm">{_inline_md(line[4:])}</h3>')
        elif line.startswith("## "):
            html_parts.append(f'<h2 class="font-h2 text-h2 text-on-surface mt-xl mb-md pt-sm border-b border-outline-variant pb-xs">{_inline_md(line[3:])}</h2>')
        elif line.startswith("# "):
            html_parts.append(f'<h1 class="font-h1 text-h1 text-on-background mb-lg">{_inline_md(line[2:])}</h1>')
        # リスト
        elif line.startswith("- ") or line.startswith("* "):
            if not in_list:
                html_parts.append('<ul class="list-disc pl-6 space-y-1 mb-sm">')
                in_list = True
            html_parts.append(f'<li class="font-body-md text-body-md text-on-surface-variant">{_inline_md(line[2:])}</li>')
        # 引用
        elif line.startswith("> "):
            html_parts.append(f'<blockquote class="border-l-4 border-primary/30 pl-md text-on-surface-variant italic my-sm font-body-md text-body-md">{_inline_md(line[2:])}</blockquote>')
        # 区切り線
        elif line.strip() == "---":
            html_parts.append('<hr class="border-outline-variant my-lg">')
        # 空行
        elif line.strip() == "":
            html_parts.append("")
        # 通常段落
        else:
            html_parts.append(f'<p class="font-body-md text-body-md text-on-surface mb-sm">{_inline_md(line)}</p>')

    if in_table:
        html_parts.append('</tbody></table></div>')
    if in_list:
        html_parts.append('</ul>')

    return "\n".join(html_parts)


def _stitch_head(title: str) -> str:
    """Stitch デザイン共通 <head> セクションを返す"""
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8"/>
<meta content="width=device-width, initial-scale=1.0" name="viewport"/>
<title>{_escape_html(title)}</title>
<script src="https://cdn.tailwindcss.com?plugins=forms,container-queries"></script>
<link href="https://fonts.googleapis.com" rel="preconnect"/>
<link crossorigin="" href="https://fonts.gstatic.com" rel="preconnect"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet"/>
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:wght,FILL@100..700,0..1&display=swap" rel="stylesheet"/>
<style>.material-symbols-outlined {{ font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24; }} body {{ line-height: 1.8; }} p, li, td, th {{ line-height: 1.8; }}</style>
<script id="tailwind-config">
tailwind.config = {{
  darkMode: "class",
  theme: {{
    extend: {{
      colors: {{
        "brand-blue": "#4F8EF7",
        "surface-dim": "#d8d9e2", "on-secondary": "#ffffff",
        "secondary-fixed": "#d3e4fe", "on-primary-fixed": "#001a40",
        "surface-container-low": "#f2f3fc", "on-error": "#ffffff",
        "on-tertiary-container": "#fffbff", "tertiary": "#894e00",
        "on-secondary-container": "#54647a", "surface-bright": "#f9f9ff",
        "on-primary-container": "#fefcff", "surface-variant": "#e1e2eb",
        "surface-container-high": "#e7e8f1", "primary": "#0059ba",
        "tertiary-fixed": "#ffdcbf", "on-secondary-fixed-variant": "#38485d",
        "on-primary-fixed-variant": "#004492", "error": "#ba1a1a",
        "outline": "#727784", "primary-container": "#2c72d9",
        "secondary-container": "#d0e1fb", "inverse-surface": "#2e3037",
        "surface-container-lowest": "#ffffff", "inverse-primary": "#acc7ff",
        "surface-container": "#ecedf6", "on-primary": "#ffffff",
        "on-surface": "#191c22", "surface": "#f9f9ff",
        "on-tertiary-fixed": "#2d1600", "outline-variant": "#c2c6d5",
        "inverse-on-surface": "#eff0f9", "on-background": "#191c22",
        "primary-fixed": "#d7e2ff", "on-secondary-fixed": "#0b1c30",
        "on-tertiary-fixed-variant": "#6a3b00", "secondary": "#505f76",
        "surface-tint": "#005bbf", "error-container": "#ffdad6",
        "primary-fixed-dim": "#acc7ff", "on-tertiary": "#ffffff",
        "background": "#f8f7f6", "tertiary-container": "#ac6300",
        "secondary-fixed-dim": "#b7c8e1", "on-surface-variant": "#424753",
        "surface-container-highest": "#e1e2eb", "on-error-container": "#93000a",
        "tertiary-fixed-dim": "#ffb873"
      }},
      borderRadius: {{ DEFAULT: "0.25rem", lg: "0.5rem", xl: "0.75rem", full: "9999px" }},
      spacing: {{ xs: "4px", xl: "32px", sm: "8px", margin: "24px", md: "16px", gutter: "20px", unit: "4px", lg: "24px" }},
      fontFamily: {{
        "body-md": ["Inter", "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Meiryo", "sans-serif"],
        "data-mono": ["Inter", "sans-serif"],
        "h2": ["Inter", "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Meiryo", "sans-serif"],
        "label-sm": ["Inter", "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Meiryo", "sans-serif"],
        "body-lg": ["Inter", "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Meiryo", "sans-serif"],
        "h1": ["Inter", "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Meiryo", "sans-serif"],
        "h3": ["Inter", "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Meiryo", "sans-serif"]
      }},
      fontSize: {{
        "body-md": ["14px", {{"lineHeight":"20px","fontWeight":"400"}}],
        "data-mono": ["14px", {{"lineHeight":"20px","fontWeight":"500"}}],
        "h2": ["24px", {{"lineHeight":"32px","letterSpacing":"-0.01em","fontWeight":"600"}}],
        "label-sm": ["12px", {{"lineHeight":"16px","letterSpacing":"0.02em","fontWeight":"500"}}],
        "body-lg": ["16px", {{"lineHeight":"24px","fontWeight":"400"}}],
        "h1": ["30px", {{"lineHeight":"38px","letterSpacing":"-0.02em","fontWeight":"700"}}],
        "h3": ["20px", {{"lineHeight":"28px","fontWeight":"600"}}]
      }}
    }}
  }}
}}
</script>
</head>"""


def _sidebar_html(active: str = "dashboard", back_prefix: str = "", latest_date=None) -> str:
    """サイドバーナビゲーションを返す。
    active: 'dashboard' | 'daily' | 'weekly' | 'glossary' | 'trends' | 'industry'
    latest_date: 最新日次レポートの date オブジェクト（None の場合は # になる）
    """
    def nav_item(icon: str, label: str, href: str, is_active: bool) -> str:
        if is_active:
            return (f'<a class="flex items-center gap-3 px-4 py-3 text-primary bg-secondary-container/30 '
                    f'font-bold border-r-4 border-primary" href="{href}">'
                    f'<span class="material-symbols-outlined">{icon}</span>'
                    f'<span class="font-body-md text-body-md">{label}</span></a>')
        return (f'<a class="flex items-center gap-3 px-4 py-3 text-on-surface-variant '
                f'hover:bg-surface-container-high transition-colors duration-200 rounded-lg" href="{href}">'
                f'<span class="material-symbols-outlined">{icon}</span>'
                f'<span class="font-body-md text-body-md">{label}</span></a>')

    bp = back_prefix
    index_href    = f"{bp}index.html"
    daily_href    = f"{bp}daily/{latest_date.isoformat()}.html" if latest_date else f"{bp}daily/"
    weekly_href   = f"{bp}weekly.html"
    glossary_href = f"{bp}glossary.html"
    trends_href   = f"{bp}trends.html"
    industry_href = f"{bp}industry.html"

    return f"""<aside class="bg-surface-container-lowest border-r border-outline-variant fixed left-0 top-0 h-full w-64 flex flex-col py-6 z-20 hidden md:flex">
<div class="px-6 mb-8">
  <div class="flex items-center gap-3 mb-1">
    <div class="w-8 h-8 rounded bg-primary flex items-center justify-center text-on-primary">
      <span class="material-symbols-outlined text-[20px]" style="font-variation-settings: 'FILL' 1;">analytics</span>
    </div>
    <span class="font-h2 text-h2 font-bold tracking-tight text-primary">ai-research</span>
  </div>
  <p class="font-label-sm text-label-sm text-on-surface-variant">Analytical Intelligence</p>
</div>
<nav class="flex-1 flex flex-col space-y-1 px-2">
  {nav_item("dashboard",       "ダッシュボード",      index_href,    active=="dashboard")}
  {nav_item("description",     "今日の最新レポート",  daily_href,    active=="daily")}
  {nav_item("calendar_view_week","今週のまとめ",      weekly_href,   active=="weekly")}
  {nav_item("menu_book",       "AI用語図鑑",          glossary_href, active=="glossary")}
  {nav_item("trending_up",     "トレンド分析",        trends_href,   active=="trends")}
  {nav_item("business",        "業界への影響",        industry_href, active=="industry")}
</nav>
<div class="mt-auto pt-4 border-t border-outline-variant px-4">
  <div class="flex items-center gap-2">
    <div class="w-8 h-8 rounded-full bg-surface-variant flex items-center justify-center">
      <span class="material-symbols-outlined text-primary">science</span>
    </div>
    <span class="font-label-sm text-label-sm text-on-surface-variant">devaise-support</span>
  </div>
</div>
</aside>"""


def _score_circle_html(score: float, size: str = "md") -> str:
    """スコアに応じた色のスコアサークルを返す"""
    if score >= 4.0:
        color = "primary"
    elif score >= 3.5:
        color = "tertiary"
    else:
        color = "secondary"
    sz = "w-16 h-16 text-lg" if size == "lg" else "w-12 h-12 text-base"
    return (f'<div class="flex-shrink-0 flex items-center justify-center {sz} rounded-full '
            f'border-[3px] border-{color} bg-{color}/10 shadow-sm">'
            f'<span class="font-bold text-{color}">{score:.1f}</span></div>')


def _badge_html(article: dict) -> str:
    """記事の候補フラグをバッジHTMLで返す"""
    badges = []
    if article.get("is_weekly_candidate"):
        badges.append('<span class="bg-blue-100 text-blue-700 px-2 py-0.5 rounded font-label-sm text-label-sm border border-blue-200">週次候補</span>')
    if article.get("is_monthly_candidate"):
        badges.append('<span class="bg-green-100 text-green-700 px-2 py-0.5 rounded font-label-sm text-label-sm border border-green-200">月次候補</span>')
    if article.get("is_content_candidate"):
        badges.append('<span class="bg-orange-100 text-orange-700 px-2 py-0.5 rounded font-label-sm text-label-sm border border-orange-200">コンテンツ候補</span>')
    if article.get("is_x_post_candidate"):
        badges.append('<span class="bg-purple-100 text-purple-700 px-2 py-0.5 rounded font-label-sm text-label-sm border border-purple-200">X投稿候補</span>')
    if article.get("is_primary_source"):
        badges.append('<span class="bg-gray-100 text-gray-600 px-2 py-0.5 rounded font-label-sm text-label-sm border border-gray-200">一次情報</span>')
    return " ".join(badges) if badges else '<span class="text-on-surface-variant font-label-sm text-label-sm">—</span>'


def _level_badge_html(level: str) -> str:
    """用語のレベルバッジHTMLを返す（初級=緑、中級=黄、上級=赤）"""
    config = {
        "初級": ("bg-green-100 text-green-700 border-green-200", "初級"),
        "中級": ("bg-yellow-100 text-yellow-700 border-yellow-200", "中級"),
        "上級": ("bg-red-100 text-red-700 border-red-200", "上級"),
    }
    cls, label = config.get(level, ("bg-gray-100 text-gray-600 border-gray-200", level or "中級"))
    return f'<span class="{cls} px-2 py-0.5 rounded font-label-sm text-label-sm border">{label}</span>'


def _category_chart_html(articles: list[dict]) -> str:
    """カテゴリ別横棒グラフHTMLを返す"""
    from collections import Counter
    counts = Counter(a.get("category", "その他") for a in articles)
    total = max(sum(counts.values()), 1)
    colors = ["primary", "primary-container", "secondary", "tertiary", "outline"]
    rows = ""
    for i, (cat, cnt) in enumerate(counts.most_common(5)):
        pct = int(cnt / total * 100)
        color = colors[i % len(colors)]
        rows += f"""<div>
  <div class="flex justify-between font-label-sm text-label-sm text-on-surface-variant mb-1.5">
    <span>{_escape_html(cat)}</span>
    <span class="font-data-mono">{pct}%</span>
  </div>
  <div class="w-full bg-surface-variant rounded-full h-2 overflow-hidden">
    <div class="bg-{color} h-2 rounded-full" style="width:{pct}%"></div>
  </div>
</div>"""
    return rows


def generate_daily_html(articles: list[dict], target_date: date) -> str:
    """日次レポートHTML（Stitch デザイン）を生成する"""
    sorted_arts = sorted(articles, key=lambda x: x.get("score", 0), reverse=True)
    weekly_count = sum(1 for a in articles if a.get("is_weekly_candidate"))
    content_count = sum(1 for a in articles if a.get("is_content_candidate"))
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 日付表示（曜日付き）
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekdays[target_date.weekday()]
    date_ja = f"{target_date.year}年{target_date.month}月{target_date.day}日（{wd}）"

    # カテゴリ集計（フィルタ用）
    from collections import Counter
    cats = list(Counter(a.get("category", "その他") for a in articles).keys())

    # カテゴリフィルタ pills
    cat_pills = '<button onclick="filterArticles(\'all\')" id="filter-all" class="px-md py-sm bg-brand-blue text-white rounded-full font-label-sm text-label-sm shadow-sm">すべて</button>'
    for cat in cats[:4]:
        cat_pills += (f'<button onclick="filterArticles(\'{_escape_html(cat)}\')" '
                      f'class="px-md py-sm bg-surface text-on-surface border border-outline-variant rounded-full '
                      f'font-label-sm text-label-sm hover:border-brand-blue hover:text-brand-blue transition-colors">'
                      f'{_escape_html(cat)}</button>')

    # 記事カード
    article_cards = ""
    for a in sorted_arts:
        score = a.get("score", 0)
        title = _escape_html(a.get("title", "（タイトルなし）"))
        url = _escape_html(a.get("url", "#"))
        source = _escape_html(a.get("source_name", ""))
        pub = (a.get("published_at") or "")[:10]
        category = _escape_html(a.get("category", ""))
        lang = a.get("language", "")
        badges = _badge_html(a)
        circle = _score_circle_html(score, "lg")
        card_id = f"card-{_escape_html(a.get('id', ''))}"

        # ★ 表示する要約: summary_ja を優先、エラー文字列/空の場合は summary_raw にフォールバック
        summary_ja = a.get("summary_ja") or ""
        if not summary_ja or (summary_ja.startswith("（") and summary_ja.endswith("）")):
            summary_ja = a.get("summary_raw") or ""
        summary_display = _escape_html(summary_ja)

        # EN→JP バッジ（英語記事のみ表示）
        lang_badge = ('<span class="bg-blue-100 text-blue-700 px-2 py-0.5 rounded-full font-label-sm text-label-sm '
                      'border border-blue-200 flex-shrink-0">EN→JP</span>') if lang == "en" else ""

        # スコア内訳
        details = a.get("score_details", {})
        score_rows = ""
        label_map = {"importance":"重要度","novelty":"新規性","business_value":"ビジネス価値","learning_value":"学習価値","primary_source_score":"一次情報度"}
        for key, label in label_map.items():
            val = details.get(key, 0)
            score_rows += f'<li class="flex justify-between"><span class="text-on-surface-variant">{label}:</span><span class="text-brand-blue font-bold">{val}/5</span></li>'

        # タグ（new_terms が list[dict] の場合も term 文字列を取り出す）
        raw_tags = a.get("tags") or []
        if not raw_tags:
            raw_new_terms = a.get("new_terms") or []
            raw_tags = [
                (t.get("term", "") if isinstance(t, dict) else str(t))
                for t in raw_new_terms
                if (t.get("term", "") if isinstance(t, dict) else str(t))
            ]
        if isinstance(raw_tags, list):
            tag_html = " ".join(f'<span class="bg-surface-variant text-on-surface px-2 py-0.5 rounded font-label-sm text-label-sm">{_escape_html(str(t))}</span>' for t in raw_tags[:6])
        else:
            tag_html = ""

        article_cards += f"""<article class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg hover:shadow-md transition-shadow" data-category="{category}" data-score="{score}">
  <div class="flex gap-lg items-start">
    {circle}
    <div class="flex-1 flex flex-col gap-sm">
      <div class="flex items-start justify-between gap-md">
        <div class="flex-1">
          <!-- カテゴリ + 言語バッジ -->
          <div class="flex items-center gap-xs flex-wrap mb-sm">
            <span class="font-label-sm text-label-sm text-on-surface-variant uppercase tracking-wide">{category}</span>
            {lang_badge}
          </div>
          <!-- ★ 日本語要約をメインコンテンツとして大きく表示 -->
          <p class="font-body-lg text-body-lg text-on-surface leading-relaxed line-clamp-4 mb-md">{summary_display}</p>
          <!-- 元記事タイトル（小さめ）+ リンク -->
          <div class="flex items-start justify-between gap-sm flex-wrap">
            <p class="font-label-sm text-label-sm text-on-surface-variant line-clamp-2 flex-1">{title}</p>
            <a href="{url}" target="_blank" rel="noopener"
               class="flex items-center gap-xs text-primary font-label-sm text-label-sm hover:underline flex-shrink-0 mt-xs">
              記事を読む
              <span class="material-symbols-outlined text-[14px]">open_in_new</span>
            </a>
          </div>
          <p class="font-label-sm text-label-sm text-on-surface-variant mt-xs">{source} · {pub}</p>
        </div>
        <button onclick="toggleDetails('{card_id}')" class="text-on-surface-variant hover:text-brand-blue transition-colors flex-shrink-0 mt-xs">
          <span class="material-symbols-outlined" id="icon-{card_id}">expand_more</span>
        </button>
      </div>
      <div class="flex items-center gap-sm flex-wrap">{badges}</div>
      <div id="{card_id}" class="hidden bg-surface-container-low rounded-lg p-md border border-outline-variant/50 flex flex-col gap-md mt-sm">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-md">
          <div>
            <h4 class="font-label-sm text-label-sm text-on-surface-variant mb-xs">スコア内訳</h4>
            <ul class="font-data-mono text-data-mono text-on-surface space-y-1">{score_rows}</ul>
          </div>
          <div>
            <h4 class="font-label-sm text-label-sm text-on-surface-variant mb-xs">タグ</h4>
            <div class="flex flex-wrap gap-xs">{tag_html}</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</article>"""

    # 新出用語テーブル（list[dict] または list[str] 両フォーマット対応）
    new_terms = []
    for a in articles:
        terms = a.get("new_terms") or []
        if isinstance(terms, list):
            for t in terms:
                term_str = t.get("term", "") if isinstance(t, dict) else str(t)
                if term_str:
                    new_terms.append({"term": term_str, "source": a.get("source_name", ""), "score": a.get("score", 0)})
    term_rows = ""
    seen_terms: set = set()
    for t in new_terms:
        if t["term"] not in seen_terms:
            seen_terms.add(t["term"])
            # 関連度バッジ: スコアが4.0以上→High(red)、3.5以上→Med(amber)、他→Low(green)
            score_val = t.get("score", 0)
            if score_val >= 4.0:
                rel_badge = '<span class="bg-red-500 text-white px-3 py-1 rounded-full text-[11px] font-bold shadow-sm">High</span>'
            elif score_val >= 3.5:
                rel_badge = '<span class="bg-amber-400 text-amber-900 px-3 py-1 rounded-full text-[11px] font-bold shadow-sm">Med</span>'
            else:
                rel_badge = '<span class="bg-green-500 text-white px-3 py-1 rounded-full text-[11px] font-bold shadow-sm">Low</span>'
            term_rows += f"""<tr class="hover:bg-surface-container-low transition-colors">
  <td class="py-sm px-md font-data-mono text-data-mono text-brand-blue font-bold w-36">{_escape_html(t["term"])}</td>
  <td class="py-sm px-md font-body-md text-body-md text-on-surface-variant">{_escape_html(t["source"])}</td>
  <td class="py-sm px-md w-20">{rel_badge}</td>
</tr>"""

    terms_section = ""
    if term_rows:
        terms_section = f"""<section class="mt-xl">
  <div class="flex items-center gap-sm mb-md border-b border-outline-variant pb-sm">
    <span class="material-symbols-outlined text-brand-blue">new_releases</span>
    <h2 class="font-h3 text-h3 text-on-surface">🆕 本日の新出AI用語</h2>
  </div>
  <div class="bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm">
    <table class="w-full text-left border-collapse">
      <thead><tr class="bg-surface-container-low border-b border-outline-variant">
        <th class="py-sm px-md font-label-sm text-label-sm text-on-surface-variant font-medium">用語</th>
        <th class="py-sm px-md font-label-sm text-label-sm text-on-surface-variant font-medium">意味・定義</th>
        <th class="py-sm px-md font-label-sm text-label-sm text-on-surface-variant font-medium w-24">関連度</th>
      </tr></thead>
      <tbody class="divide-y divide-outline-variant">{term_rows}</tbody>
    </table>
  </div>
</section>"""

    head = _stitch_head(f"AI情報まとめ — {date_ja} | ai-research")
    sidebar = _sidebar_html(active="daily", back_prefix="../", latest_date=target_date)

    return f"""{head}
<body class="bg-background text-on-background min-h-screen flex">
{sidebar}
<main class="flex-1 md:ml-64 flex flex-col min-h-screen bg-surface w-full">
<header class="bg-surface-container-lowest border-b border-outline-variant flex justify-between items-center w-full h-16 px-margin sticky top-0 z-40">
  <div class="flex items-center gap-md">
    <a class="flex items-center gap-xs text-on-surface-variant hover:text-brand-blue transition-colors" href="../index.html">
      <span class="material-symbols-outlined text-[20px]">arrow_back</span>
      <span class="font-label-sm text-label-sm">ダッシュボードへ</span>
    </a>
    <div class="h-6 w-px bg-outline-variant mx-sm"></div>
    <h2 class="font-h3 text-h3 font-bold text-on-surface hidden sm:block">📰 AI情報まとめ — {date_ja}</h2>
  </div>
  <span class="font-label-sm text-label-sm text-on-surface-variant hidden lg:inline">更新: {now_str}</span>
</header>
<div class="bg-surface-container-lowest/90 backdrop-blur-sm border-b border-outline-variant sticky top-16 z-30 px-margin py-md flex flex-wrap items-center justify-between gap-md">
  <div class="flex items-center gap-sm flex-wrap">
    {cat_pills}
  </div>
  <div class="flex items-center gap-sm">
    <span class="font-label-sm text-label-sm text-on-surface-variant">スコア:</span>
    <select onchange="filterByScore(this.value)" class="bg-surface border border-outline-variant text-on-surface font-body-md text-body-md rounded-md px-sm py-xs outline-none">
      <option value="0">すべて</option>
      <option value="4.0">4.0以上</option>
      <option value="3.5">3.5以上</option>
      <option value="3.0">3.0以上</option>
    </select>
    <span id="article-count" class="font-label-sm text-label-sm text-on-surface-variant">{len(articles)}件表示中</span>
  </div>
</div>
<div class="p-margin flex flex-col gap-xl max-w-5xl mx-auto w-full">
  <p class="font-body-md text-body-md text-on-surface-variant">
    収集{len(articles)}件 · 週次候補{weekly_count}件 · コンテンツ候補{content_count}件
  </p>
  <section id="articles-list" class="flex flex-col gap-lg">
    {article_cards}
  </section>
  {terms_section}
  <footer class="mt-xl pt-md border-t border-outline-variant font-label-sm text-label-sm text-on-surface-variant">
    ai-research システムにより自動生成 | {now_str}
  </footer>
</div>
</main>
<script>
function toggleDetails(id) {{
  const el = document.getElementById(id);
  const icon = document.getElementById('icon-' + id);
  if (el.classList.contains('hidden')) {{
    el.classList.remove('hidden');
    el.classList.add('flex');
    icon.textContent = 'expand_less';
  }} else {{
    el.classList.add('hidden');
    el.classList.remove('flex');
    icon.textContent = 'expand_more';
  }}
}}
let currentCat = 'all', currentScore = 0;
function applyFilters() {{
  const articles = document.querySelectorAll('#articles-list article');
  let visible = 0;
  articles.forEach(a => {{
    const cat = a.dataset.category;
    const score = parseFloat(a.dataset.score);
    const show = (currentCat === 'all' || cat === currentCat) && score >= currentScore;
    a.style.display = show ? '' : 'none';
    if (show) visible++;
  }});
  document.getElementById('article-count').textContent = visible + '件表示中';
}}
function filterArticles(cat) {{
  currentCat = cat;
  document.querySelectorAll('[onclick^="filterArticles"]').forEach(b => {{
    b.className = b.className.replace('bg-brand-blue text-white', 'bg-surface text-on-surface border border-outline-variant');
  }});
  event.target.className = event.target.className.replace('bg-surface text-on-surface border border-outline-variant', 'bg-brand-blue text-white');
  applyFilters();
}}
function filterByScore(val) {{ currentScore = parseFloat(val); applyFilters(); }}
</script>
</body></html>"""


def generate_index_html(recent: list[tuple[date, list[dict]]]) -> str:
    """GitHub Pages ダッシュボードHTML（Stitch デザイン）を生成する"""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 直近1日分のデータを統計に使う
    today_date, today_articles = recent[0] if recent else (date.today(), [])
    total = len(today_articles)
    weekly_count = sum(1 for a in today_articles if a.get("is_weekly_candidate"))
    content_count = sum(1 for a in today_articles if a.get("is_content_candidate"))
    new_terms_all: list[str] = []
    for a in today_articles:
        terms = a.get("new_terms") or []
        if isinstance(terms, list):
            for t in terms:
                s = t.get("term", "") if isinstance(t, dict) else str(t)
                if s:
                    new_terms_all.append(s)
    new_terms_unique = list(dict.fromkeys(new_terms_all))  # 重複除去・順序保持

    # KPI カード（Stitch v2: アイコン付き正方形バッジスタイル）
    kpi_cards = f"""<section class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-gutter mb-gutter">
  <div class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg flex flex-col justify-between hover:bg-surface-container-low transition-colors duration-200">
    <div class="flex justify-between items-start mb-4">
      <span class="font-label-sm text-label-sm text-on-surface-variant">本日の収集記事</span>
      <div class="w-8 h-8 rounded bg-primary/10 flex items-center justify-center text-primary">
        <span class="material-symbols-outlined text-[18px]">article</span>
      </div>
    </div>
    <span class="font-h1 text-h1 text-on-background">{total}</span>
  </div>
  <div class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg flex flex-col justify-between hover:bg-surface-container-low transition-colors duration-200">
    <div class="flex justify-between items-start mb-4">
      <span class="font-label-sm text-label-sm text-on-surface-variant">週次候補</span>
      <div class="w-8 h-8 rounded bg-primary/10 flex items-center justify-center text-primary">
        <span class="material-symbols-outlined text-[18px]">star</span>
      </div>
    </div>
    <span class="font-h1 text-h1 text-on-background">{weekly_count}</span>
  </div>
  <div class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg flex flex-col justify-between hover:bg-surface-container-low transition-colors duration-200">
    <div class="flex justify-between items-start mb-4">
      <span class="font-label-sm text-label-sm text-on-surface-variant">コンテンツ候補</span>
      <div class="w-8 h-8 rounded bg-primary/10 flex items-center justify-center text-primary">
        <span class="material-symbols-outlined text-[18px]">lightbulb</span>
      </div>
    </div>
    <span class="font-h1 text-h1 text-on-background">{content_count}</span>
  </div>
  <div class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg flex flex-col justify-between hover:bg-surface-container-low transition-colors duration-200">
    <div class="flex justify-between items-start mb-4">
      <span class="font-label-sm text-label-sm text-on-surface-variant">新出AI用語</span>
      <div class="w-8 h-8 rounded bg-primary/10 flex items-center justify-center text-primary">
        <span class="material-symbols-outlined text-[18px]">new_releases</span>
      </div>
    </div>
    <span class="font-h1 text-h1 text-on-background">{len(new_terms_unique)}</span>
  </div>
</section>"""

    # トップ記事リスト行（Stitch v2: inline score pill スタイル）
    top_articles_html = ""
    for a in sorted(today_articles, key=lambda x: x.get("score", 0), reverse=True)[:5]:
        score = a.get("score", 0)
        title = _escape_html(a.get("title", "（タイトルなし）"))
        url = _escape_html(a.get("url", "#"))
        source = _escape_html(a.get("source_name", ""))
        pub = (a.get("published_at") or "")[:10]
        category = _escape_html(a.get("category", ""))
        summary = _escape_html((a.get("summary_raw") or "")[:120])
        top_articles_html += f"""<div class="p-lg hover:bg-surface-container-low transition-colors duration-150 group">
  <div class="flex justify-between items-start gap-4 mb-2">
    <div class="flex-1">
      <div class="flex items-center gap-2 mb-2">
        <span class="bg-primary/10 text-primary font-data-mono text-label-sm px-2 py-0.5 rounded flex items-center gap-1">
          <span class="material-symbols-outlined text-[14px]">psychology</span>{score:.1f}
        </span>
        <span class="font-label-sm text-label-sm text-on-surface-variant uppercase tracking-wider">{category}</span>
        <span class="w-1 h-1 rounded-full bg-outline-variant"></span>
        <span class="font-label-sm text-label-sm text-on-surface-variant">{pub}</span>
      </div>
      <h3 class="font-h3 text-body-lg font-semibold text-on-background group-hover:text-primary transition-colors">
        <a href="{url}" target="_blank" rel="noopener">{title}</a>
      </h3>
    </div>
  </div>
  <p class="font-body-md text-body-md text-on-surface-variant line-clamp-2 w-11/12">{summary}</p>
</div>"""

    # カテゴリグラフ
    chart_rows = _category_chart_html(today_articles)

    # 新出用語チップ（Stitch v2: hover付き rounded-full スタイル）
    term_chips = " ".join(
        f'<span class="bg-surface-container-high text-on-surface-variant border border-outline-variant font-label-sm text-label-sm px-3 py-1.5 rounded-full cursor-pointer hover:bg-primary/10 hover:text-primary hover:border-primary/30 transition-colors">{_escape_html(t)}</span>'
        for t in new_terms_unique[:10]
    ) or '<span class="font-body-md text-body-md text-on-surface-variant">（本日の新出用語なし）</span>'

    # 直近レポート テーブル
    timeline_rows = ""
    for d, arts in recent[:7]:
        sorted_arts = sorted(arts, key=lambda x: x.get("score", 0), reverse=True)
        top = sorted_arts[0] if sorted_arts else None
        top_title = _escape_html((top.get("title", "") if top else "")[:40])
        top_url = _escape_html(top.get("url", "#") if top else "#")
        weekly = sum(1 for a in arts if a.get("is_weekly_candidate"))
        monthly = sum(1 for a in arts if a.get("is_monthly_candidate"))
        daily_link = f'daily/{d.isoformat()}.html'
        timeline_rows += f"""<tr class="hover:bg-surface-container-lowest transition-colors">
  <td class="p-md font-data-mono text-data-mono">
    <a href="{daily_link}" class="text-primary hover:underline">{d.isoformat()}</a>
  </td>
  <td class="p-md text-on-surface">{len(arts)}件</td>
  <td class="p-md text-on-surface">{weekly}件</td>
  <td class="p-md text-on-surface">{monthly}件</td>
  <td class="p-md text-on-surface-variant font-body-md text-body-md">
    <a href="{top_url}" target="_blank" rel="noopener" class="hover:text-primary transition-colors">{top_title}{"…" if top_title else ""}</a>
  </td>
</tr>"""

    head = _stitch_head("AI Research JP - Dashboard | ai-research")
    latest_d = recent[0][0] if recent else None
    sidebar = _sidebar_html(active="dashboard", back_prefix="", latest_date=latest_d)

    return f"""{head}
<body class="bg-background text-on-background font-body-md text-body-md antialiased overflow-hidden flex h-screen">
{sidebar}
<div class="flex-1 ml-64 flex flex-col relative min-h-screen">
<header class="bg-surface-bright fixed top-0 right-0 w-[calc(100%-16rem)] z-10 border-b border-outline-variant flex justify-between items-center px-6 h-16">
  <h1 class="font-h3 text-h3 text-on-background">Dashboard</h1>
  <div class="flex items-center gap-4">
    <span class="font-label-sm text-label-sm text-on-surface-variant">最終更新: {now_str}</span>
    <div class="flex items-center gap-2">
      <button class="w-10 h-10 rounded-full flex items-center justify-center text-on-surface-variant hover:text-primary hover:bg-surface-container-high">
        <span class="material-symbols-outlined">refresh</span>
      </button>
      <button class="w-10 h-10 rounded-full flex items-center justify-center text-on-surface-variant hover:text-primary hover:bg-surface-container-high">
        <span class="material-symbols-outlined">notifications</span>
      </button>
    </div>
  </div>
</header>
<main class="flex-1 mt-16 p-margin overflow-y-auto">
  {kpi_cards}
  <div class="grid grid-cols-1 lg:grid-cols-12 gap-gutter">
    <div class="lg:col-span-8 flex flex-col gap-gutter">
      <div class="bg-surface-container-lowest border border-outline-variant rounded-lg p-0 overflow-hidden">
        <div class="p-lg border-b border-outline-variant flex justify-between items-center bg-surface-bright">
          <h2 class="font-h3 text-h3 text-on-background">Today's Top Articles</h2>
          <a href="daily/{today_date.isoformat()}.html" class="font-label-sm text-label-sm text-primary hover:underline">全{total}件を見る</a>
        </div>
        <div class="divide-y divide-outline-variant">{top_articles_html}</div>
      </div>
    </div>
    <div class="lg:col-span-4 flex flex-col gap-gutter">
      <div class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg">
        <h2 class="font-h3 text-h3 text-on-background mb-6">Category Breakdown</h2>
        <div class="space-y-4">{chart_rows}</div>
      </div>
      <div class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg flex-1">
        <div class="flex justify-between items-center mb-6">
          <h2 class="font-h3 text-h3 text-on-background">Recent New Terms</h2>
          <span class="material-symbols-outlined text-outline">local_offer</span>
        </div>
        <div class="flex flex-wrap gap-2">{term_chips}</div>
      </div>
    </div>
  </div>
  <section class="mt-gutter">
    <h2 class="font-h3 text-h3 text-on-background mb-4">📅 直近のレポート履歴</h2>
    <div class="bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden">
      <table class="w-full text-left border-collapse">
        <thead><tr class="bg-surface-bright border-b border-outline-variant">
          <th class="p-md font-label-sm text-label-sm text-on-surface-variant font-medium">日付</th>
          <th class="p-md font-label-sm text-label-sm text-on-surface-variant font-medium">収集</th>
          <th class="p-md font-label-sm text-label-sm text-on-surface-variant font-medium">週次候補</th>
          <th class="p-md font-label-sm text-label-sm text-on-surface-variant font-medium">月次候補</th>
          <th class="p-md font-label-sm text-label-sm text-on-surface-variant font-medium">トップ記事</th>
        </tr></thead>
        <tbody class="font-body-md text-body-md text-on-surface divide-y divide-outline-variant">
          {timeline_rows}
        </tbody>
      </table>
    </div>
  </section>
  <footer class="mt-gutter pt-md border-t border-outline-variant font-label-sm text-label-sm text-on-surface-variant">
    ai-research システムにより自動生成 | {now_str}
  </footer>
</main>
</div>
</body></html>"""


def _page_shell(title: str, icon: str, active: str, content_html: str, latest_date=None) -> str:
    """全ページ共通シェル（サイドバー + ヘッダー + コンテンツ）"""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    head = _stitch_head(f"{title} | ai-research")
    sidebar = _sidebar_html(active=active, back_prefix="", latest_date=latest_date)
    return f"""{head}
<body class="bg-background text-on-background min-h-screen flex">
{sidebar}
<main class="flex-1 md:ml-64 flex flex-col min-h-screen bg-surface w-full">
<header class="bg-surface-container-lowest border-b border-outline-variant flex items-center gap-md w-full h-16 px-margin sticky top-0 z-40">
  <div class="w-8 h-8 rounded bg-primary/10 flex items-center justify-center text-primary flex-shrink-0">
    <span class="material-symbols-outlined text-[20px]">{icon}</span>
  </div>
  <h1 class="font-h3 text-h3 font-bold text-on-surface">{_escape_html(title)}</h1>
  <span class="ml-auto font-label-sm text-label-sm text-on-surface-variant hidden lg:inline">更新: {now_str}</span>
</header>
<div class="p-margin flex flex-col gap-xl max-w-5xl mx-auto w-full">
{content_html}
  <footer class="mt-xl pt-md border-t border-outline-variant font-label-sm text-label-sm text-on-surface-variant">
    ai-research システムにより自動生成 | {now_str}
  </footer>
</div>
</main>
</body></html>"""


def generate_weekly_html(latest_date=None) -> str:
    """今週のまとめページ HTML を生成する（weekly/*.md を読み込む）"""
    weekly_dir = ROOT / "weekly"
    md_files = sorted(weekly_dir.glob("*.md"), reverse=True) if weekly_dir.exists() else []

    if not md_files:
        content = '<p class="font-body-md text-body-md text-on-surface-variant">週次レポートはまだ生成されていません。</p>'
        return _page_shell("今週のまとめ", "calendar_view_week", "weekly", content, latest_date)

    # 最新ファイルを全文表示
    latest_file = md_files[0]
    latest_md = latest_file.read_text(encoding="utf-8")
    latest_html = _md_section_to_html(latest_md)

    # 過去レポートリスト
    past_links = ""
    for f in md_files[1:]:
        week_label = f.stem  # 例: 2026-W19
        past_links += (f'<li><a href="#" class="text-primary hover:underline font-body-md text-body-md">'
                       f'{week_label}</a></li>\n')
    past_section = ""
    if past_links:
        past_section = f"""<section class="mt-xl">
  <h2 class="font-h2 text-h2 text-on-surface mb-md border-b border-outline-variant pb-xs">📂 過去のレポート</h2>
  <ul class="list-disc pl-6 space-y-1">{past_links}</ul>
</section>"""

    content = f"""<section class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg">
{latest_html}
</section>
{past_section}"""
    return _page_shell("今週のまとめ", "calendar_view_week", "weekly", content, latest_date)


def generate_glossary_html(latest_date=None) -> str:
    """AI用語図鑑 HTML を生成する（terms/glossary.md を読み込む）"""
    import json as _json

    glossary_path = ROOT / "terms" / "glossary.md"
    if not glossary_path.exists():
        content = '<p class="font-body-md text-body-md text-on-surface-variant">用語集がまだ作成されていません。</p>'
        return _page_shell("AI用語図鑑", "menu_book", "glossary", content, latest_date)

    raw_md = glossary_path.read_text(encoding="utf-8")

    def _url_from_md_link(s: str) -> str:
        """[title](url) → url を抽出"""
        m = re.search(r'\(([^)]+)\)', s)
        return m.group(1) if m else ""

    # `## 用語名` セクションを解析してカード形式で表示
    sections = re.split(r'\n(?=## )', raw_md)
    term_cards = ""
    term_count = 0
    glossary_rows: list[dict] = []  # CSV出力用

    for sec in sections:
        lines = sec.strip().split("\n")
        if not lines:
            continue
        heading = lines[0]
        if not heading.startswith("## "):
            continue
        term_name = heading[3:].strip()
        if term_name in ("凡例", "用語名"):
            continue
        body_lines = lines[1:]

        # フィールド抽出（レベル追加）
        init_date = ""
        source_link = ""
        definition = ""
        level = "中級"  # 旧エントリのデフォルト
        for bl in body_lines:
            bl = bl.strip()
            if bl.startswith("- 初出日:"):
                init_date = bl[6:].strip()
            elif bl.startswith("- レベル:"):
                level = bl[6:].strip()
            elif bl.startswith("- 出典:"):
                source_link = bl[5:].strip()
            elif bl.startswith("- 定義:"):
                definition = bl[5:].strip()
            elif bl and not bl.startswith("-"):
                if not definition:
                    definition = bl

        # 「（要追記）」はブランク扱い
        if definition in ("（要追記）", "（要追記）"):
            definition = ""

        level_badge = _level_badge_html(level)
        def_html = _inline_md(_escape_html(definition)) if definition else '<em class="text-on-surface-variant/60">定義未登録</em>'

        term_cards += f"""<div class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg hover:shadow-md transition-shadow" data-level="{_escape_html(level)}">
  <div class="flex items-start justify-between gap-md mb-sm">
    <div class="flex items-center gap-xs flex-wrap">
      <h3 class="font-h3 text-h3 text-primary font-bold">{_escape_html(term_name)}</h3>
      {level_badge}
    </div>
    <span class="font-label-sm text-label-sm text-on-surface-variant bg-surface-container px-2 py-0.5 rounded-full flex-shrink-0">{_escape_html(init_date)}</span>
  </div>
  <p class="font-body-md text-body-md text-on-surface mb-sm leading-relaxed">{def_html}</p>
  {"<p class='font-label-sm text-label-sm text-on-surface-variant'>" + _inline_md(source_link) + "</p>" if source_link else ""}
</div>"""
        glossary_rows.append({
            "term": term_name,
            "level": level,
            "definition": definition,
            "date": init_date,
            "url": _url_from_md_link(source_link),
        })
        term_count += 1

    if not term_cards:
        term_cards = '<p class="font-body-md text-body-md text-on-surface-variant">用語はまだ登録されていません。</p>'

    glossary_data_json = _json.dumps(glossary_rows, ensure_ascii=False)

    content = f"""
<div class="flex flex-col gap-md mb-lg">
  <div class="flex items-center justify-between gap-md flex-wrap">
    <p class="font-body-md text-body-md text-on-surface-variant">全 <strong id="glossary-count">{term_count}</strong> 用語</p>
    <div class="flex items-center gap-sm flex-wrap">
      <input type="text" id="glossary-search" placeholder="用語を検索..."
        oninput="filterGlossary()"
        class="bg-surface border border-outline-variant text-on-surface font-body-md text-body-md rounded-md px-md py-sm outline-none w-48 focus:border-primary">
      <button onclick="downloadCSV()"
        class="flex items-center gap-xs px-md py-sm bg-primary text-on-primary rounded-md font-label-sm text-label-sm hover:opacity-90 transition-opacity">
        <span class="material-symbols-outlined text-[16px]">download</span>
        CSVダウンロード
      </button>
    </div>
  </div>
  <div class="flex items-center gap-sm flex-wrap">
    <span class="font-label-sm text-label-sm text-on-surface-variant">レベル:</span>
    <button onclick="filterByLevel('all')" id="level-all"
      class="px-md py-xs bg-primary text-on-primary rounded-full font-label-sm text-label-sm transition-all">すべて</button>
    <button onclick="filterByLevel('初級')" id="level-初級"
      class="px-md py-xs bg-green-100 text-green-700 border border-green-200 rounded-full font-label-sm text-label-sm hover:bg-green-200 transition-all">初級</button>
    <button onclick="filterByLevel('中級')" id="level-中級"
      class="px-md py-xs bg-yellow-100 text-yellow-700 border border-yellow-200 rounded-full font-label-sm text-label-sm hover:bg-yellow-200 transition-all">中級</button>
    <button onclick="filterByLevel('上級')" id="level-上級"
      class="px-md py-xs bg-red-100 text-red-700 border border-red-200 rounded-full font-label-sm text-label-sm hover:bg-red-200 transition-all">上級</button>
  </div>
</div>
<div id="glossary-grid" class="grid grid-cols-1 md:grid-cols-2 gap-gutter">
{term_cards}
</div>
<script>
const glossaryData = {glossary_data_json};
let currentLevel = 'all';

function filterGlossary() {{
  const q = document.getElementById('glossary-search').value.toLowerCase();
  const cards = document.querySelectorAll('#glossary-grid > div');
  let visible = 0;
  cards.forEach(c => {{
    const matchText = c.textContent.toLowerCase().includes(q);
    const matchLevel = currentLevel === 'all' || c.dataset.level === currentLevel;
    const show = matchText && matchLevel;
    c.style.display = show ? '' : 'none';
    if (show) visible++;
  }});
  document.getElementById('glossary-count').textContent = visible;
}}

function filterByLevel(level) {{
  currentLevel = level;
  ['all','初級','中級','上級'].forEach(l => {{
    const btn = document.getElementById('level-' + l);
    if (!btn) return;
    if (l === level) {{
      btn.classList.add('ring-2', 'ring-offset-1', 'ring-primary');
    }} else {{
      btn.classList.remove('ring-2', 'ring-offset-1', 'ring-primary');
    }}
  }});
  filterGlossary();
}}

function downloadCSV() {{
  const BOM = '﻿';
  const header = '用語,レベル,定義,初出日,出典URL\n';
  const esc = s => '"' + (s || '').replace(/"/g, '""') + '"';
  const rows = glossaryData.map(d =>
    [esc(d.term), esc(d.level), esc(d.definition), esc(d.date), esc(d.url)].join(',')
  ).join('\n');
  const blob = new Blob([BOM + header + rows], {{type: 'text/csv;charset=utf-8;'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'ai-glossary.csv';
  a.click();
}}
</script>"""
    return _page_shell("AI用語図鑑", "menu_book", "glossary", content, latest_date)


def generate_trends_html(latest_date=None) -> str:
    """トレンド分析ページ HTML を生成する（knowledge/trends/*.md を読み込む）"""
    trends_dir = ROOT / "knowledge" / "trends"
    rising_path = trends_dir / "rising.md"
    declining_path = trends_dir / "declining.md"

    rising_html = ""
    declining_html = ""

    if rising_path.exists():
        rising_html = _md_section_to_html(rising_path.read_text(encoding="utf-8"))
    if declining_path.exists():
        declining_html = _md_section_to_html(declining_path.read_text(encoding="utf-8"))

    content = f"""<div class="grid grid-cols-1 lg:grid-cols-2 gap-gutter">
  <section class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg">
    <div class="flex items-center gap-sm mb-md pb-sm border-b border-outline-variant">
      <span class="material-symbols-outlined text-primary">trending_up</span>
      <h2 class="font-h3 text-h3 text-on-surface font-bold">急上昇トピック</h2>
    </div>
    {rising_html or '<p class="font-body-md text-body-md text-on-surface-variant">データ蓄積中（2週以上で検出開始）</p>'}
  </section>
  <section class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg">
    <div class="flex items-center gap-sm mb-md pb-sm border-b border-outline-variant">
      <span class="material-symbols-outlined text-tertiary">trending_down</span>
      <h2 class="font-h3 text-h3 text-on-surface font-bold">衰退トレンド</h2>
    </div>
    {declining_html or '<p class="font-body-md text-body-md text-on-surface-variant">データ蓄積中（2週以上で検出開始）</p>'}
  </section>
</div>"""
    return _page_shell("トレンド分析", "trending_up", "trends", content, latest_date)


def generate_industry_html(latest_date=None) -> str:
    """業界への影響ページ HTML を生成する（knowledge/industry-impact/*.md を読み込む）"""
    industry_dir = ROOT / "knowledge" / "industry-impact"
    industry_labels = {
        "beauty": ("美容・サロン", "face"),
        "real-estate": ("不動産", "home"),
        "healthcare": ("医療・ヘルスケア", "medical_services"),
        "finance": ("金融・ファイナンス", "account_balance"),
    }

    tabs_html = ""
    panels_html = ""
    for i, (stem, (label, icon)) in enumerate(industry_labels.items()):
        md_path = (industry_dir / f"{stem}.md") if industry_dir.exists() else None
        if md_path and md_path.exists():
            panel_content = _md_section_to_html(md_path.read_text(encoding="utf-8"))
        else:
            panel_content = f'<p class="font-body-md text-body-md text-on-surface-variant">{label}のデータはまだ蓄積されていません。</p>'

        active_tab = "text-primary border-b-2 border-primary" if i == 0 else "text-on-surface-variant hover:text-on-surface"
        tabs_html += (f'<button onclick="switchTab(\'{stem}\')" id="tab-{stem}" '
                      f'class="flex items-center gap-xs px-lg py-sm font-label-sm text-label-sm transition-colors {active_tab}">'
                      f'<span class="material-symbols-outlined text-[18px]">{icon}</span>{_escape_html(label)}</button>')

        display = "" if i == 0 else "hidden"
        panels_html += f'<div id="panel-{stem}" class="{display}">{panel_content}</div>'

    content = f"""<div class="bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden">
  <div class="flex border-b border-outline-variant overflow-x-auto">{tabs_html}</div>
  <div class="p-lg">{panels_html}</div>
</div>
<script>
function switchTab(id) {{
  document.querySelectorAll('[id^="panel-"]').forEach(p => p.classList.add('hidden'));
  document.querySelectorAll('[id^="tab-"]').forEach(t => {{
    t.classList.remove('text-primary','border-b-2','border-primary');
    t.classList.add('text-on-surface-variant');
  }});
  document.getElementById('panel-' + id).classList.remove('hidden');
  const tb = document.getElementById('tab-' + id);
  tb.classList.remove('text-on-surface-variant');
  tb.classList.add('text-primary','border-b-2','border-primary');
}}
</script>"""
    return _page_shell("業界への影響", "business", "industry", content, latest_date)


def publish_github_pages(target_date: date, dry_run: bool = False) -> bool:
    """GitHub Pages 用の HTML ファイルを全ページ生成する"""
    articles = load_articles_for_date(target_date)
    if not articles:
        logger.warning(f"GitHub Pages: {target_date} のスコアデータなし")
        return False

    # 最新記事データ（サイドバーの今日リンク用）
    recent = load_recent_articles(days=10)
    latest_d = recent[0][0] if recent else target_date

    # 日次HTML
    daily_html = generate_daily_html(articles, target_date)
    daily_path = DOCS_DIR / "daily" / f"{target_date.isoformat()}.html"

    # インデックスHTML
    index_html = generate_index_html(recent)
    index_path = DOCS_DIR / "index.html"

    # サブページHTML（サイドバーリンク先）
    weekly_html   = generate_weekly_html(latest_date=latest_d)
    glossary_html = generate_glossary_html(latest_date=latest_d)
    trends_html   = generate_trends_html(latest_date=latest_d)
    industry_html = generate_industry_html(latest_date=latest_d)

    if dry_run:
        logger.info(f"[dry-run] GitHub Pages 生成スキップ: {daily_path.name} + 5ページ")
        return True

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "daily").mkdir(parents=True, exist_ok=True)

    pages = [
        (daily_path, daily_html),
        (index_path, index_html),
        (DOCS_DIR / "weekly.html",   weekly_html),
        (DOCS_DIR / "glossary.html", glossary_html),
        (DOCS_DIR / "trends.html",   trends_html),
        (DOCS_DIR / "industry.html", industry_html),
    ]
    for path, html in pages:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)

    generated = ", ".join(p.name for p, _ in pages)
    logger.info(f"GitHub Pages 生成完了: {generated}")
    return True


# ---------------------------------------------------------------------------
# 3. X（Twitter）自動投稿
# ---------------------------------------------------------------------------

def post_to_x(articles: list[dict], target_date: date, dry_run: bool = False) -> int:
    """
    スコア4.5以上（x_post_candidate）の記事を X に投稿する。
    APIキー未設定時はファイル生成のみ行い投稿はスキップ。

    Returns:
        投稿した件数
    """
    candidates = [a for a in articles if a.get("is_x_post_candidate", False)]
    if not candidates:
        logger.info("X投稿: x_post_candidate（4.5点以上）の記事なし")
        return 0

    # 1日最大3件に制限
    candidates = sorted(candidates, key=lambda a: a.get("score", 0), reverse=True)[:3]

    x_api_key = os.environ.get("X_API_KEY", "")
    x_api_secret = os.environ.get("X_API_SECRET", "")
    x_access_token = os.environ.get("X_ACCESS_TOKEN", "")
    x_access_token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET", "")
    can_post = all([x_api_key, x_api_secret, x_access_token, x_access_token_secret])

    if not can_post:
        logger.info("X APIキー未設定 → 投稿文ファイル生成のみ行います")

    posted = 0
    X_POSTS_DIR.mkdir(parents=True, exist_ok=True)

    # tweepy client（APIキーある場合のみ初期化）
    x_client = None
    if can_post and not dry_run:
        try:
            import tweepy
            x_client = tweepy.Client(
                consumer_key=x_api_key,
                consumer_secret=x_api_secret,
                access_token=x_access_token,
                access_token_secret=x_access_token_secret,
            )
            logger.info("X API: tweepy クライアント初期化完了")
        except ImportError:
            logger.warning("tweepy がインストールされていません: pip install tweepy>=4.14.0")
        except Exception as e:
            logger.warning(f"X APIクライアント初期化エラー: {e}")

    for article in candidates:
        article_id = article.get("id", "unknown")
        url = article.get("url", "")
        title = article.get("title", "")

        # content/x_posts/ から投稿文ファイルを探す（あれば使用）
        post_file = X_POSTS_DIR / f"{target_date.isoformat()}-{article_id}.txt"
        if post_file.exists():
            with open(post_file, encoding="utf-8") as f:
                post_text = f.read().strip()
        else:
            # 簡易テンプレート生成
            short_title = title[:40] + "..." if len(title) > 40 else title
            post_text = f"【AI速報】{short_title}\n{url}\n#生成AI #AIビジネス #DX"

        if dry_run:
            logger.info(f"[dry-run] X投稿スキップ: {post_text[:60]}...")
            posted += 1
            continue

        # ファイルに保存（投稿前バックアップ）
        if not post_file.exists():
            with open(post_file, "w", encoding="utf-8") as f:
                f.write(post_text)

        # 実際の投稿
        if x_client:
            try:
                x_client.create_tweet(text=post_text)
                logger.info(f"X投稿完了: {post_text[:60]}...")
                posted += 1
                import time as _time
                _time.sleep(2)  # API レート制限対策
            except Exception as e:
                logger.warning(f"X投稿エラー: {e}")
        else:
            logger.info(f"X投稿ファイル生成済み: {post_file.name}")
            posted += 1

    return posted


# ---------------------------------------------------------------------------
# テスト通知
# ---------------------------------------------------------------------------

def test_line() -> None:
    """LINE通知のテスト送信（本文固定）"""
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    user_id = os.environ.get("LINE_USER_ID", "")
    if not token or not user_id:
        logger.error("LINE_CHANNEL_ACCESS_TOKEN / LINE_USER_ID が未設定です")
        return

    test_article = {
        "title": "【テスト】ai-research システム LINE通知テスト",
        "source_name": "ai-research",
        "score": 4.5,
        "url": GITHUB_PAGES_BASE,
        "category": "テスト",
        "summary_raw": "LINE通知が正常に動作しています。このメッセージはテスト用です。",
    }
    result = send_line_notification([test_article], date.today())
    if result:
        logger.info("LINE テスト通知送信成功")
    else:
        logger.error("LINE テスト通知送信失敗")


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def publish_all(target_date: date, dry_run: bool = False) -> dict:
    """
    指定日の全配信処理を実行する。

    Returns:
        結果サマリー dict
    """
    articles = load_articles_for_date(target_date)
    summary = {"date": str(target_date), "articles": len(articles), "line": False, "pages": False, "x_posted": 0}

    if not articles:
        logger.warning(f"{target_date} のスコアデータが存在しません。配信をスキップします。")
        return summary

    # GitHub Pages
    summary["pages"] = publish_github_pages(target_date, dry_run=dry_run)

    # LINE通知
    summary["line"] = send_line_notification(articles, target_date, dry_run=dry_run)

    # X投稿
    summary["x_posted"] = post_to_x(articles, target_date, dry_run=dry_run)

    logger.info(
        f"[完了] 配信処理完了: LINE={summary['line']} / "
        f"Pages={summary['pages']} / X投稿={summary['x_posted']}件"
    )
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="AI情報 配信モジュール")
    parser.add_argument("--date", default=str(date.today()), help="対象日 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="実際の送信・投稿をせずに確認のみ")
    parser.add_argument("--test", action="store_true", help="LINE通知テストを実行")
    parser.add_argument("--pages-only", action="store_true", help="GitHub Pages 生成のみ")
    parser.add_argument("--line-only", action="store_true", help="LINE通知のみ")
    parser.add_argument("--x-only", action="store_true", help="X投稿のみ")
    args = parser.parse_args()

    if args.test:
        test_line()
        return

    try:
        target_date = date.fromisoformat(args.date)
    except ValueError:
        logger.error(f"日付の形式が不正です: {args.date}")
        sys.exit(1)

    articles = load_articles_for_date(target_date)

    if args.pages_only:
        publish_github_pages(target_date, dry_run=args.dry_run)
    elif args.line_only:
        send_line_notification(articles, target_date, dry_run=args.dry_run)
    elif args.x_only:
        post_to_x(articles, target_date, dry_run=args.dry_run)
    else:
        publish_all(target_date, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
