"""
fetcher.py - RSS・YouTube・Reddit から生成AI記事を収集するモジュール

収集ソース:
  - RSS (feedparser): Anthropic / OpenAI / DeepMind / HuggingFace / Zenn / arXiv 等
  - YouTube Data API v3: AI関連チャンネル（YOUTUBE_API_KEY 必要）
  - Reddit JSON API: r/MachineLearning 等（認証不要）

単一責任: 収集のみ。スコアリングは scorer.py が担当。
"""

import calendar
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
import yaml
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# プロジェクトルート
ROOT = Path(__file__).parent.parent
CONFIG_DIR = ROOT / "config"
SCORES_DIR = ROOT / "scores"
SEEN_FILE = SCORES_DIR / "seen_articles.json"


# ---------------------------------------------------------------------------
# Article データクラス
# ---------------------------------------------------------------------------

@dataclass
class Article:
    """収集記事の統一データ構造"""
    id: str                          # SHA256(canonical_url)[:12]
    title: str
    url: str
    source_name: str
    category: str
    is_primary_source: bool
    language: str                    # "ja" または "en"
    published_at: datetime
    summary_raw: str                 # 元の要約（最大500字、HTMLタグ除去済み）
    fetched_at: datetime
    source_type: str = "rss"         # "rss" | "youtube" | "reddit"
    # スコアリング後に設定されるフィールド
    score: float = 0.0
    score_details: dict = field(default_factory=dict)
    is_weekly_candidate: bool = False
    is_monthly_candidate: bool = False
    is_content_candidate: bool = False
    is_x_post_candidate: bool = False
    new_terms: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    summary_ja: str = ""
    target_clients: list = field(default_factory=list)
    content_potential: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ユーティリティ関数
# ---------------------------------------------------------------------------

def _canonical_url(url: str) -> str:
    """クエリパラメータを除いた正規化URLを返す"""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _make_id(url: str) -> str:
    """正規化URLのSHA256ハッシュ先頭12文字をIDとして返す"""
    canonical = _canonical_url(url)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _strip_html(text: str) -> str:
    """HTMLタグを除去して平文を返す"""
    if not text:
        return ""
    clean = re.sub(r'<[^>]+>', ' ', text)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean[:500]


def _detect_language(text: str) -> str:
    """テキストが日本語か英語かを簡易判定する"""
    ja_chars = len(re.findall(r'[぀-鿿]', text))
    return "ja" if ja_chars > 5 else "en"


def _is_primary_source(url: str, title: str, summary: str, primary_config: dict) -> bool:
    """一次情報かどうかを2段階で判定する"""
    domain = urlparse(url).netloc.replace("www.", "")
    if domain in primary_config.get("primary_domains", []):
        return True
    text = (title + " " + summary).lower()
    for kw in primary_config.get("primary_keywords_ja", []):
        if kw in text:
            return True
    for kw in primary_config.get("primary_keywords_en", []):
        if kw.lower() in text:
            return True
    return False


def _load_seen_articles() -> dict:
    """収集済みURLを読み込む。ファイルがない場合は空辞書を返す。"""
    if SEEN_FILE.exists():
        try:
            with open(SEEN_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"seen_articles.json の読み込みエラー: {e}")
    return {}


def _save_seen_articles(seen: dict) -> None:
    """収集済みURLを保存する"""
    SCORES_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    # 30日以上前のエントリを自動パージ
    cleaned = {
        k: v for k, v in seen.items()
        if datetime.fromisoformat(v).replace(tzinfo=timezone.utc) >= cutoff
    }
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"seen_articles.json の保存エラー: {e}")


def _load_config(filename: str) -> dict:
    """config/ ディレクトリの YAML ファイルを読み込む"""
    path = CONFIG_DIR / filename
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# RSS 収集
# ---------------------------------------------------------------------------

