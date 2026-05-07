"""
knowledge_builder.py - 総合知識ベース構築エンジン

機能:
  1. トピック別時系列まとめ（knowledge/topics/）
     - scores/ 全JSONをトピック設定に基づき振り分け
     - 各トピックページに「最新5件」「主要キーワード」を記載
     - Claude / Gemini でトピックサマリーを自然言語生成

  2. 技術比較ページ（knowledge/comparisons/）
     - 対象エンティティを含む記事を自動収集
     - テーブル形式の比較ページを生成・更新

  3. 業界インパクトスコア（knowledge/industry-impact/）
     - 業種キーワードにマッチした記事のスコア推移を記録

実行方法:
  python src/knowledge_builder.py --all
  python src/knowledge_builder.py --topics
  python src/knowledge_builder.py --compare
  python src/knowledge_builder.py --industry
  python src/knowledge_builder.py --topic llm-models

単一責任: 知識ベース構築のみ。トレンド分析は trend_analyzer.py が担当。
"""

import argparse
import io
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml
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
SCORES_DIR = ROOT / "scores"
KNOWLEDGE_DIR = ROOT / "knowledge"
CONFIG_DIR = ROOT / "config"


# ---------------------------------------------------------------------------
# 設定ロード
# ---------------------------------------------------------------------------

def _load_topic_mapping() -> dict:
    path = CONFIG_DIR / "topic_mapping.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# スコアJSONからの全記事読み込み
# ---------------------------------------------------------------------------

def load_all_articles() -> list[dict]:
    """scores/ 配下の全記事を読み込む"""
    articles = []
    for json_path in sorted(SCORES_DIR.rglob("*.json")):
        if json_path.name == "seen_articles.json":
            continue
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                articles.extend(data)
        except (json.JSONDecodeError, OSError):
            continue
    logger.info(f"全記事読み込み完了: {len(articles)}件")
    return articles


# ---------------------------------------------------------------------------
# トピック振り分けロジック
# ---------------------------------------------------------------------------

def _matches_topic(article: dict, topic_cfg: dict) -> bool:
    """記事がトピック設定にマッチするか判定する"""
    category = article.get("category", "")
    tags = [t.lower() for t in article.get("tags", [])]
    title = article.get("title", "").lower()
    summary = article.get("summary_raw", "").lower()
    summary_ja = article.get("summary_ja", "").lower()
    lang = article.get("language", "")
    text_blob = " ".join([title, summary, summary_ja])

    # 言語フィルター
    if topic_cfg.get("language_filter") and lang != topic_cfg["language_filter"]:
        return False

    # カテゴリマッチ
    for cat in topic_cfg.get("categories", []):
        if category == cat:
            return True

    # キーワードマッチ（タイトル・要約・タグ）
    for kw in topic_cfg.get("keywords", []):
        kw_lower = kw.lower()
        if kw_lower in text_blob or kw_lower in tags:
            return True

    return False


def classify_articles(articles: list[dict], mapping: dict) -> dict[str, list[dict]]:
    """
    全記事をトピックに振り分ける。
    1記事が複数トピックにマッチしてよい。

    Returns:
        {topic_key: [article, ...]}
    """
    result: dict[str, list[dict]] = defaultdict(list)
    topics = mapping.get("topics", {})

    for article in articles:
        for key, cfg in topics.items():
            if _matches_topic(article, cfg):
                result[key].append(article)

    for key, arts in result.items():
        logger.info(f"  トピック [{key}]: {len(arts)}件マッチ")

    return dict(result)


# ---------------------------------------------------------------------------
# AI サマリー生成
# ---------------------------------------------------------------------------

_KNOWLEDGE_SYSTEM_PROMPT = (
    "あなたは生成AI分野のトレンドリサーチャーです。"
    "収集した記事データをもとに、日本のビジネスパーソン向けに"
    "分かりやすく簡潔にトピックをまとめてください。"
    "専門用語には括弧内で平易な説明を補足してください。"
)


