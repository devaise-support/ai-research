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
<style>.material-symbols-outlined {{ font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24; }}</style>
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
        "background": "#f9f9ff", "tertiary-container": "#ac6300",
        "secondary-fixed-dim": "#b7c8e1", "on-surface-variant": "#424753",
        "surface-container-highest": "#e1e2eb", "on-error-container": "#93000a",
        "tertiary-fixed-dim": "#ffb873"
      }},
      borderRadius: {{ DEFAULT: "0.25rem", lg: "0.5rem", xl: "0.75rem", full: "9999px" }},
      spacing: {{ xs: "4px", xl: "32px", sm: "8px", margin: "24px", md: "16px", gutter: "20px", unit: "4px", lg: "24px" }},
      fontFamily: {{
        "body-md": ["Inter","sans-serif"],
        "data-mono": ["Inter","sans-serif"],
        "h2": ["Inter","sans-serif"],
        "label-sm": ["Inter","sans-serif"],
        "body-lg": ["Inter","sans-serif"],
        "h1": ["Inter","sans-serif"],
        "h3": ["Inter","sans-serif"]
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


def _sidebar_html(active: str = "dashboard", back_prefix: str = "") -> str:
    """サイドバーナビゲーションを返す。active='dashboard' or 'daily'"""
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

    index_href = f"{back_prefix}index.html"
    daily_href = "#"
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
  {nav_item("dashboard","ダッシュボード", index_href, active=="dashboard")}
  {nav_item("description","今日の最新レポート", daily_href, active=="daily")}
  {nav_item("calendar_view_week","今週のまとめ","#", False)}
  {nav_item("menu_book","AI用語図鑑","#", False)}
  {nav_item("trending_up","トレンド分析","#", False)}
  {nav_item("business","業界への影響","#", False)}
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
        summary = _escape_html((a.get("summary_raw") or "")[:200])
        badges = _badge_html(a)
        circle = _score_circle_html(score, "lg")
        card_id = f"card-{_escape_html(a.get('id', ''))}"

        # スコア内訳
        details = a.get("score_details", {})
        score_rows = ""
        label_map = {"importance":"重要度","novelty":"新規性","business_value":"ビジネス価値","learning_value":"学習価値","primary_source_score":"一次情報度"}
        for key, label in label_map.items():
            val = details.get(key, 0)
            score_rows += f'<li class="flex justify-between"><span class="text-on-surface-variant">{label}:</span><span class="text-brand-blue font-bold">{val}/5</span></li>'

        # タグ
        tags = a.get("tags") or a.get("new_terms") or []
        if isinstance(tags, list):
            tag_html = " ".join(f'<span class="bg-surface-variant text-on-surface px-2 py-0.5 rounded font-label-sm text-label-sm">{_escape_html(str(t))}</span>' for t in tags[:6])
        else:
            tag_html = ""

        article_cards += f"""<article class="bg-surface-container-lowest border border-outline-variant rounded-lg p-lg hover:shadow-md transition-shadow" data-category="{category}" data-score="{score}">
  <div class="flex gap-lg items-start">
    {circle}
    <div class="flex-1 flex flex-col gap-sm">
      <div class="flex items-start justify-between gap-md">
        <div class="flex-1">
          <h3 class="font-h3 text-h3 text-on-surface mb-xs hover:text-brand-blue transition-colors">
            <a href="{url}" target="_blank" rel="noopener">{title}</a>
          </h3>
          <p class="font-label-sm text-label-sm text-on-surface-variant mb-sm">{source} · {pub} · {category}</p>
          <p class="font-body-md text-body-md text-on-surface-variant line-clamp-3">{summary}</p>
        </div>
        <button onclick="toggleDetails('{card_id}')" class="text-on-surface-variant hover:text-brand-blue transition-colors flex-shrink-0">
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

    # 新出用語テーブル
    new_terms = []
    for a in articles:
        terms = a.get("new_terms") or []
        if isinstance(terms, list):
            for t in terms:
                new_terms.append({"term": str(t), "source": a.get("source_name", ""), "score": a.get("score", 0)})
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
    sidebar = _sidebar_html(active="daily", back_prefix="../")

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
            new_terms_all.extend(str(t) for t in terms)
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
    sidebar = _sidebar_html(active="dashboard", back_prefix="")

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


def publish_github_pages(target_date: date, dry_run: bool = False) -> bool:
    """GitHub Pages 用の HTML ファイルを生成する"""
    articles = load_articles_for_date(target_date)
    if not articles:
        logger.warning(f"GitHub Pages: {target_date} のスコアデータなし")
        return False

    # 日次HTML
    daily_html = generate_daily_html(articles, target_date)
    daily_path = DOCS_DIR / "daily" / f"{target_date.isoformat()}.html"

    # インデックスHTML
    recent = load_recent_articles(days=10)
    index_html = generate_index_html(recent)
    index_path = DOCS_DIR / "index.html"

    if dry_run:
        logger.info(f"[dry-run] GitHub Pages 生成スキップ: {daily_path.name} + index.html")
        return True

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / "daily").mkdir(parents=True, exist_ok=True)

    with open(daily_path, "w", encoding="utf-8") as f:
        f.write(daily_html)
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(index_html)

    logger.info(f"GitHub Pages 生成完了: {daily_path} / {index_path}")
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
