"""
aggregator.py - 週次・月次レポート集約モジュール

使い方:
  python src/aggregator.py --weekly           # 今週の週次レポートを生成
  python src/aggregator.py --weekly --week 2026-W18  # 特定週を指定
  python src/aggregator.py --monthly          # 今月の月次レポートを生成
  python src/aggregator.py --monthly --month 2026-05 # 特定月を指定

出力先:
  weekly/YYYY-WNN.md   週次まとめ
  monthly/YYYY-MM.md   月次レポート

単一責任: 集約・レポート生成のみ。トレンド分析は comparator.py が担当。
"""

import argparse
import io
import json
import logging
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

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
WEEKLY_DIR = ROOT / "weekly"
MONTHLY_DIR = ROOT / "monthly"
SCORES_DIR = ROOT / "scores"


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _stars(score: float) -> str:
    full = int(score)
    empty = 5 - full
    return "★" * full + "☆" * empty


def _top_articles(articles: list[dict], n: int = 5) -> list[dict]:
    return sorted(articles, key=lambda a: a.get("score", 0), reverse=True)[:n]


def _primary_rate(articles: list[dict]) -> str:
    total = len(articles)
    if total == 0:
        return "0%"
    primary = sum(1 for a in articles if a.get("is_primary_source"))
    return f"{primary}/{total}件（{round(primary/total*100)}%）"


def _collect_terms(articles: list[dict]) -> list[str]:
    seen: set[str] = set()
    result = []
    for a in articles:
        for term in a.get("new_terms", []):
            if term and term not in seen:
                seen.add(term)
                result.append(term)
    return result


def _category_summary(articles: list[dict]) -> list[tuple[str, int, float]]:
    """(カテゴリ, 件数, 平均スコア) のリストを返す"""
    cat_map: dict[str, list[float]] = {}
    for a in articles:
        cat = a.get("category", "不明")
        cat_map.setdefault(cat, []).append(a.get("score", 0))
    result = []
    for cat, scores in cat_map.items():
        result.append((cat, len(scores), round(sum(scores) / len(scores), 2)))
    result.sort(key=lambda x: -x[1])
    return result


