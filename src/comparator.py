"""
comparator.py - トレンド比較・キーワード時系列分析モジュール

機能:
  - 今週 vs 先週のカテゴリ別記事数の増減率計算
  - キーワード出現頻度の時系列記録（trends/keyword_history.json）
  - 新規キーワード（過去4週未出現）の検出
  - 急上昇（+50%以上）・衰退トピックの検出

単一責任: トレンド分析のみ。出力は aggregator.py が担当。
"""

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
SCORES_DIR = ROOT / "scores"
TRENDS_DIR = ROOT / "trends"
KEYWORD_HISTORY_FILE = TRENDS_DIR / "keyword_history.json"


# ---------------------------------------------------------------------------
# scores/ からの記事読み込み
# ---------------------------------------------------------------------------

def load_articles_in_range(start: datetime, end: datetime) -> list[dict]:
    """
    scores/ 配下から指定期間内の全記事を読み込む。

    Args:
        start: 期間開始（UTC）
        end:   期間終了（UTC）

    Returns:
        記事辞書のリスト
    """
    articles = []
    for json_path in SCORES_DIR.rglob("*.json"):
        if json_path.name == "seen_articles.json":
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                continue
            for article in data:
                pub_str = article.get("published_at", "")
                try:
                    pub_dt = datetime.fromisoformat(pub_str)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
                    if start <= pub_dt <= end:
                        articles.append(article)
                except (ValueError, TypeError):
                    continue
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"スコアJSONの読み込みエラー [{json_path}]: {e}")
    return articles


def load_articles_for_week(year: int, week: int) -> list[dict]:
    """ISO週番号を指定して記事を取得する"""
    # ISO週の月曜日を計算
    monday = datetime.fromisocalendar(year, week, 1).replace(tzinfo=timezone.utc)
    sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)
    return load_articles_in_range(monday, sunday)


def load_articles_for_month(year: int, month: int) -> list[dict]:
    """年月を指定して記事を取得する"""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc) - timedelta(seconds=1)
    return load_articles_in_range(start, end)


# ---------------------------------------------------------------------------
# キーワード時系列管理
# ---------------------------------------------------------------------------

