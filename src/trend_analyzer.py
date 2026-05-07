"""
trend_analyzer.py - トレンド分析・知識ベース更新モジュール

機能:
  - trends/keyword_history.json を参照して急上昇・衰退トレンドを検出
  - knowledge/trends/rising.md  急上昇トピック（週次更新）
  - knowledge/trends/declining.md 衰退トピック（週次更新）

急上昇の定義:
  直近4週合計 / 前4週合計 の増加率 >= 50% かつ 直近4週合計 >= 2件

衰退の定義:
  前4週合計 >= 3件 かつ 直近4週との減少率 <= -50%

単一責任: トレンドページ生成のみ。
         キーワード時系列の記録は comparator.py が担当。
"""

import io
import json
import logging
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
TRENDS_DIR = ROOT / "trends"
KNOWLEDGE_DIR = ROOT / "knowledge"
KEYWORD_HISTORY_FILE = TRENDS_DIR / "keyword_history.json"


# ---------------------------------------------------------------------------
# keyword_history.json 読み込み
# ---------------------------------------------------------------------------

def _load_history() -> dict:
    if not KEYWORD_HISTORY_FILE.exists():
        return {}
    try:
        with open(KEYWORD_HISTORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# ---------------------------------------------------------------------------
# 急上昇・衰退トレンド検出（comparator.py との役割分担）
# ---------------------------------------------------------------------------

def _aggregate_counts(history: dict, week_keys: list[str]) -> Counter:
    """指定週リストのキーワード合計を返す"""
    totals: Counter = Counter()
    for wk in week_keys:
        for kw, cnt in history.get(wk, {}).items():
            totals[kw] += cnt
    return totals


def detect_rising(history: dict, lookback: int = 4, min_recent: int = 2, min_rate: float = 50.0) -> list[dict]:
    """
    急上昇キーワードを検出する。

    Returns:
        [{"keyword": str, "recent": int, "prev": int, "rate": float}, ...]
        rate降順、上位10件
    """
    weeks = sorted(history.keys())
    if len(weeks) < 2:
        return []

    recent_weeks = weeks[-lookback:]
    prev_weeks = weeks[-lookback * 2: -lookback] if len(weeks) >= lookback * 2 else weeks[:-lookback]

    recent = _aggregate_counts(history, recent_weeks)
    prev = _aggregate_counts(history, prev_weeks)

    results = []
    for kw, r_cnt in recent.items():
        if r_cnt < min_recent:
            continue
        p_cnt = prev.get(kw, 0)
        rate = (r_cnt - p_cnt) / max(p_cnt, 1) * 100
        if rate >= min_rate:
            results.append({"keyword": kw, "recent": r_cnt, "prev": p_cnt, "rate": round(rate, 1)})

    results.sort(key=lambda x: -x["rate"])
    return results[:10]


def detect_declining(
    history: dict,
    lookback: int = 4,
    min_prev: int = 3,
    max_rate: float = -50.0,
) -> list[dict]:
    """
    衰退キーワードを検出する。

    Returns:
        [{"keyword": str, "recent": int, "prev": int, "rate": float}, ...]
        rate昇順（マイナス大きい順）、上位10件
    """
    weeks = sorted(history.keys())
    if len(weeks) < 2:
        return []

    recent_weeks = weeks[-lookback:]
    prev_weeks = weeks[-lookback * 2: -lookback] if len(weeks) >= lookback * 2 else weeks[:-lookback]

    recent = _aggregate_counts(history, recent_weeks)
    prev = _aggregate_counts(history, prev_weeks)

    results = []
    for kw, p_cnt in prev.items():
        if p_cnt < min_prev:
            continue
        r_cnt = recent.get(kw, 0)
        rate = (r_cnt - p_cnt) / max(p_cnt, 1) * 100
        if rate <= max_rate:
            results.append({"keyword": kw, "recent": r_cnt, "prev": p_cnt, "rate": round(rate, 1)})

    results.sort(key=lambda x: x["rate"])
    return results[:10]


# ---------------------------------------------------------------------------
# AI サマリー生成
# ---------------------------------------------------------------------------

def _generate_trend_comment(prompt: str) -> str | None:
    if os.environ.get("CLAUDE_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
            resp = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=300,
                system="あなたは生成AIトレンドアナリストです。キーワードの急上昇・衰退について簡潔にコメントしてください。",
                messages=[{"role": "user", "content": prompt}],
            )
            time.sleep(0.5)
            return resp.content[0].text
        except Exception as e:
            logger.warning(f"Claude API エラー: {e}")

    if os.environ.get("GEMINI_API_KEY"):
        try:
            import google.generativeai as genai
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            model = genai.GenerativeModel("gemini-2.0-flash")
            resp = model.generate_content(prompt)
            time.sleep(0.5)
            return resp.text
        except Exception as e:
            logger.warning(f"Gemini API エラー: {e}")

    return None


# ---------------------------------------------------------------------------
# rising.md 生成
# ---------------------------------------------------------------------------

def build_rising_page(rising: list[dict], history: dict, dry_run: bool = False) -> Path:
    """knowledge/trends/rising.md を生成する"""
    out_path = KNOWLEDGE_DIR / "trends" / "rising.md"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    weeks = sorted(history.keys())

    lines = [
        "# 急上昇トピック",
        f"\n> 週次更新: {now_str}",
        "",
        "直近4週と前4週を比較して、出現頻度が+50%以上増加したキーワードです。",
        "",
    ]

    if rising:
        lines.append("## 🚀 急上昇キーワード TOP10")
        lines.append("")
        lines.append("| # | キーワード | 直近4週 | 前4週 | 増加率 |")
        lines.append("|---|-----------|---------|-------|--------|")
        for i, r in enumerate(rising, 1):
            sign = "+" if r["rate"] >= 0 else ""
            lines.append(
                f"| {i} | **{r['keyword']}** | {r['recent']}件 | {r['prev']}件 | {sign}{r['rate']:.0f}% |"
            )
        lines.append("")

        # AI コメント（上位3件）
        top3_str = " / ".join(f"{r['keyword']}（+{r['rate']:.0f}%）" for r in rising[:3])
        ai_comment = _generate_trend_comment(
            f"以下のAI関連キーワードが急上昇しています: {top3_str}\n"
            f"なぜ今これらが注目されているか、3〜4文で解説してください。"
        )
        if ai_comment:
            lines.append("## 急上昇の背景")
            lines.append("")
            lines.append(ai_comment)
            lines.append("")
    else:
        lines.append("*急上昇キーワードは検出されませんでした。*")
        lines.append("")
        lines.append(f"（データ蓄積週数: {len(weeks)}週 / 検出には2週以上必要）")
        lines.append("")

    # 週別キーワードサマリー（直近4週）
    recent_weeks = weeks[-4:] if len(weeks) >= 4 else weeks
    if recent_weeks:
        lines.append("## 直近4週のキーワード推移")
        lines.append("")
        lines.append("| 週 | トップキーワード |")
        lines.append("|----|----|")
        for wk in reversed(recent_weeks):
            wk_data = history.get(wk, {})
            top_kws = sorted(wk_data.items(), key=lambda x: -x[1])[:5]
            kw_str = " / ".join(f"{k}（{v}）" for k, v in top_kws) if top_kws else "—"
            lines.append(f"| {wk} | {kw_str} |")
        lines.append("")

    lines.append(f"---\n*本ページは ai-research システムにより自動生成されました。*")
    content = "\n".join(lines)

    if dry_run:
        logger.info(f"[dry-run] 急上昇ページ生成スキップ: {out_path}")
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    logger.info(f"急上昇トレンドページ生成: {out_path} ({len(rising)}件)")
    return out_path


# ---------------------------------------------------------------------------
# declining.md 生成
# ---------------------------------------------------------------------------

def build_declining_page(declining: list[dict], history: dict, dry_run: bool = False) -> Path:
    """knowledge/trends/declining.md を生成する"""
    out_path = KNOWLEDGE_DIR / "trends" / "declining.md"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# 衰退トピック",
        f"\n> 週次更新: {now_str}",
        "",
        "前4週に多く出現していたが、直近4週に50%以上減少したキーワードです。",
        "",
    ]

    if declining:
        lines.append("## 📉 衰退キーワード TOP10")
        lines.append("")
        lines.append("| # | キーワード | 直近4週 | 前4週 | 減少率 |")
        lines.append("|---|-----------|---------|-------|--------|")
        for i, d in enumerate(declining, 1):
            lines.append(
                f"| {i} | **{d['keyword']}** | {d['recent']}件 | {d['prev']}件 | {d['rate']:.0f}% |"
            )
        lines.append("")

        # AI コメント
        top3_str = " / ".join(f"{d['keyword']}（{d['rate']:.0f}%）" for d in declining[:3])
        ai_comment = _generate_trend_comment(
            f"以下のAI関連キーワードが衰退傾向にあります: {top3_str}\n"
            f"なぜ関心が薄れているか、または代替キーワードが台頭しているか、2〜3文で解説してください。"
        )
        if ai_comment:
            lines.append("## 衰退の背景")
            lines.append("")
            lines.append(ai_comment)
            lines.append("")
    else:
        lines.append("*衰退キーワードは検出されませんでした。*")
        lines.append("")

    lines.append(f"---\n*本ページは ai-research システムにより自動生成されました。*")
    content = "\n".join(lines)

    if dry_run:
        logger.info(f"[dry-run] 衰退ページ生成スキップ: {out_path}")
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    logger.info(f"衰退トレンドページ生成: {out_path} ({len(declining)}件)")
    return out_path


# ---------------------------------------------------------------------------
# エントリーポイント
# ---------------------------------------------------------------------------

def run(dry_run: bool = False) -> None:
    history = _load_history()
    weeks = sorted(history.keys())
    logger.info(f"キーワード履歴: {len(weeks)}週分 / {sum(len(v) for v in history.values())}エントリー")

    rising = detect_rising(history)
    declining = detect_declining(history)

    logger.info(f"急上昇: {len(rising)}件 / 衰退: {len(declining)}件")

    build_rising_page(rising, history, dry_run=dry_run)
    build_declining_page(declining, history, dry_run=dry_run)

    logger.info("[完了] トレンドページ生成完了")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="生成AI トレンド分析・ページ生成")
    parser.add_argument("--dry-run", action="store_true", help="ファイル出力せず確認のみ")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