def _generate_summary(prompt: str, max_tokens: int = 500) -> str | None:
    """Claude（claude-sonnet-4-5）→ Gemini（gemini-2.0-flash）の順でサマリー生成"""
    if os.environ.get("CLAUDE_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
            resp = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=max_tokens,
                system=_KNOWLEDGE_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            time.sleep(0.5)
            return resp.content[0].text
        except Exception as e:
            logger.warning(f"Claude API エラー: {e} → Geminiにフォールバック")

    if os.environ.get("GEMINI_API_KEY"):
        try:
            import google.generativeai as genai
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            model = genai.GenerativeModel("gemini-2.0-flash")
            resp = model.generate_content(f"{_KNOWLEDGE_SYSTEM_PROMPT}\n\n{prompt}")
            time.sleep(0.5)
            return resp.text
        except Exception as e:
            logger.warning(f"Gemini API エラー: {e}")

    return None


# ---------------------------------------------------------------------------
# 1. トピック別時系列まとめ
# ---------------------------------------------------------------------------

def _top_keywords(articles: list[dict], n: int = 10) -> list[str]:
    """記事リストから頻出タグを抽出"""
    counter: Counter = Counter()
    for a in articles:
        for tag in a.get("tags", []):
            if tag and tag not in ("mock", "test") and len(tag) > 1:
                counter[tag] += 1
    return [kw for kw, _ in counter.most_common(n)]


def _format_article_brief(article: dict, rank: int) -> str:
    title = article.get("title", "（タイトルなし）")[:55]
    url = article.get("url", "#")
    source = article.get("source_name", "")
    score = article.get("score", 0)
    pub = article.get("published_at", "")[:10]
    summary = article.get("summary_ja") or article.get("summary_raw", "")[:120]
    stars = "★" * min(int(score), 5) + "☆" * max(0, 5 - int(score))
    tags = " ".join(f"`{t}`" for t in article.get("tags", [])[:4] if t not in ("mock", "test"))

    lines = [
        f"### {rank}. [{title}]({url})",
        f"**{score:.1f} {stars}** | {source} | {pub}",
    ]
    if summary:
        lines.append(f"\n{summary}")
    if tags:
        lines.append(f"\n{tags}")
    return "\n".join(lines)


def build_topic_page(
    topic_key: str,
    topic_cfg: dict,
    articles: list[dict],
    dry_run: bool = False,
) -> Path:
    """トピック別まとめページを生成する"""
    topic_name = topic_cfg.get("name", topic_key)
    out_rel = topic_cfg.get("output_file", f"topics/{topic_key}.md")
    # output_file が "knowledge/" 始まりの場合は除去
    out_rel = re.sub(r"^knowledge/", "", out_rel)
    out_path = KNOWLEDGE_DIR / out_rel

    sorted_arts = sorted(articles, key=lambda a: a.get("score", 0), reverse=True)
    top5 = sorted_arts[:5]
    keywords = _top_keywords(articles)

    # AI サマリー（上位5件のタイトルと要約から生成）
    ai_summary = None
    if articles:
        top_info = "\n".join(
            f"- {a.get('title','')[:60]}（スコア{a.get('score',0):.1f}）"
            for a in top5
        )
        kw_str = " / ".join(keywords[:8])
        prompt = (
            f"【{topic_name}】トピックの最新動向まとめ\n\n"
            f"収集記事数: {len(articles)}件\n"
            f"主要キーワード: {kw_str}\n"
            f"トップ記事:\n{top_info}\n\n"
            f"上記のデータから、{topic_name}分野の最新動向を"
            f"日本のビジネスパーソン向けに3〜4文で簡潔にまとめてください。"
        )
        ai_summary = _generate_summary(prompt)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# {topic_name}",
        f"\n> 最終更新: {now_str} | 累計記事数: {len(articles)}件",
        "",
        "## 概要",
        "",
    ]

    if ai_summary:
        lines.append(ai_summary)
    else:
        lines.append(f"*{topic_name}に関する情報を自動収集・整理しています。*")
        lines.append(f"*（APIキー設定でAI生成サマリーが有効になります）*")
    lines.append("")

    # 主要キーワード
    if keywords:
        lines.append("## 主要キーワード")
        lines.append("")
        lines.append(" / ".join(f"`{kw}`" for kw in keywords))
        lines.append("")

    # 最新ニュース5件
    lines.append("## 最新ニュース TOP5")
    lines.append("")
    if top5:
        for i, a in enumerate(top5, 1):
            lines.append(_format_article_brief(a, i))
            lines.append("")
    else:
        lines.append("*記事データなし（scores/ にデータが蓄積されると自動更新されます）*")
    lines.append("")

    # 月別記事数（時系列）
    monthly: Counter = Counter()
    for a in articles:
        pub = a.get("published_at", "")[:7]  # YYYY-MM
        if pub:
            monthly[pub] += 1

    if monthly:
        lines.append("## 月別収集件数（時系列）")
        lines.append("")
        lines.append("| 月 | 件数 |")
        lines.append("|-----|------|")
        for ym in sorted(monthly.keys(), reverse=True)[:12]:
            lines.append(f"| {ym} | {monthly[ym]}件 |")
        lines.append("")

    lines.append(f"---\n*本ページは ai-research システムにより自動生成されました。*")

    content = "\n".join(lines)

    if dry_run:
        logger.info(f"[dry-run] トピックページ生成スキップ: {out_path}")
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    logger.info(f"トピックページ生成: {out_path} ({len(articles)}件)")
    return out_path