def _load_keyword_history() -> dict:
    TRENDS_DIR.mkdir(parents=True, exist_ok=True)
    if KEYWORD_HISTORY_FILE.exists():
        try:
            with open(KEYWORD_HISTORY_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_keyword_history(history: dict) -> None:
    TRENDS_DIR.mkdir(parents=True, exist_ok=True)
    with open(KEYWORD_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _extract_keywords(articles: list[dict]) -> Counter:
    """記事リストからキーワード（タグ）の出現頻度を集計する"""
    counter: Counter = Counter()
    for article in articles:
        tags = article.get("tags", [])
        for tag in tags:
            if tag and tag not in ("mock", "test") and len(tag) > 1:
                counter[tag] += 1
        # new_termsもカウント対象
        for term in article.get("new_terms", []):
            if term and len(term) > 1:
                counter[term] += 1
    return counter


def update_keyword_history(articles: list[dict], week_key: str) -> dict:
    """
    指定週のキーワード出現頻度を keyword_history.json に記録する。

    Args:
        articles: その週の記事リスト
        week_key: "YYYY-WNN" 形式の週キー

    Returns:
        更新後の history dict
    """
    history = _load_keyword_history()
    counter = _extract_keywords(articles)

    # 上位30キーワードのみ記録（ノイズ排除）
    top_keywords = dict(counter.most_common(30))
    history[week_key] = top_keywords
    _save_keyword_history(history)
    logger.info(f"キーワード時系列を更新: {week_key} ({len(top_keywords)}キーワード)")
    return history


# ---------------------------------------------------------------------------
# カテゴリ増減率計算
# ---------------------------------------------------------------------------

def compare_categories(
    current_articles: list[dict],
    prev_articles: list[dict],
) -> list[dict]:
    """
    今週 vs 先週のカテゴリ別記事数の増減率を計算する。

    Returns:
        [{"category": str, "current": int, "prev": int, "rate": float, "direction": str}, ...]
        rate: 増加率（%）、direction: "up" / "down" / "same"
    """
    current_counts = Counter(a.get("category", "不明") for a in current_articles)
    prev_counts = Counter(a.get("category", "不明") for a in prev_articles)
    all_categories = set(current_counts.keys()) | set(prev_counts.keys())

    results = []
    for cat in sorted(all_categories):
        current = current_counts.get(cat, 0)
        prev = prev_counts.get(cat, 0)
        rate = (current - prev) / max(prev, 1) * 100

        if rate > 5:
            direction = "up"
        elif rate < -5:
            direction = "down"
        else:
            direction = "same"

        results.append({
            "category": cat,
            "current": current,
            "prev": prev,
            "rate": round(rate, 1),
            "direction": direction,
        })

    # 変化率の絶対値が大きい順にソート
    results.sort(key=lambda x: abs(x["rate"]), reverse=True)
    return results


# ---------------------------------------------------------------------------
# 新規キーワード検出
# ---------------------------------------------------------------------------

def detect_new_keywords(
    current_articles: list[dict],
    lookback_weeks: int = 4,
    reference_week: tuple[int, int] | None = None,
) -> list[str]:
    """
    過去 lookback_weeks 週に出現していない新規キーワードを検出する。

    Args:
        current_articles: 今週の記事リスト
        lookback_weeks: 比較する過去週数
        reference_week: (year, week) 基準週（Noneなら今週）

    Returns:
        新規キーワードのリスト
    """
    history = _load_keyword_history()

    if reference_week:
        year, week = reference_week
    else:
        now = datetime.now(timezone.utc)
        iso = now.isocalendar()
        year, week = iso.year, iso.week

    # 過去N週のキーワードセットを構築
    past_keywords: set[str] = set()
    for i in range(1, lookback_weeks + 1):
        past_week = week - i
        past_year = year
        if past_week <= 0:
            past_year -= 1
            past_week += 52
        key = f"{past_year}-W{past_week:02d}"
        past_data = history.get(key, {})
        past_keywords.update(past_data.keys())

    # 今週のキーワード
    current_keywords = set(_extract_keywords(current_articles).keys())

    # 差集合 = 新規キーワード
    new_keywords = sorted(current_keywords - past_keywords)
    if new_keywords:
        logger.info(f"新規キーワード {len(new_keywords)} 件: {', '.join(new_keywords[:10])}")
    return new_keywords


# ---------------------------------------------------------------------------
# 急上昇・衰退トレンド検出
# ---------------------------------------------------------------------------

def detect_rising_trends(lookback: int = 4) -> list[dict]:
    """
    急上昇トピックを検出する（直近N週 vs 前N週で増加率+50%以上）。

    Returns:
        [{"keyword": str, "recent": int, "prev": int, "rate": float}, ...]
    """
    history = _load_keyword_history()
    if not history:
        return []

    weeks = sorted(history.keys())
    if len(weeks) < 2:
        return []

    # 直近N週と前N週に分割
    recent_weeks = weeks[-lookback:]
    prev_weeks = weeks[-lookback * 2:-lookback] if len(weeks) >= lookback * 2 else weeks[:-lookback]

    recent_totals: Counter = Counter()
    for wk in recent_weeks:
        for kw, cnt in history.get(wk, {}).items():
            recent_totals[kw] += cnt

    prev_totals: Counter = Counter()
    for wk in prev_weeks:
        for kw, cnt in history.get(wk, {}).items():
            prev_totals[kw] += cnt

    rising = []
    for kw, recent_cnt in recent_totals.items():
        prev_cnt = prev_totals.get(kw, 0)
        rate = (recent_cnt - prev_cnt) / max(prev_cnt, 1) * 100
        if rate >= 50 and recent_cnt >= 2:
            rising.append({
                "keyword": kw,
                "recent": recent_cnt,
                "prev": prev_cnt,
                "rate": round(rate, 1),
            })

    rising.sort(key=lambda x: -x["rate"])
    return rising[:10]


def detect_declining_trends(lookback: int = 4, min_prev_count: int = 3) -> list[dict]:
    """
    衰退トピックを検出する（前N週に多かったが直近N週に激減）。

    Returns:
        [{"keyword": str, "recent": int, "prev": int, "rate": float}, ...]
    """
    history = _load_keyword_history()
    if not history:
        return []

    weeks = sorted(history.keys())
    if len(weeks) < 2:
        return []

    recent_weeks = weeks[-lookback:]
    prev_weeks = weeks[-lookback * 2:-lookback] if len(weeks) >= lookback * 2 else weeks[:-lookback]

    recent_totals: Counter = Counter()
    for wk in recent_weeks:
        for kw, cnt in history.get(wk, {}).items():
            recent_totals[kw] += cnt

    prev_totals: Counter = Counter()
    for wk in prev_weeks:
        for kw, cnt in history.get(wk, {}).items():
            prev_totals[kw] += cnt

    declining = []
    for kw, prev_cnt in prev_totals.items():
        if prev_cnt < min_prev_count:
            continue
        recent_cnt = recent_totals.get(kw, 0)
        rate = (recent_cnt - prev_cnt) / max(prev_cnt, 1) * 100
        if rate <= -50:
            declining.append({
                "keyword": kw,
                "recent": recent_cnt,
                "prev": prev_cnt,
                "rate": round(rate, 1),
            })

    declining.sort(key=lambda x: x["rate"])
    return declining[:10]


# ---------------------------------------------------------------------------
# 自然言語サマリー生成（簡易版）
# ---------------------------------------------------------------------------

def build_change_summary(
    category_changes: list[dict],
    new_keywords: list[str],
    rising: list[dict],
) -> str:
    """
    カテゴリ変化・新規キーワード・急上昇トレンドから自然言語サマリーを生成する。
    （APIなし版：テンプレートベース）

    Returns:
        サマリーテキスト（Markdown形式）
    """
    lines = []

    # カテゴリ変化
    up_cats = [c for c in category_changes if c["direction"] == "up"]
    down_cats = [c for c in category_changes if c["direction"] == "down"]

    if up_cats:
        top_up = up_cats[0]
        lines.append(
            f"今週は **{top_up['category']}** カテゴリが先週比 **+{top_up['rate']:.0f}%** 増加しました。"
        )
    if down_cats:
        top_down = down_cats[0]
        lines.append(
            f"一方 **{top_down['category']}** は **{top_down['rate']:.0f}%** 減少しました。"
        )

    # 急上昇キーワード
    if rising:
        kw_str = " / ".join(f"**{r['keyword']}**（+{r['rate']:.0f}%）" for r in rising[:3])
        lines.append(f"\n急上昇キーワード: {kw_str}")

    # 新出キーワード
    if new_keywords:
        kw_str = " / ".join(f"`{k}`" for k in new_keywords[:5])
        lines.append(f"\n新出キーワード: {kw_str}")

    if not lines:
        lines.append("今週は先週と比較して大きな変化はありませんでした。")

    return "\n".join(lines)
