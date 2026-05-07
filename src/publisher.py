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
# 2. GitHub Pages 生成
# ---------------------------------------------------------------------------

_HTML_DAILY_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI情報 {date} | ai-research</title>
  <style>
    body {{ font-family: 'Noto Sans JP', 'Helvetica Neue', Arial, sans-serif;
           max-width: 860px; margin: 0 auto; padding: 16px; color: #333; }}
    h1 {{ font-size: 1.4rem; border-bottom: 2px solid #0066CC; padding-bottom: 8px; }}
    h2 {{ font-size: 1.1rem; margin-top: 24px; }}
    .meta {{ font-size: 0.8rem; color: #888; margin-bottom: 4px; }}
    .score {{ color: #f5a623; font-weight: bold; }}
    .card {{ border: 1px solid #e0e0e0; border-radius: 8px; padding: 14px;
             margin-bottom: 14px; background: #fafafa; }}
    .card h2 {{ margin: 0 0 6px; font-size: 1rem; }}
    .flags {{ font-size: 0.75rem; }}
    .flag-weekly {{ background:#e8f4fd; color:#0066CC; padding:2px 6px;
                   border-radius:4px; margin-right:4px; }}
    .flag-monthly {{ background:#e8fdf0; color:#28a745; padding:2px 6px;
                    border-radius:4px; margin-right:4px; }}
    .flag-content {{ background:#fff3e0; color:#e65100; padding:2px 6px;
                    border-radius:4px; margin-right:4px; }}
    .summary {{ font-size: 0.9rem; color: #555; margin-top: 8px; }}
    .source-link {{ font-size: 0.8rem; }}
    nav {{ margin-bottom: 20px; font-size: 0.85rem; }}
    nav a {{ color: #0066CC; text-decoration: none; margin-right: 12px; }}
    footer {{ margin-top: 40px; font-size: 0.75rem; color: #aaa;
              border-top: 1px solid #e0e0e0; padding-top: 12px; }}
  </style>
</head>
<body>
  <nav><a href="../index.html">← トップへ戻る</a></nav>
  <h1>AI情報まとめ — {date}</h1>
  <p class="meta">収集件数: {count}件 | 週次候補: {weekly}件 | コンテンツ候補: {content}件</p>
  {cards}
  <footer>ai-research システムにより自動生成 | {generated_at}</footer>
</body>
</html>
"""

_HTML_CARD_TEMPLATE = """\
<div class="card">
  <div class="meta">{source} | {published}</div>
  <h2><a href="{url}" target="_blank" rel="noopener">{title}</a></h2>
  <div class="score">{stars} {score:.1f}</div>
  <div class="flags">{flags}</div>
  <p class="summary">{summary}</p>
</div>
"""

_HTML_INDEX_TEMPLATE = """\
<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>AI情報ダッシュボード | ai-research</title>
  <style>
    body {{ font-family: 'Noto Sans JP', 'Helvetica Neue', Arial, sans-serif;
           max-width: 860px; margin: 0 auto; padding: 16px; color: #333; }}
    h1 {{ font-size: 1.5rem; border-bottom: 2px solid #0066CC; padding-bottom: 8px; }}
    .day-section {{ margin-bottom: 28px; }}
    .day-header {{ display: flex; align-items: baseline; gap: 12px; }}
    .day-title {{ font-size: 1.1rem; font-weight: bold; }}
    .day-link {{ font-size: 0.82rem; color: #0066CC; text-decoration: none; }}
    .article-list {{ list-style: none; padding: 0; margin: 8px 0 0; }}
    .article-list li {{ padding: 6px 0; border-bottom: 1px solid #f0f0f0;
                       font-size: 0.9rem; display: flex; gap: 8px; }}
    .article-list li a {{ color: #222; text-decoration: none; flex: 1; }}
    .article-list li a:hover {{ color: #0066CC; }}
    .score-badge {{ font-size: 0.75rem; color: #f5a623; white-space: nowrap; }}
    .category-badge {{ font-size: 0.72rem; color: #888; white-space: nowrap; }}
    footer {{ margin-top: 40px; font-size: 0.75rem; color: #aaa;
              border-top: 1px solid #e0e0e0; padding-top: 12px; }}
  </style>
</head>
<body>
  <h1>AI情報ダッシュボード</h1>
  <p style="font-size:0.85rem;color:#666;">毎朝6時JST自動更新 | 最新{days}日分</p>
  {day_sections}
  <footer>ai-research システムにより自動生成 | {generated_at}</footer>
</body>
</html>
"""


def _escape_html(text: str) -> str:
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def _stars_html(score: float) -> str:
    full = min(int(score), 5)
    empty = max(0, 5 - full)
    return "★" * full + "☆" * empty


def generate_daily_html(articles: list[dict], target_date: date) -> str:
    """日次レポートHTMLを生成する"""
    cards_html = ""
    weekly = 0
    content_count = 0

    for a in sorted(articles, key=lambda x: x.get("score", 0), reverse=True):
        flags = ""
        if a.get("is_weekly_candidate"):
            flags += '<span class="flag-weekly">週次候補</span>'
            weekly += 1
        if a.get("is_monthly_candidate"):
            flags += '<span class="flag-monthly">月次候補</span>'
        if a.get("is_content_candidate"):
            flags += '<span class="flag-content">コンテンツ候補</span>'
            content_count += 1

        pub = (a.get("published_at") or "")[:10]
        cards_html += _HTML_CARD_TEMPLATE.format(
            source=_escape_html(a.get("source_name", "")),
            published=_escape_html(pub),
            url=_escape_html(a.get("url", "#")),
            title=_escape_html(a.get("title", "（タイトルなし）")),
            stars=_stars_html(a.get("score", 0)),
            score=a.get("score", 0),
            flags=flags or "—",
            summary=_escape_html((a.get("summary_raw") or "")[:200]),
        )

    return _HTML_DAILY_TEMPLATE.format(
        date=target_date.isoformat(),
        count=len(articles),
        weekly=weekly,
        content=content_count,
        cards=cards_html,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


def generate_index_html(recent: list[tuple[date, list[dict]]]) -> str:
    """GitHub Pages トップページHTMLを生成する"""
    day_sections = ""
    for d, articles in recent:
        sorted_arts = sorted(articles, key=lambda x: x.get("score", 0), reverse=True)
        daily_url = f"daily/{d.isoformat()}.html"
        items = ""
        for a in sorted_arts[:5]:
            items += (
                f'<li>'
                f'<a href="{_escape_html(a.get("url","#"))}" target="_blank" rel="noopener">'
                f'{_escape_html(a.get("title","")[:60])}</a>'
                f'<span class="score-badge">{_stars_html(a.get("score",0))} {a.get("score",0):.1f}</span>'
                f'<span class="category-badge">{_escape_html(a.get("category",""))}</span>'
                f'</li>'
            )
        day_sections += (
            f'<div class="day-section">'
            f'<div class="day-header">'
            f'<span class="day-title">{d.isoformat()}</span>'
            f'<a class="day-link" href="{daily_url}">全{len(articles)}件を見る →</a>'
            f'</div>'
            f'<ul class="article-list">{items}</ul>'
            f'</div>'
        )

    return _HTML_INDEX_TEMPLATE.format(
        days=len(recent),
        day_sections=day_sections,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


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
