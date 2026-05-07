"""
feedback.py - フィードバック学習システム

記事に対するフィードバック（like/dislike/sellable/educational/shareable）を記録し、
蓄積されたフィードバックに基づいてスコアリング重みを自動更新する。

使い方:
  python src/feedback.py --id abc123def456 --type like
  python src/feedback.py --id abc123def456 --type sellable
  python src/feedback.py --recalculate       # 手動で重み再計算
  python src/feedback.py --list              # 最近のフィードバック一覧
  python src/feedback.py --history           # 重み更新履歴一覧
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# src/ をパスに追加
sys.path.insert(0, str(Path(__file__).parent))

import io
import os
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
FEEDBACK_DIR = ROOT / "feedback"
CONFIG_DIR = ROOT / "config"
SCORES_DIR = ROOT / "scores"

WEIGHTS_FILE = CONFIG_DIR / "scoring_weights.json"
WEIGHT_HISTORY_FILE = CONFIG_DIR / "weight_history.json"
FEEDBACK_FILE = FEEDBACK_DIR / "feedbacks.json"

# フィードバック種別の定義
FEEDBACK_TYPES = {
    "like":        {"label": "いいね",           "emoji": "👍"},
    "dislike":     {"label": "不要",             "emoji": "👎"},
    "sellable":    {"label": "販売に使えそう",   "emoji": "⭐"},
    "educational": {"label": "勉強になった",     "emoji": "📚"},
    "shareable":   {"label": "クライアントに送りたい", "emoji": "📤"},
}

FEEDBACK_THRESHOLD = 10  # この件数を超えたら自動で重み再計算
MAX_WEIGHT = 2.0
MIN_WEIGHT = 0.5


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _load_json(path: Path, default) -> dict | list:
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"JSONファイルの読み込みエラー [{path.name}]: {e}")
    return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_weights() -> dict:
    return _load_json(WEIGHTS_FILE, {
        "weights": {
            "importance": 0.30, "novelty": 0.25, "business_value": 0.20,
            "learning_value": 0.15, "primary_source_score": 0.10
        },
        "category_boost": {},
        "thresholds": {"weekly_candidate": 3.5, "monthly_candidate": 4.0, "content_candidate": 4.0},
        "version": 1, "updated_at": None
    })


def _load_feedbacks() -> list:
    data = _load_json(FEEDBACK_FILE, {"feedbacks": [], "last_recalculated_at": None, "processed_count": 0})
    return data if isinstance(data, dict) else {"feedbacks": [], "last_recalculated_at": None, "processed_count": 0}


def _save_feedbacks(data: dict) -> None:
    _save_json(FEEDBACK_FILE, data)


# ---------------------------------------------------------------------------
# 記事情報の検索
# ---------------------------------------------------------------------------

def _find_article_info(article_id: str) -> dict | None:
    """
    scores/ 配下の JSON から article_id に一致する記事情報を検索して返す。
    見つからなければ None を返す。
    """
    for json_path in sorted(SCORES_DIR.rglob("*.json"), reverse=True):
        if json_path.name == "seen_articles.json":
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                articles = json.load(f)
            for article in articles:
                if article.get("id") == article_id:
                    return article
        except (json.JSONDecodeError, OSError):
            continue
    return None


# ---------------------------------------------------------------------------
# フィードバック登録
# ---------------------------------------------------------------------------

def add_feedback(article_id: str, feedback_type: str) -> bool:
    """
    フィードバックを登録する。

    Args:
        article_id: 記事ID（12桁ハッシュ）
        feedback_type: like / dislike / sellable / educational / shareable

    Returns:
        bool: 登録成功かどうか
    """
    if feedback_type not in FEEDBACK_TYPES:
        logger.error(f"不正なフィードバック種別: {feedback_type}（使用可能: {', '.join(FEEDBACK_TYPES.keys())}）")
        return False

    # 記事情報を検索
    article_info = _find_article_info(article_id)
    if article_info is None:
        logger.warning(f"記事ID [{article_id}] が scores/ 配下に見つかりません。フィードバックは登録しますが記事情報は空になります。")

    data = _load_feedbacks()
    feedbacks: list = data.get("feedbacks", [])

    entry = {
        "article_id": article_id,
        "title": article_info.get("title", "（不明）") if article_info else "（不明）",
        "type": feedback_type,
        "category": article_info.get("category", "不明") if article_info else "不明",
        "tags": article_info.get("tags", []) if article_info else [],
        "score": article_info.get("score", 0) if article_info else 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    feedbacks.append(entry)
    data["feedbacks"] = feedbacks

    _save_feedbacks(data)

    fb_label = FEEDBACK_TYPES[feedback_type]["label"]
    title_short = entry["title"][:40]
    logger.info(f"フィードバック登録: [{article_id}] {title_short} → {fb_label}")

    # 未処理件数が閾値を超えたら自動再計算
    processed_count = data.get("processed_count", 0)
    unprocessed = len(feedbacks) - processed_count
    if unprocessed >= FEEDBACK_THRESHOLD:
        logger.info(f"未処理フィードバックが {unprocessed} 件に達しました。重みを自動更新します...")
        recalculate_weights(data, reason="auto_threshold")
    else:
        remaining = FEEDBACK_THRESHOLD - unprocessed
        logger.info(f"あと {remaining} 件フィードバックが蓄積されると重みが自動更新されます")

    return True


# ---------------------------------------------------------------------------
# 重み再計算
# ---------------------------------------------------------------------------

def recalculate_weights(data: dict | None = None, reason: str = "manual") -> None:
    """
    蓄積されたフィードバックを元にスコアリング重みを再計算して保存する。

    Args:
        data: feedbacks データ（Noneの場合はファイルから読み込む）
        reason: 更新理由（ログ・履歴用）
    """
    if data is None:
        data = _load_feedbacks()

    feedbacks: list = data.get("feedbacks", [])
    if not feedbacks:
        logger.info("フィードバックがありません。重みの更新はスキップします。")
        return

    weights = _load_weights()
    weight_map: dict = weights.get("weights", {})
    category_boost: dict = weights.get("category_boost", {})

    # フィードバックを集計
    like_by_category: dict[str, int] = {}
    dislike_by_category: dict[str, int] = {}
    sellable_count = 0
    educational_count = 0

    for fb in feedbacks:
        cat = fb.get("category", "不明")
        fb_type = fb.get("type", "")

        if fb_type == "like":
            like_by_category[cat] = like_by_category.get(cat, 0) + 1
        elif fb_type == "dislike":
            dislike_by_category[cat] = dislike_by_category.get(cat, 0) + 1
        elif fb_type == "sellable":
            sellable_count += 1
        elif fb_type == "educational":
            educational_count += 1

    changes = []

    # category_boost 更新：like > dislike のカテゴリを +0.05、逆は -0.05
    all_categories = set(like_by_category.keys()) | set(dislike_by_category.keys())
    for cat in all_categories:
        likes = like_by_category.get(cat, 0)
        dislikes = dislike_by_category.get(cat, 0)
        current = category_boost.get(cat, 1.0)

        if likes > dislikes:
            new_val = min(MAX_WEIGHT, round(current + 0.05, 3))
            if new_val != current:
                category_boost[cat] = new_val
                changes.append(f"category_boost[{cat}]: {current:.2f} → {new_val:.2f} (like+)")
        elif dislikes > likes:
            new_val = max(MIN_WEIGHT, round(current - 0.05, 3))
            if new_val != current:
                category_boost[cat] = new_val
                changes.append(f"category_boost[{cat}]: {current:.2f} → {new_val:.2f} (dislike-)")

    # sellable → business_value 重み +0.03
    if sellable_count >= 3:
        old = weight_map.get("business_value", 0.20)
        new_val = min(MAX_WEIGHT, round(old + 0.03, 3))
        if new_val != old:
            weight_map["business_value"] = new_val
            changes.append(f"weights.business_value: {old:.3f} → {new_val:.3f} (sellable+)")

    # educational → learning_value 重み +0.03
    if educational_count >= 3:
        old = weight_map.get("learning_value", 0.15)
        new_val = min(MAX_WEIGHT, round(old + 0.03, 3))
        if new_val != old:
            weight_map["learning_value"] = new_val
            changes.append(f"weights.learning_value: {old:.3f} → {new_val:.3f} (educational+)")

    # 重みの正規化（合計が1.0になるように）
    total = sum(weight_map.values())
    if total > 0 and abs(total - 1.0) > 0.001:
        weight_map = {k: round(v / total, 4) for k, v in weight_map.items()}
        changes.append(f"重み正規化: 合計 {total:.3f} → 1.0000")

    # 更新を反映
    weights["weights"] = weight_map
    weights["category_boost"] = category_boost
    weights["updated_at"] = datetime.now(timezone.utc).isoformat()
    weights["version"] = weights.get("version", 1) + 1

    _save_json(WEIGHTS_FILE, weights)

    # 更新履歴を追記
    history = _load_json(WEIGHT_HISTORY_FILE, [])
    if not isinstance(history, list):
        history = []
    history.append({
        "timestamp": weights["updated_at"],
        "version": weights["version"],
        "reason": reason,
        "total_feedbacks": len(feedbacks),
        "changes": changes,
        "weights": dict(weight_map),
        "category_boost": dict(category_boost),
    })
    _save_json(WEIGHT_HISTORY_FILE, history)

    # processed_count を更新
    data["processed_count"] = len(feedbacks)
    data["last_recalculated_at"] = weights["updated_at"]
    _save_feedbacks(data)

    if changes:
        logger.info(f"重み更新完了（バージョン {weights['version']}）:")
        for c in changes:
            logger.info(f"  {c}")
    else:
        logger.info("フィードバック分析完了。重みに有意な変化はありませんでした。")


# ---------------------------------------------------------------------------
# 一覧表示
# ---------------------------------------------------------------------------

def list_feedbacks(limit: int = 20) -> None:
    """最近のフィードバック一覧を表示する"""
    data = _load_feedbacks()
    feedbacks = data.get("feedbacks", [])

    if not feedbacks:
        logger.info("フィードバックはまだありません。")
        return

    logger.info(f"フィードバック一覧（最新 {min(limit, len(feedbacks))} 件）:")
    for fb in reversed(feedbacks[-limit:]):
        fb_type = fb.get("type", "")
        emoji = FEEDBACK_TYPES.get(fb_type, {}).get("emoji", "?")
        label = FEEDBACK_TYPES.get(fb_type, {}).get("label", fb_type)
        ts = fb.get("timestamp", "")[:10]
        title = fb.get("title", "（不明）")[:50]
        cat = fb.get("category", "不明")
        logger.info(f"  {emoji} [{ts}] {fb.get('article_id', '')[:8]}... | {title} ({cat}) | {label}")

    processed = data.get("processed_count", 0)
    unprocessed = len(feedbacks) - processed
    logger.info(f"\n合計 {len(feedbacks)} 件（未処理 {unprocessed} 件 / 処理済み {processed} 件）")
    logger.info(f"次の重み更新まであと {max(0, FEEDBACK_THRESHOLD - unprocessed)} 件")


def show_weight_history(limit: int = 5) -> None:
    """重み更新履歴を表示する"""
    history = _load_json(WEIGHT_HISTORY_FILE, [])
    if not isinstance(history, list) or not history:
        logger.info("重み更新履歴はまだありません。")
        return

    logger.info(f"重み更新履歴（最新 {min(limit, len(history))} 件）:")
    for record in reversed(history[-limit:]):
        ts = record.get("timestamp", "")[:16].replace("T", " ")
        ver = record.get("version", "-")
        reason_map = {"auto_threshold": "自動（閾値到達）", "manual": "手動実行"}
        reason = reason_map.get(record.get("reason", ""), record.get("reason", ""))
        total = record.get("total_feedbacks", 0)
        changes = record.get("changes", [])
        logger.info(f"\n  バージョン {ver} | {ts} | {reason} | フィードバック {total} 件")
        for c in changes:
            logger.info(f"    {c}")

    logger.info(f"\n合計 {len(history)} 件の更新履歴")


def show_current_weights() -> None:
    """現在の重み設定を表示する"""
    weights = _load_weights()
    logger.info("現在のスコアリング重み設定:")
    logger.info(f"  バージョン: {weights.get('version', 1)}")
    logger.info(f"  最終更新: {weights.get('updated_at', '未更新')}")
    logger.info("  各項目の重み:")
    for k, v in weights.get("weights", {}).items():
        logger.info(f"    {k}: {v:.4f}")
    logger.info("  カテゴリboost（デフォルト以外）:")
    for k, v in weights.get("category_boost", {}).items():
        if v != 1.0:
            logger.info(f"    {k}: {v:.3f}")


# ---------------------------------------------------------------------------
# CLI エントリーポイント
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成AI記事フィードバック学習システム"
    )
    parser.add_argument(
        "--id",
        type=str,
        help="フィードバック対象の記事ID（12桁ハッシュ）",
    )
    parser.add_argument(
        "--type",
        type=str,
        choices=list(FEEDBACK_TYPES.keys()),
        help="フィードバック種別: like / dislike / sellable / educational / shareable",
    )
    parser.add_argument(
        "--recalculate",
        action="store_true",
        help="フィードバックから重みを手動で再計算する",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="最近のフィードバック一覧を表示する",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="重み更新履歴を表示する",
    )
    parser.add_argument(
        "--weights",
        action="store_true",
        help="現在の重み設定を表示する",
    )
    args = parser.parse_args()

    # フィードバック登録
    if args.id and args.type:
        success = add_feedback(args.id, args.type)
        return 0 if success else 1

    # 手動再計算
    if args.recalculate:
        recalculate_weights(reason="manual")
        return 0

    # 一覧表示
    if args.list:
        list_feedbacks()
        return 0

    # 更新履歴
    if args.history:
        show_weight_history()
        return 0

    # 現在の重み表示
    if args.weights:
        show_current_weights()
        return 0

    # 引数なし → ヘルプ表示
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