def build_all_topics(articles: list[dict], mapping: dict, dry_run: bool = False) -> list[Path]:
    """全トピックページを生成する"""
    classified = classify_articles(articles, mapping)
    paths = []
    for topic_key, topic_cfg in mapping.get("topics", {}).items():
        topic_articles = classified.get(topic_key, [])
        path = build_topic_page(topic_key, topic_cfg, topic_articles, dry_run=dry_run)
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# 2. 技術比較ページ
# ---------------------------------------------------------------------------

def _articles_mentioning(articles: list[dict], entities: list[str]) -> dict[str, list[dict]]:
    """エンティティごとに言及している記事を返す"""
    result: dict[str, list[dict]] = defaultdict(list)
    for article in articles:
        text = " ".join([
            article.get("title", ""),
            article.get("summary_raw", ""),
            article.get("summary_ja", ""),
            " ".join(article.get("tags", [])),
        ]).lower()
        for entity in entities:
            if entity.lower() in text:
                result[entity].append(article)
    return dict(result)


def build_comparison_page(
    comp_key: str,
    comp_cfg: dict,
    articles: list[dict],
    dry_run: bool = False,
) -> Path:
    """技術比較ページを生成する"""
    comp_name = comp_cfg.get("name", comp_key)
    out_rel = re.sub(r"^knowledge/", "", comp_cfg.get("output_file", f"comparisons/{comp_key}.md"))
    out_path = KNOWLEDGE_DIR / out_rel

    entities = comp_cfg.get("track_entities", [])
    axes = comp_cfg.get("comparison_axes", ["特徴", "コスト", "性能"])
    entity_articles = _articles_mentioning(articles, entities)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# {comp_name}",
        f"\n> 最終更新: {now_str}",
        "",
        "> ⚠️ 本ページはスコアリング記事から自動生成されたものです。"
        "最新情報は公式ドキュメントをご確認ください。",
        "",
    ]

    # AI による比較サマリー
    entity_summary = "\n".join(
        f"- {e}: {len(entity_articles.get(e, []))}件の記事"
        for e in entities
    )
    ai_summary = _generate_summary(
        f"【{comp_name}】の最新比較情報\n\n"
        f"言及記事数:\n{entity_summary}\n\n"
        f"以下の軸で簡潔に比較してください: {', '.join(axes)}\n"
        f"各エンティティの現在の特徴を2〜3文で説明してください。",
        max_tokens=600,
    )

    if ai_summary:
        lines.append("## AI生成比較サマリー")
        lines.append("")
        lines.append(ai_summary)
        lines.append("")

    # 言及記事数テーブル
    lines.append("## 記事言及数（収集データより）")
    lines.append("")
    lines.append("| モデル/ツール | 言及記事数 | 最新記事 |")
    lines.append("|--------------|-----------|---------|")
    for entity in entities:
        arts = entity_articles.get(entity, [])
        latest_pub = ""
        if arts:
            latest = max(arts, key=lambda a: a.get("published_at", ""))
            latest_pub = latest.get("published_at", "")[:10]
        lines.append(f"| **{entity}** | {len(arts)}件 | {latest_pub} |")
    lines.append("")

    # 各エンティティの最新記事
    for entity in entities:
        arts = sorted(
            entity_articles.get(entity, []),
            key=lambda a: a.get("score", 0),
            reverse=True,
        )[:3]
        if arts:
            lines.append(f"### {entity} の最新記事")
            lines.append("")
            for a in arts:
                title = a.get("title", "")[:55]
                url = a.get("url", "#")
                pub = a.get("published_at", "")[:10]
                score = a.get("score", 0)
                lines.append(f"- [{title}]({url}) ★{score:.1f} | {pub}")
            lines.append("")

    lines.append(f"---\n*本ページは ai-research システムにより自動生成されました。*")

    content = "\n".join(lines)

    if dry_run:
        logger.info(f"[dry-run] 比較ページ生成スキップ: {out_path}")
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    logger.info(f"比較ページ生成: {out_path}")
    return out_path