def _fetch_rss(feed_config: dict, primary_config: dict, seen: dict,
               max_age_days: int, timeout: int) -> list[Article]:
    """1つのRSSフィードから記事を収集して返す"""
    articles = []
    name = feed_config["name"]
    url = feed_config["url"]
    category = feed_config.get("category", "ニュース・動向")
    lang = feed_config.get("language", "en")
    max_per_feed = feed_config.get("max_per_feed", 3)

    try:
        # feedparser はタイムアウトを直接サポートしないため requests で取得
        resp = requests.get(url, timeout=timeout,
                            headers={"User-Agent": "ai-research/1.0"})
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except requests.exceptions.Timeout:
        logger.warning(f"タイムアウト（{timeout}秒）: {name}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"取得エラー [{name}]: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    count = 0

    for entry in feed.entries:
        if count >= max_per_feed:
            break

        entry_url = getattr(entry, "link", "")
        if not entry_url:
            continue

        article_id = _make_id(entry_url)
        if article_id in seen:
            continue

        # 公開日時の取得
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                pub_ts = calendar.timegm(entry.published_parsed)
                published_at = datetime.fromtimestamp(pub_ts, tz=timezone.utc)
            except Exception:
                published_at = datetime.now(timezone.utc)
        else:
            published_at = datetime.now(timezone.utc)

        if published_at < cutoff:
            continue

        title = getattr(entry, "title", "（タイトルなし）")
        summary_raw = _strip_html(getattr(entry, "summary", ""))
        language = lang if lang else _detect_language(title + summary_raw)
        is_primary = _is_primary_source(entry_url, title, summary_raw, primary_config)

        article = Article(
            id=article_id,
            title=title,
            url=entry_url,
            source_name=name,
            category=category,
            is_primary_source=is_primary,
            language=language,
            published_at=published_at,
            summary_raw=summary_raw,
            fetched_at=datetime.now(timezone.utc),
            source_type="rss",
        )
        articles.append(article)
        seen[article_id] = datetime.now(timezone.utc).isoformat()
        count += 1

    return articles


def fetch_rss_all(rss_config: dict, primary_config: dict, seen: dict) -> list[Article]:
    """全RSSフィードから記事を収集する"""
    all_articles: list[Article] = []
    max_age_days = rss_config.get("max_age_days", 14)
    timeout = rss_config.get("timeout_seconds", 10)
    total_limit = rss_config.get("total_limit", 10)
    skip_count = 0
    error_count = 0
    feed_errors = []

    for feed_cfg in rss_config.get("feeds", []):
        feed_cfg["max_per_feed"] = rss_config.get("max_per_feed", 3)
        try:
            articles = _fetch_rss(feed_cfg, primary_config, seen, max_age_days, timeout)
            all_articles.extend(articles)
        except Exception as e:
            error_count += 1
            feed_errors.append(f"{feed_cfg['name']}: {e}")
            logger.warning(f"フィード収集エラー [{feed_cfg['name']}]: {e}")

    # published_at 降順でソートして上限件数に絞る
    all_articles.sort(key=lambda a: a.published_at, reverse=True)
    result = all_articles[:total_limit]
    skip_count = len(all_articles) - len(result)

    logger.info(
        f"RSS収集: {len(result)}件取得 / スキップ {skip_count}件 / "
        f"エラー {error_count}件"
        + (f"（{', '.join(feed_errors)}）" if feed_errors else "")
    )
    return result


# ---------------------------------------------------------------------------
# YouTube 収集
# ---------------------------------------------------------------------------

def fetch_youtube(platform_config: dict, primary_config: dict, seen: dict) -> list[Article]:
    """YouTube Data API v3 で AI関連動画を収集する"""
    yt_cfg = platform_config.get("youtube", {})
    if not yt_cfg.get("enabled", False):
        return []

    api_key = os.environ.get(yt_cfg.get("api_key_env", "YOUTUBE_API_KEY"))
    if not api_key:
        logger.info("YOUTUBE_API_KEY 未設定のためYouTube収集をスキップ")
        return []

    try:
        from googleapiclient.discovery import build
        youtube = build("youtube", "v3", developerKey=api_key)
    except ImportError:
        logger.warning("google-api-python-client がインストールされていません。YouTube収集をスキップ。")
        return []
    except Exception as e:
        logger.warning(f"YouTube APIクライアントの初期化エラー: {e}")
        return []

    max_age_days = yt_cfg.get("max_age_days", 14)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    max_results = yt_cfg.get("max_results", 3)
    articles: list[Article] = []
    category = yt_cfg.get("category", "動画・チュートリアル")

    # チャンネル別収集
    for channel in yt_cfg.get("channels", []):
        if len(articles) >= max_results:
            break
        try:
            response = youtube.search().list(
                part="snippet",
                channelId=channel["id"],
                type="video",
                order="date",
                maxResults=3,
                publishedAfter=cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ).execute()

            for item in response.get("items", []):
                if len(articles) >= max_results:
                    break
                video_id = item["id"]["videoId"]
                video_url = f"https://www.youtube.com/watch?v={video_id}"
                article_id = _make_id(video_url)
                if article_id in seen:
                    continue

                snippet = item["snippet"]
                title = snippet.get("title", "")
                description = snippet.get("description", "")[:500]
                published_str = snippet.get("publishedAt", "")
                try:
                    published_at = datetime.fromisoformat(
                        published_str.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    published_at = datetime.now(timezone.utc)

                is_primary = _is_primary_source(video_url, title, description, primary_config)

                article = Article(
                    id=article_id,
                    title=title,
                    url=video_url,
                    source_name=f"YouTube/{channel.get('name', 'Unknown')}",
                    category=category,
                    is_primary_source=is_primary,
                    language=_detect_language(title + description),
                    published_at=published_at,
                    summary_raw=description,
                    fetched_at=datetime.now(timezone.utc),
                    source_type="youtube",
                )
                articles.append(article)
                seen[article_id] = datetime.now(timezone.utc).isoformat()

        except Exception as e:
            logger.warning(f"YouTubeチャンネル収集エラー [{channel.get('name')}]: {e}")

    logger.info(f"YouTube収集: {len(articles)}件取得")
    return articles


# ---------------------------------------------------------------------------
# Reddit 収集
# ---------------------------------------------------------------------------

def fetch_reddit(platform_config: dict, primary_config: dict, seen: dict) -> list[Article]:
    """Reddit JSON API でAI関連投稿を収集する（認証不要）"""
    rd_cfg = platform_config.get("reddit", {})
    if not rd_cfg.get("enabled", False):
        return []

    max_age_days = rd_cfg.get("max_age_days", 7)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    min_score = rd_cfg.get("min_score", 50)
    max_results = rd_cfg.get("max_results", 3)
    ai_keywords = [kw.lower() for kw in rd_cfg.get("ai_keywords", [])]
    category = rd_cfg.get("category", "コミュニティ・議論")
    user_agent = rd_cfg.get("user_agent", "ai-research/1.0")
    articles: list[Article] = []

    for subreddit_cfg in rd_cfg.get("subreddits", []):
        if len(articles) >= max_results:
            break

        sub_name = subreddit_cfg["name"]
        sort = subreddit_cfg.get("sort", "hot")
        api_url = f"https://www.reddit.com/r/{sub_name}/{sort}.json?limit=25"

        try:
            resp = requests.get(
                api_url,
                headers={"User-Agent": user_agent},
                timeout=10,
            )
            if resp.status_code == 429:
                logger.warning(f"Reddit API レート制限 (429): {sub_name}。5秒後リトライ")
                time.sleep(5)
                resp = requests.get(api_url, headers={"User-Agent": user_agent}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            logger.warning(f"Reddit収集エラー [r/{sub_name}]: {e}")
            continue
        except json.JSONDecodeError as e:
            logger.warning(f"Reddit JSONパースエラー [r/{sub_name}]: {e}")
            continue

        posts = data.get("data", {}).get("children", [])
        for post in posts:
            if len(articles) >= max_results:
                break

            pdata = post.get("data", {})
            reddit_score = pdata.get("score", 0)
            if reddit_score < min_score:
                continue

            title = pdata.get("title", "")
            title_lower = title.lower()
            if ai_keywords and not any(kw in title_lower for kw in ai_keywords):
                continue

            post_url = pdata.get("url", "")
            permalink = f"https://www.reddit.com{pdata.get('permalink', '')}"
            if not post_url:
                post_url = permalink

            article_id = _make_id(post_url)
            if article_id in seen:
                continue

            created_utc = pdata.get("created_utc", 0)
            published_at = datetime.fromtimestamp(created_utc, tz=timezone.utc)
            if published_at < cutoff:
                continue

            selftext = pdata.get("selftext", "")[:500]
            summary_raw = selftext if selftext else f"Reddit投稿 in r/{sub_name}。スコア: {reddit_score}"

            is_primary = _is_primary_source(post_url, title, summary_raw, primary_config)

            article = Article(
                id=article_id,
                title=f"[r/{sub_name}] {title}",
                url=post_url,
                source_name=f"Reddit/r/{sub_name}",
                category=category,
                is_primary_source=is_primary,
                language=_detect_language(title + selftext),
                published_at=published_at,
                summary_raw=summary_raw,
                fetched_at=datetime.now(timezone.utc),
                source_type="reddit",
            )
            articles.append(article)
            seen[article_id] = datetime.now(timezone.utc).isoformat()

    logger.info(f"Reddit収集: {len(articles)}件取得")
    return articles


# ---------------------------------------------------------------------------
# メイン収集関数
# ---------------------------------------------------------------------------

def fetch_all() -> list[Article]:
    """
    RSS・YouTube・Redditから全記事を収集してArticleリストを返す。

    Returns:
        list[Article]: 収集済み記事リスト（重複なし、日時降順）
    """
    rss_config = _load_config("rss_feeds.yaml")
    platform_config = _load_config("platform_sources.yaml")
    primary_config = _load_config("primary_domains.yaml")

    seen = _load_seen_articles()

    # RSS収集
    rss_articles = fetch_rss_all(rss_config, primary_config, seen)

    # YouTube収集
    yt_articles = fetch_youtube(platform_config, primary_config, seen)

    # Reddit収集
    rd_articles = fetch_reddit(platform_config, primary_config, seen)

    all_articles = rss_articles + yt_articles + rd_articles

    # 全体を published_at 降順にソート
    all_articles.sort(key=lambda a: a.published_at, reverse=True)

    # seen_articles.json を更新保存
    _save_seen_articles(seen)

    logger.info(
        f"収集合計: {len(all_articles)}件 "
        f"（RSS {len(rss_articles)}件 / YouTube {len(yt_articles)}件 / Reddit {len(rd_articles)}件）"
    )
    return all_articles
