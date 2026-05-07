"""
writer.py - 日次Markdownレポートの生成モジュール

出力先:
  - articles/YYYY/MM/DD.md  日次Markdownレポート
  - scores/YYYY/MM/DD.json  スコアリング結果JSON
  - terms/glossary.md       新出AI用語の追記

単一責任: ファイル出力のみ。収集は fetcher.py、スコアリングは scorer.py が担当。
"""

import json
import logging
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fetcher import Article

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
ARTICLES_DIR = ROOT / "articles"
SCORES_DIR = ROOT / "scores"
GLOSSARY_FILE = ROOT / "terms" / "glossary.md"


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _serialize_article(article: Article) -> dict:
    """Article を JSON シリアライズ可能な dict に変換する"""
    d = asdict(article)
    for key in ("published_at", "fetched_at"):
        if isinstance(d.get(key), datetime):
            d[key] = d[key].isoformat()
    return d


def _stars(score: float) -> str:
    """スコアを星評価文字列に変換する（例: ★★★★☆）"""
    full = int(score)
    half = 1 if (score - full) >= 0.5 else 0
    empty = 5 - full - half
    return "★" * full + ("☆" * half) + "☆" * empty


def _flag(cond: bool, label: str) -> str:
    return f"✅ {label}" if cond else ""


# ---------------------------------------------------------------------------
# 用語集更新
# ---------------------------------------------------------------------------