def build_all_comparisons(articles: list[dict], mapping: dict, dry_run: bool = False) -> list[Path]:
    """全比較ページを生成する"""
    paths = []
    for comp_key, comp_cfg in mapping.get("comparisons", {}).items():
        path = build_comparison_page(comp_key, comp_cfg, articles, dry_run=dry_run)
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# 3. 業界インパクトスコア
# ---------------------------------------------------------------------------

def build_industry_impact_page(
    industry_key: str,
    industry_cfg: dict,
    articles: list[dict],
    dry_run: bool = False,
) -> Path:
    """業界インパクトスコアページを生成する"""
    industry_name = industry_cfg.get("name", industry_key)
    out_rel = re.sub(r"^knowledge/", "", industry_cfg.get("output_file", f"industry-impact/{industry_key}.md"))
    out_path = KNOWLEDGE_DIR / out_rel
    keywords = industry_cfg.get("keywords", [])

    # 業種関連記事を抽出
    related = []
    for article in articles:
        text = " ".join([
            article.get("title", ""),
            article.get("summary_raw", ""),
            article.get("summary_ja", ""),
            " ".join(article.get("target_clients", [])),
        ]).lower()
        if any(kw.lower() in text for kw in keywords):
            related.append(article)

    # 月別平均スコア集計
    monthly_scores: dict[str, list[float]] = defaultdict(list)
    for a in related:
        pub = a.get("published_at", "")[:7]
        score = a.get("score", 0)
        if pub and score:
            monthly_scores[pub].append(score)

    monthly_avg = {
        ym: round(sum(scores) / len(scores), 2)
        for ym, scores in monthly_scores.items()
    }

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    top3 = sorted(related, key=lambda a: a.get("score", 0), reverse=True)[:3]

    # AI サマリー
    ai_summary = None
    if related:
        top_info = "\n".join(f"- {a.get('title','')[:55]}（スコア{a.get('score',0):.1f}）" for a in top3)
        ai_summary = _generate_summary(
            f"【{industry_name}】へのAIインパクト分析\n\n"
            f"関連記事数: {len(related)}件\n"
            f"直近の注目記事:\n{top_info}\n\n"
            f"{industry_name}において、これらのAI技術がどのような影響を与えるか"
            f"3〜4文でまとめてください。",
            max_tokens=400,
        )

    lines = [
        f"# {industry_name} — AI活用インパクトスコア",
        f"\n> 最終更新: {now_str} | 関連記事数: {len(related)}件",
        "",
    ]

    if ai_summary:
        lines.append("## AIインパクト概要")
        lines.append("")
        lines.append(ai_summary)
        lines.append("")

    # 月別スコア推移（Mermaid グラフ）
    if monthly_avg:
        lines.append("## 月別インパクトスコア推移")
        lines.append("")
        lines.append("```mermaid")
        lines.append("xychart-beta")
        lines.append(f'  title "{industry_name} 月別インパクトスコア"')
        sorted_months = sorted(monthly_avg.keys())[-12:]
        lines.append(f"  x-axis [{', '.join(sorted_months)}]")
        lines.append(f"  y-axis \"スコア\" 0 --> 5")
        score_vals = [str(monthly_avg.get(m, 0)) for m in sorted_months]
        lines.append(f"  bar [{', '.join(score_vals)}]")
        lines.append("```")
        lines.append("")

        # テーブル形式でも表示
        lines.append("| 月 | 関連記事数 | 平均スコア |")
        lines.append("|-----|-----------|-----------|")
        for ym in sorted(monthly_avg.keys(), reverse=True)[:12]:
            cnt = len(monthly_scores.get(ym, []))
            avg = monthly_avg[ym]
            lines.append(f"| {ym} | {cnt}件 | {avg:.2f} |")
        lines.append("")

    # 今月注目の技術TOP3
    if top3:
        lines.append(f"## 注目記事 TOP3")
        lines.append("")
        for i, a in enumerate(top3, 1):
            lines.append(_format_article_brief(a, i))
            lines.append("")

    lines.append(f"---\n*本ページは ai-research システムにより自動生成されました。*")

    content = "\n".join(lines)

    if dry_run:
        logger.info(f"[dry-run] 業界インパクトページ生成スキップ: {out_path}")
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    logger.info(f"業界インパクトページ生成: {out_path} ({len(related)}件)")
    return out_path


