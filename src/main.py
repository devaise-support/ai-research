"""
main.py - 全体実行エントリーポイント

実行フロー:
  1. fetcher.py: RSS・YouTube・Redditから記事収集
  2. scorer.py:  AIスコアリング（Gemini / Claude / Mock）
  3. writer.py:  日次Markdownレポート・スコアJSON・用語集の出力

使い方:
  python src/main.py                   # 通常実行（Gemini優先）
  python src/main.py --claude          # Claude API優先
  python src/main.py --dry-run         # 収集・スコアリングのみ（ファイル出力なし）
  python src/main.py --date 2026-05-07 # 特定日付で出力（テスト用）
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# src/ ディレクトリをパスに追加
sys.path.insert(0, str(Path(__file__).parent))

load_dotenv()

# ---------------------------------------------------------------------------
# ログ設定（日本語出力）
# ---------------------------------------------------------------------------
import io
# Windows環境でのUTF-8出力を強制
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def main() -> int:
    """
    Returns:
        0: 正常終了
        1: エラー終了
    """
    parser = argparse.ArgumentParser(
        description="生成AI情報収集・スコアリング・日次レポート生成システム"
    )
    parser.add_argument(
        "--claude",
        action="store_true",
        help="Claude API を優先してスコアリングに使用する",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="出力日付を指定（形式: YYYY-MM-DD）。未指定の場合は今日",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="収集・スコアリングのみ実行し、ファイル出力はしない",
    )
    parser.add_argument(
        "--no-rss",
        action="store_true",
        help="RSSフィードの収集をスキップ（デバッグ用）",
    )
    args = parser.parse_args()

    # Claude API 優先フラグ
    if args.claude:
        os.environ["PREFER_CLAUDE"] = "1"
        logger.info("Claude API 優先モードで実行します")

    # 出力日付の決定
    if args.date:
        try:
            target_date = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            logger.error(f"日付の形式が正しくありません: {args.date}（正しい形式: YYYY-MM-DD）")
            return 1
    else:
        target_date = datetime.now(timezone.utc)

    logger.info("=" * 60)
    logger.info(f"生成AI日次リサーチ開始: {target_date.strftime('%Y-%m-%d')}")
    logger.info("=" * 60)

    # -----------------------------------------------------------------------
    # Step 1: 収集
    # -----------------------------------------------------------------------
    logger.info("【Step 1】RSS・YouTube・Reddit から記事を収集中...")
    try:
        from fetcher import fetch_all
        articles = fetch_all()
    except Exception as e:
        logger.error(f"収集処理で致命的なエラーが発生しました: {e}")
        return 1

    if not articles:
        logger.warning("収集された記事が0件です。処理を終了します。")
        return 0

    logger.info(f"収集完了: {len(articles)}件")

    # -----------------------------------------------------------------------
    # Step 2: スコアリング
    # -----------------------------------------------------------------------
    logger.info("【Step 2】AIスコアリング中...")
    try:
        from scorer import score_all
        prefer_claude = args.claude or bool(os.environ.get("PREFER_CLAUDE"))
        articles = score_all(articles, prefer_claude=prefer_claude)
    except Exception as e:
        logger.error(f"スコアリング処理で致命的なエラーが発生しました: {e}")
        return 1

    logger.info(f"スコアリング完了: {len(articles)}件")

    # -----------------------------------------------------------------------
    # Step 3: 出力
    # -----------------------------------------------------------------------
    if args.dry_run:
        logger.info("【Step 3】ドライランモード: ファイル出力をスキップします")
        logger.info("スコア上位5件:")
        for i, a in enumerate(articles[:5], 1):
            logger.info(f"  {i}. [{a.score:.1f}] {a.title[:60]}")
    else:
        logger.info("【Step 3】日次レポートを出力中...")
        try:
            from writer import write_daily
            out_path = write_daily(articles, target_date=target_date)
            logger.info(f"日次レポート出力完了: {out_path}")
        except Exception as e:
            logger.error(f"レポート出力で致命的なエラーが発生しました: {e}")
            return 1

    # -----------------------------------------------------------------------
    # 完了サマリー
    # -----------------------------------------------------------------------
    logger.info("=" * 60)
    weekly_count = sum(1 for a in articles if a.is_weekly_candidate)
    content_count = sum(1 for a in articles if a.is_content_candidate)
    new_terms = list(dict.fromkeys(t for a in articles for t in a.new_terms))
    logger.info(f"[完了] 処理完了: {len(articles)}件収集・スコアリング")
    logger.info(f"   週次候補: {weekly_count}件 / コンテンツ候補: {content_count}件")
    if new_terms:
        logger.info(f"   新出用語: {', '.join(new_terms[:5])}")
    logger.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