def update_glossary(articles: list[Article], today: datetime) -> int:
    """
    新出AI用語を terms/glossary.md に追記する。
    重複する用語はスキップ。

    Returns:
        追加した用語数
    """
    # 既存用語を読み込む
    existing_terms: set[str] = set()
    if GLOSSARY_FILE.exists():
        content = GLOSSARY_FILE.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.startswith("## "):
                existing_terms.add(line[3:].strip())

    # 新出用語を収集
    new_entries: list[tuple[str, str, str]] = []  # (term, source_title, url)
    seen_terms: set[str] = set()
    for article in articles:
        for term in article.new_terms:
            term = term.strip()
            if term and term not in existing_terms and term not in seen_terms:
                new_entries.append((term, article.title, article.url))
                seen_terms.add(term)

    if not new_entries:
        return 0

    # 追記
    today_str = today.strftime("%Y-%m-%d")
    lines = []
    for term, src_title, src_url in new_entries:
        lines.append(f"\n## {term}")
        lines.append(f"- 初出日: {today_str}")
        lines.append(f"- 出典: [{src_title[:60]}]({src_url})")
        lines.append("- 定義: （要追記）")

    try:
        with open(GLOSSARY_FILE, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info(f"用語集に {len(new_entries)} 件の新出用語を追記しました")
    except OSError as e:
        logger.warning(f"用語集の更新エラー: {e}")

    return len(new_entries)


# ---------------------------------------------------------------------------
# スコアJSON保存
# ---------------------------------------------------------------------------

def save_scores(articles: list[Article], target_date: datetime) -> None:
    """スコアリング結果を scores/YYYY/MM/DD.json に保存する"""
    score_path = (
        SCORES_DIR
        / target_date.strftime("%Y")
        / target_date.strftime("%m")
        / f"{target_date.strftime('%d')}.json"
    )
    score_path.parent.mkdir(parents=True, exist_ok=True)

    data = [_serialize_article(a) for a in articles]
    try:
        with open(score_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"スコアJSONを保存: {score_path}")
    except OSError as e:
        logger.warning(f"スコアJSON保存エラー: {e}")


# ---------------------------------------------------------------------------
# Markdown 生成
# ---------------------------------------------------------------------------

def _build_article_section(article: Article, rank: int) -> str:
    """1件の記事セクションを生成する"""
    lines = []
    stars = _stars(article.score)
    flags = " / ".join(filter(None, [
        _flag(article.is_primary_source, "一次情報"),
        _flag(article.is_weekly_candidate, "週次候補"),
        _flag(article.is_content_candidate, "コンテンツ候補"),
        _flag(article.is_x_post_candidate, "X投稿候補"),
    ]))

    lang_badge = "🇯🇵" if article.language == "ja" else "🌐"
    source_badge = "🎬" if article.source_type == "youtube" else ("💬" if article.source_type == "reddit" else "📰")

    lines.append(f"### {rank}. {source_badge} [{article.title}]({article.url})")
    lines.append(f"**スコア: {article.score:.1f} {stars}** {lang_badge}")
    lines.append("")
    lines.append(f"- **ソース**: {article.source_name} | **カテゴリ**: {article.category}")
    lines.append(f"- **公開日**: {article.published_at.strftime('%Y-%m-%d')} | **収集**: {article.fetched_at.strftime('%H:%M')}")

    # スコア詳細テーブル
    d = article.score_details
    if d:
        lines.append("")
        lines.append("| 重要度 | 新規性 | ビジネス価値 | 学習価値 | 一次情報度 |")
        lines.append("|--------|--------|------------|----------|----------|")
        lines.append(
            f"| {d.get('importance','-')}/5 | {d.get('novelty','-')}/5 | "
            f"{d.get('business_value','-')}/5 | {d.get('learning_value','-')}/5 | "
            f"{d.get('primary_source_score','-')}/5 |"
        )

    # 要約
    if article.summary_ja:
        lines.append("")
        lines.append(f"**要約**: {article.summary_ja}")
    elif article.summary_raw:
        lines.append("")
        preview = article.summary_raw[:150].replace("\n", " ")
        lines.append(f"**要約**: {preview}...")

    # タグ
    if article.tags:
        tag_str = " ".join(f"`{t}`" for t in article.tags[:7])
        lines.append(f"\n**タグ**: {tag_str}")

    # フラグ
    if flags:
        lines.append(f"\n{flags}")

    # 新出用語
    if article.new_terms:
        terms_str = " / ".join(article.new_terms[:5])
        lines.append(f"\n🆕 **新出用語**: {terms_str}")

    # 対象クライアント
    if article.target_clients:
        clients_str = " / ".join(article.target_clients[:5])
        lines.append(f"\n🏢 **活用業種**: {clients_str}")

    # コンテンツ展開可能性
    cp = article.content_potential
    if cp:
        potential = []
        if cp.get("blog"):
            potential.append("📝ブログ")
        if cp.get("instagram"):
            potential.append("📷Instagram")
        if cp.get("youtube"):
            potential.append("🎬YouTube台本")
        if cp.get("client_report"):
            potential.append("📊クライアントレポート")
        if potential:
            lines.append(f"\n✍️ **コンテンツ展開**: {' / '.join(potential)}")

    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def write_daily(articles: list[Article], target_date: datetime | None = None) -> Path:
    """
    日次Markdownレポートを生成して保存する。

    Args:
        articles: スコアリング済み記事リスト（スコア降順）
        target_date: 出力日付（Noneの場合は今日）

    Returns:
        出力ファイルパス
    """
    if target_date is None:
        target_date = datetime.now(timezone.utc)

    # 出力パス
    out_path = (
        ARTICLES_DIR
        / target_date.strftime("%Y")
        / target_date.strftime("%m")
        / f"{target_date.strftime('%d')}.md"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 統計情報
    total = len(articles)
    primary_count = sum(1 for a in articles if a.is_primary_source)
    primary_rate = round(primary_count / total * 100) if total > 0 else 0
    weekly_count = sum(1 for a in articles if a.is_weekly_candidate)
    monthly_count = sum(1 for a in articles if a.is_monthly_candidate)
    content_count = sum(1 for a in articles if a.is_content_candidate)
    x_post_count = sum(1 for a in articles if a.is_x_post_candidate)

    # 新出用語
    all_terms = []
    for a in articles:
        all_terms.extend(a.new_terms)
    unique_terms = list(dict.fromkeys(all_terms))  # 重複排除・順序保持

    # カテゴリ集計
    category_counts = Counter(a.category for a in articles)
    avg_scores: dict[str, float] = {}
    cat_article_map: dict[str, list[Article]] = {}
    for a in articles:
        cat_article_map.setdefault(a.category, []).append(a)
    for cat, arts in cat_article_map.items():
        avg_scores[cat] = round(sum(a.score for a in arts) / len(arts), 2)

    # ソース種別集計
    source_type_counts = Counter(a.source_type for a in articles)

    date_str = target_date.strftime("%Y年%m月%d日")
    lines = []

    # ヘッダー
    lines.append(f"# 生成AI日次レポート {date_str}")
    lines.append("")
    lines.append("## 📊 サマリー")
    lines.append("")
    lines.append(f"| 項目 | 値 |")
    lines.append(f"|------|-----|")
    lines.append(f"| 収集記事数 | {total}件 |")
    lines.append(f"| 一次情報率 | {primary_count}/{total}件（{primary_rate}%）|")
    lines.append(f"| 週次候補（3.5点以上） | {weekly_count}件 |")
    lines.append(f"| 月次候補（4.0点以上） | {monthly_count}件 |")
    lines.append(f"| コンテンツ候補（4.0点以上） | {content_count}件 |")
    lines.append(f"| X投稿候補（4.5点以上） | {x_post_count}件 |")
    lines.append(f"| 新出AI用語数 | {len(unique_terms)}件 |")
    lines.append("")

    # ソース種別内訳
    lines.append("**収集ソース内訳**:")
    if source_type_counts.get("rss", 0):
        lines.append(f"- 📰 RSS: {source_type_counts['rss']}件")
    if source_type_counts.get("youtube", 0):
        lines.append(f"- 🎬 YouTube: {source_type_counts['youtube']}件")
    if source_type_counts.get("reddit", 0):
        lines.append(f"- 💬 Reddit: {source_type_counts['reddit']}件")
    lines.append("")

    # カテゴリ別内訳
    lines.append("## 📂 カテゴリ別内訳")
    lines.append("")
    lines.append("| カテゴリ | 件数 | 平均スコア |")
    lines.append("|----------|------|-----------|")
    for cat, cnt in sorted(category_counts.items(), key=lambda x: -x[1]):
        avg = avg_scores.get(cat, 0)
        lines.append(f"| {cat} | {cnt}件 | {avg:.1f} |")
    lines.append("")

    # 新出用語
    if unique_terms:
        lines.append("## 🆕 新出AI用語")
        lines.append("")
        for term in unique_terms[:15]:
            # 出典記事を探す
            source_article = next(
                (a for a in articles if term in a.new_terms), None
            )
            if source_article:
                lines.append(f"- **{term}**: [{source_article.source_name}]({source_article.url})")
            else:
                lines.append(f"- **{term}**")
        lines.append("")

    # 記事一覧（スコア降順）
    lines.append("## 📰 記事一覧（スコア降順）")
    lines.append("")
    for i, article in enumerate(articles, 1):
        lines.append(_build_article_section(article, i))

    # ハイライト
    lines.append("## 🏆 ハイライト")
    lines.append("")
    if articles:
        top = articles[0]
        lines.append(f"**本日の最高スコア**: [{top.title[:60]}]({top.url}) — {top.score:.1f}点")
    lines.append(f"\n**週次候補合計**: {weekly_count}件")
    lines.append(f"**コンテンツ候補合計**: {content_count}件")
    if unique_terms:
        lines.append(f"**新出用語**: {' / '.join(unique_terms[:5])}")
    lines.append("")
    lines.append(f"*生成日時: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")

    content = "\n".join(lines)

    try:
        out_path.write_text(content, encoding="utf-8")
        logger.info(f"日次レポートを出力: {out_path}")
    except OSError as e:
        logger.warning(f"日次レポートの書き込みエラー: {e}")

    # スコアJSONを保存
    save_scores(articles, target_date)

    # 用語集を更新
    new_term_count = update_glossary(articles, target_date)
    if new_term_count > 0:
        logger.info(f"用語集に {new_term_count} 件の新出用語を追加しました")

    return out_path