def build_all_industry_impact(articles: list[dict], mapping: dict, dry_run: bool = False) -> list[Path]:
    """全業界インパクトページを生成する"""
    paths = []
    for industry_key, industry_cfg in mapping.get("industry_impact", {}).items():
        path = build_industry_impact_page(industry_key, industry_cfg, articles, dry_run=dry_run)
        paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# 知識ベース総合インデックス
# ---------------------------------------------------------------------------

def build_index(mapping: dict, dry_run: bool = False) -> Path:
    """knowledge/index.md を生成する"""
    out_path = KNOWLEDGE_DIR / "index.md"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# 生成AI知識ベース インデックス",
        f"\n> 最終更新: {now_str}",
        "",
        "ai-research システムが自動収集・整理した生成AI分野の総合知識ベースです。",
        "",
        "## トピック別まとめ",
        "",
    ]

    for key, cfg in mapping.get("topics", {}).items():
        out_rel = re.sub(r"^knowledge/", "", cfg.get("output_file", f"topics/{key}.md"))
        lines.append(f"- [{cfg.get('name', key)}]({out_rel})")
    lines.append("")

    lines.append("## 技術比較ページ")
    lines.append("")
    for key, cfg in mapping.get("comparisons", {}).items():
        out_rel = re.sub(r"^knowledge/", "", cfg.get("output_file", f"comparisons/{key}.md"))
        lines.append(f"- [{cfg.get('name', key)}]({out_rel})")
    lines.append("")

    lines.append("## 業界インパクトスコア")
    lines.append("")
    for key, cfg in mapping.get("industry_impact", {}).items():
        out_rel = re.sub(r"^knowledge/", "", cfg.get("output_file", f"industry-impact/{key}.md"))
        lines.append(f"- [{cfg.get('name', key)}]({out_rel})")
    lines.append("")

    lines.append("## トレンド分析")
    lines.append("")
    lines.append("- [急上昇トピック](trends/rising.md)")
    lines.append("- [衰退トピック](trends/declining.md)")
    lines.append("")
    lines.append(f"---\n*本インデックスは ai-research システムにより自動生成されました。*")

    content = "\n".join(lines)

    if dry_run:
        logger.info(f"[dry-run] インデックス生成スキップ: {out_path}")
        return out_path

    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    logger.info(f"知識ベースインデックス生成: {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="生成AI 総合知識ベース構築ツール")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="全コンテンツを生成・更新")
    group.add_argument("--topics", action="store_true", help="トピック別まとめのみ")
    group.add_argument("--compare", action="store_true", help="技術比較ページのみ")
    group.add_argument("--industry", action="store_true", help="業界インパクトページのみ")
    group.add_argument("--topic", type=str, metavar="KEY", help="特定トピックのみ（例: llm-models）")
    parser.add_argument("--dry-run", action="store_true", help="ファイル出力せず確認のみ")
    args = parser.parse_args()

    mapping = _load_topic_mapping()
    articles = load_all_articles()

    if args.all or args.topics:
        if args.topic:
            topic_cfg = mapping.get("topics", {}).get(args.topic)
            if not topic_cfg:
                logger.error(f"トピックキーが見つかりません: {args.topic}")
                return 1
            topic_arts = [
                a for a in articles if _matches_topic(a, topic_cfg)
            ]
            build_topic_page(args.topic, topic_cfg, topic_arts, dry_run=args.dry_run)
        else:
            build_all_topics(articles, mapping, dry_run=args.dry_run)

    if args.all or args.compare:
        build_all_comparisons(articles, mapping, dry_run=args.dry_run)

    if args.all or args.industry:
        build_all_industry_impact(articles, mapping, dry_run=args.dry_run)

    if args.all:
        build_index(mapping, dry_run=args.dry_run)

    logger.info("[完了] 知識ベース構築完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