def _format_article_row(article: dict, rank: int) -> str:
    title = article.get("title", "（タイトルなし）")[:60]
    url = article.get("url", "")
    source = article.get("source_name", "")
    score = article.get("score", 0)
    stars = _stars(score)
    pub = article.get("published_at", "")[:10]
    summary = article.get("summary_ja", "") or article.get("summary_raw", "")[:100]
    tags = " ".join(f"`{t}`" for t in article.get("tags", [])[:5] if t not in ("mock", "test"))
    primary = " ✅一次情報" if article.get("is_primary_source") else ""

    lines = [
        f"### {rank}. [{title}]({url})",
        f"**{score:.1f} {stars}** | {source} | {pub}{primary}",
    ]
    if summary:
        lines.append(f"\n{summary}")
    if tags:
        lines.append(f"\n{tags}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# AI による自然言語サマリー（Claude優先 → Geminiフォールバック）
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM_PROMPT = (
    "あなたは生成AIトレンドに精通したアナリストです。"
    "日本のビジネスパーソン向けに、生成AI業界の動向を簡潔かつ的確にまとめてください。"
    "専門用語は使う場合、括弧内に平易な説明を添えてください。"
    "体言止めや箇条書きを避け、自然な文章で記述してください。"
)


def _generate_ai_summary(prompt: str) -> str | None:
    """
    Claude（claude-sonnet-4-5）または Gemini（gemini-2.0-flash）でサマリーを生成する。
    Claude失敗時は Gemini に自動フォールバック。キー未設定時は None。

    Phase 6: システムプロンプト追加・Claude失敗→Gemini透過フォールバック
    """
    if os.environ.get("CLAUDE_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
            response = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=600,
                system=_SUMMARY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            logger.info("AIサマリー生成: claude-sonnet-4-5")
            return response.content[0].text
        except Exception as e:
            logger.warning(f"Claude API サマリー生成エラー: {e} → Geminiにフォールバック")

    if os.environ.get("GEMINI_API_KEY"):
        try:
            import google.generativeai as genai
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            combined = f"{_SUMMARY_SYSTEM_PROMPT}\n\n{prompt}"
            model = genai.GenerativeModel("gemini-2.0-flash")
            response = model.generate_content(combined)
            logger.info("AIサマリー生成: gemini-2.0-flash")
            return response.text
        except Exception as e:
            logger.warning(f"Gemini API サマリー生成エラー: {e}")

    return None


# ---------------------------------------------------------------------------
# 週次レポート生成
# ---------------------------------------------------------------------------

def generate_weekly(year: int, week: int) -> Path:
    """
    指定週の週次まとめ Markdown を生成する。

    Args:
        year: ISO年
        week: ISO週番号

    Returns:
        出力ファイルパス
    """
    from comparator import (
        load_articles_for_week,
        compare_categories,
        detect_new_keywords,
        update_keyword_history,
        build_change_summary,
        detect_rising_trends,
    )

    week_key = f"{year}-W{week:02d}"
    logger.info(f"週次レポート生成開始: {week_key}")

    # 今週・先週の記事を取得
    current_articles = load_articles_for_week(year, week)
    prev_week = week - 1
    prev_year = year
    if prev_week <= 0:
        prev_year -= 1
        prev_week += 52
    prev_articles = load_articles_for_week(prev_year, prev_week)

    if not current_articles:
        logger.warning(f"{week_key} の記事データが見つかりません")

    # キーワード時系列を更新
    update_keyword_history(current_articles, week_key)

    # 分析
    category_changes = compare_categories(current_articles, prev_articles)
    new_keywords = detect_new_keywords(current_articles, reference_week=(year, week))
    rising = detect_rising_trends()
    all_terms = _collect_terms(current_articles)

    # 週の日付範囲
    monday = datetime.fromisocalendar(year, week, 1)
    sunday = monday + timedelta(days=6)
    date_range = f"{monday.strftime('%Y/%m/%d')}（月）〜 {sunday.strftime('%m/%d')}（日）"

    # AIサマリー（任意）
    change_summary_text = build_change_summary(category_changes, new_keywords, rising)
    ai_summary = None
    if current_articles:
        top_titles = "\n".join(
            f"- {a.get('title', '')[:60]}（スコア{a.get('score',0):.1f}）"
            for a in _top_articles(current_articles, 5)
        )
        ai_prompt = (
            f"以下は{week_key}（{date_range}）の生成AI週次まとめです。\n"
            f"トップ記事:\n{top_titles}\n\n"
            f"変化サマリー:\n{change_summary_text}\n\n"
            "上記を踏まえて、この週の生成AI業界の動向を日本語3〜4文で自然にまとめてください。"
        )
        ai_summary = _generate_ai_summary(ai_prompt)

    # Markdown 生成
    lines = []
    lines.append(f"# 週次AIリサーチまとめ {week_key}")
    lines.append(f"\n**期間**: {date_range}")
    lines.append("")

    # サマリー表
    lines.append("## サマリー")
    lines.append("")
    lines.append("| 項目 | 値 |")
    lines.append("|------|-----|")
    lines.append(f"| 総収集件数 | {len(current_articles)}件 |")
    lines.append(f"| 一次情報率 | {_primary_rate(current_articles)} |")
    lines.append(f"| 週次候補（3.5点以上） | {sum(1 for a in current_articles if a.get('is_weekly_candidate'))}件 |")
    lines.append(f"| コンテンツ候補（4.0点以上） | {sum(1 for a in current_articles if a.get('is_content_candidate'))}件 |")
    lines.append(f"| 新出AI用語数 | {len(all_terms)}件 |")
    lines.append(f"| 先週比較データ | {len(prev_articles)}件（先週） |")
    lines.append("")

    # AI自然言語サマリー
    if ai_summary:
        lines.append("## AI分析サマリー")
        lines.append("")
        lines.append(ai_summary)
        lines.append("")
    elif change_summary_text:
        lines.append("## 先週との変化")
        lines.append("")
        lines.append(change_summary_text)
        lines.append("")

    # トップ記事5選
    lines.append("## トップ記事5選")
    lines.append("")
    top5 = _top_articles(current_articles, 5)
    if top5:
        for i, a in enumerate(top5, 1):
            lines.append(_format_article_row(a, i))
            lines.append("")
    else:
        lines.append("*この週の記事データがありません*")
    lines.append("")

    # カテゴリ別トレンド
    lines.append("## カテゴリ別トレンド（先週比）")
    lines.append("")
    lines.append("| カテゴリ | 今週 | 先週 | 増減率 | 方向 |")
    lines.append("|----------|------|------|--------|------|")
    for ch in category_changes:
        arrow = "↑" if ch["direction"] == "up" else ("↓" if ch["direction"] == "down" else "→")
        sign = "+" if ch["rate"] >= 0 else ""
        lines.append(
            f"| {ch['category']} | {ch['current']}件 | {ch['prev']}件 "
            f"| {sign}{ch['rate']:.1f}% | {arrow} |"
        )
    lines.append("")

    # 急上昇キーワード
    if rising:
        lines.append("## 急上昇キーワード")
        lines.append("")
        lines.append("| キーワード | 直近4週 | 前4週 | 増加率 |")
        lines.append("|-----------|---------|-------|--------|")
        for r in rising[:5]:
            lines.append(
                f"| **{r['keyword']}** | {r['recent']}件 | {r['prev']}件 | +{r['rate']:.0f}% |"
            )
        lines.append("")

    # 週間新出AI用語
    if all_terms:
        lines.append("## 週間新出AI用語")
        lines.append("")
        for term in all_terms[:15]:
            lines.append(f"- **{term}**")
        lines.append("")

    # 新規キーワード（過去4週未出現）
    if new_keywords:
        lines.append("## 今週初登場のキーワード")
        lines.append("")
        lines.append("過去4週間に出現していない新しいキーワードです:")
        lines.append("")
        for kw in new_keywords[:10]:
            lines.append(f"- `{kw}`")
        lines.append("")

    # 来週の注目トピック予測
    lines.append("## 来週の注目トピック予測")
    lines.append("")
    if rising:
        rising_kws = " / ".join(f"**{r['keyword']}**" for r in rising[:3])
        lines.append(f"急上昇中の {rising_kws} 関連の動向に引き続き注目。")
    if category_changes:
        top_cat = next((c for c in category_changes if c["direction"] == "up"), None)
        if top_cat:
            lines.append(f"特に **{top_cat['category']}** カテゴリの拡大傾向が続く見込みです。")
    if not rising and not category_changes:
        lines.append("*（トレンドデータ蓄積中）*")
    lines.append("")
    lines.append(f"*生成日時: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")

    # 保存
    out_path = WEEKLY_DIR / f"{week_key}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"週次レポートを出力: {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# 月次レポート生成
# ---------------------------------------------------------------------------

def generate_monthly(year: int, month: int) -> Path:
    """
    指定月の月次レポート Markdown を生成する（Coconala/note販売想定）。

    Args:
        year: 年
        month: 月（1〜12）

    Returns:
        出力ファイルパス
    """
    from comparator import load_articles_for_month, detect_rising_trends

    month_key = f"{year}-{month:02d}"
    logger.info(f"月次レポート生成開始: {month_key}")

    articles = load_articles_for_month(year, month)
    if not articles:
        logger.warning(f"{month_key} の記事データが見つかりません")

    top10 = _top_articles(articles, 10)
    all_terms = _collect_terms(articles)
    cat_summary = _category_summary(articles)
    rising = detect_rising_trends()

    # AIサマリー（任意）
    ai_month_summary = None
    if articles:
        top_titles = "\n".join(
            f"- {a.get('title', '')[:60]}（スコア{a.get('score',0):.1f}）"
            for a in top10[:5]
        )
        ai_prompt = (
            f"{year}年{month}月の生成AI業界の重要トレンドをまとめます。\n"
            f"月間トップ記事:\n{top_titles}\n\n"
            f"急上昇キーワード: {', '.join(r['keyword'] for r in rising[:5])}\n\n"
            "日本のSME（中小企業・個人事業主）向けに、この月の生成AI動向を"
            "分かりやすく日本語4〜5文でまとめてください。"
            "専門用語は平易な言葉で補足してください。"
        )
        ai_month_summary = _generate_ai_summary(ai_prompt)

    # 月名
    month_ja = f"{year}年{month}月"

    lines = []
    lines.append(f"# {month_ja} 生成AIトレンドレポート")
    lines.append("")
    lines.append("> **Coconala / note 販売用月次レポート**")
    lines.append("")

    # 月間サマリー
    lines.append("## 月間サマリー")
    lines.append("")
    lines.append("| 項目 | 値 |")
    lines.append("|------|-----|")
    lines.append(f"| 総収集記事数 | {len(articles)}件 |")
    lines.append(f"| 一次情報率 | {_primary_rate(articles)} |")
    lines.append(f"| 月次候補（4.0点以上） | {sum(1 for a in articles if a.get('is_monthly_candidate'))}件 |")
    lines.append(f"| コンテンツ候補 | {sum(1 for a in articles if a.get('is_content_candidate'))}件 |")
    lines.append(f"| 新出AI用語数 | {len(all_terms)}件 |")
    lines.append("")

    # AI月次サマリー
    if ai_month_summary:
        lines.append("## 今月の総括")
        lines.append("")
        lines.append(ai_month_summary)
        lines.append("")

    # 月間重要ニュースTOP10
    lines.append("## 月間重要ニュース TOP10")
    lines.append("")
    if top10:
        for i, a in enumerate(top10, 1):
            lines.append(_format_article_row(a, i))
            lines.append("")
    else:
        lines.append("*この月の記事データがありません*")
    lines.append("")

    # ジャンル別動向レポート
    lines.append("## ジャンル別動向レポート")
    lines.append("")
    lines.append("| カテゴリ | 件数 | 平均スコア | 動向 |")
    lines.append("|----------|------|-----------|------|")
    for cat, cnt, avg in cat_summary:
        trend = "注目度高" if avg >= 3.8 else ("標準" if avg >= 3.2 else "低調")
        lines.append(f"| {cat} | {cnt}件 | {avg:.1f} | {trend} |")
    lines.append("")

    # 急上昇キーワード
    if rising:
        lines.append("## 急上昇キーワード（前月比）")
        lines.append("")
        for r in rising[:5]:
            lines.append(f"- **{r['keyword']}**: +{r['rate']:.0f}%（{r['recent']}件）")
        lines.append("")

    # 今月の新出AI用語集
    if all_terms:
        lines.append("## 今月の新出AI用語集")
        lines.append("")
        lines.append("今月の記事から初めて登場したAI用語です。")
        lines.append("")
        for term in all_terms[:20]:
            lines.append(f"- **{term}**: （定義は用語集 `terms/glossary.md` を参照）")
        lines.append("")

    # ビジネス活用事例まとめ
    biz_articles = [
        a for a in articles
        if a.get("category") in ("ビジネス活用事例", "ツール・サービス")
        or a.get("target_clients")
    ]
    if biz_articles:
        lines.append("## ビジネス活用事例まとめ")
        lines.append("")
        lines.append("中小企業・個人事業主が活用できる事例です。")
        lines.append("")
        for a in sorted(biz_articles, key=lambda x: x.get("score", 0), reverse=True)[:5]:
            title = a.get("title", "")[:60]
            url = a.get("url", "")
            clients = " / ".join(a.get("target_clients", [])[:3])
            summary = a.get("summary_ja", "") or a.get("summary_raw", "")[:80]
            lines.append(f"**[{title}]({url})**")
            if clients:
                lines.append(f"活用業種: {clients}")
            if summary:
                lines.append(f"{summary}")
            lines.append("")

    # 来月の注目トピック予測
    lines.append("## 来月の注目トピック予測")
    lines.append("")
    if rising:
        for r in rising[:3]:
            lines.append(f"- **{r['keyword']}**: 引き続き注目（直近4週 {r['recent']}件）")
    else:
        lines.append("*（トレンドデータ蓄積中）*")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(f"*本レポートは ai-research システムにより自動生成されました。*")
    lines.append(f"*生成日時: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")

    # 保存
    out_path = MONTHLY_DIR / f"{month_key}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"月次レポートを出力: {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# CLI エントリーポイント
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成AI週次・月次レポート集約システム"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--weekly", action="store_true", help="週次レポートを生成する")
    group.add_argument("--monthly", action="store_true", help="月次レポートを生成する")

    parser.add_argument(
        "--week",
        type=str,
        default=None,
        help="対象週を指定（形式: YYYY-WNN、例: 2026-W19）",
    )
    parser.add_argument(
        "--month",
        type=str,
        default=None,
        help="対象月を指定（形式: YYYY-MM、例: 2026-05）",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    if args.weekly:
        if args.week:
            try:
                y, w = args.week.split("-W")
                year, week = int(y), int(w)
            except (ValueError, AttributeError):
                logger.error(f"週の形式が正しくありません: {args.week}（例: 2026-W19）")
                return 1
        else:
            iso = now.isocalendar()
            year, week = iso.year, iso.week
            logger.info(f"週の指定なし。今週（{year}-W{week:02d}）を使用します")
        generate_weekly(year, week)

    elif args.monthly:
        if args.month:
            try:
                year, month = map(int, args.month.split("-"))
            except (ValueError, AttributeError):
                logger.error(f"月の形式が正しくありません: {args.month}（例: 2026-05）")
                return 1
        else:
            year, month = now.year, now.month
            logger.info(f"月の指定なし。今月（{year}-{month:02d}）を使用します")
        generate_monthly(year, month)

    return 0


if __name__ == "__main__":
    sys.exit(main())
