"""
content_generator.py - コンテンツ生成モジュール

機能:
  - スコア4.0以上（is_content_candidate=True）の記事を対象に以下を生成
    - ブログ記事（1500-2000字）: content/blog/YYYY-MM-DD-{id}.md
    - Instagram投稿（300字+ハッシュタグ30個）: content/instagram/YYYY-MM-DD-{id}.txt
    - YouTube台本（5分尺）: content/youtube/YYYY-MM-DD-{id}.md
    - クライアントレポート: content/client_reports/{industry}/YYYY-MM-DD-{id}.md
    - X（Twitter）投稿文: content/x_posts/YYYY-MM-DD-{id}.txt

  - APIキー未設定時はテンプレートベースの静的コンテンツを生成
  - CLAUDE_API_KEY → claude-sonnet-4-5 優先
  - GEMINI_API_KEY → gemini-2.0-flash フォールバック

単一責任: コンテンツ生成のみ。収集・スコアリングは別モジュールが担当。
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# ログ設定（Windows cp932対策）
# ---------------------------------------------------------------------------

import io

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
CONTENT_DIR = ROOT / "content"
CONFIG_DIR = ROOT / "config"


# ---------------------------------------------------------------------------
# スコアJSONからの記事読み込み
# ---------------------------------------------------------------------------

def load_articles_for_date(target_date: date) -> list[dict]:
    """指定日のスコアJSONから記事を読み込む"""
    json_path = SCORES_DIR / str(target_date.year) / f"{target_date.month:02d}" / f"{target_date.day:02d}.json"
    if not json_path.exists():
        logger.warning(f"スコアJSONが見つかりません: {json_path}")
        return []
    try:
        with open(json_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"スコアJSONの読み込みエラー: {e}")
        return []


def filter_content_candidates(articles: list[dict]) -> list[dict]:
    """is_content_candidate=True の記事のみ抽出"""
    return [a for a in articles if a.get("is_content_candidate", False)]


# ---------------------------------------------------------------------------
# クライアントプロフィール読み込み
# ---------------------------------------------------------------------------

def load_client_profiles() -> dict:
    path = CONFIG_DIR / "client_profiles.yaml"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data.get("profiles", {})
    except Exception as e:
        logger.warning(f"client_profiles.yaml の読み込みエラー: {e}")
        return {}


# ---------------------------------------------------------------------------
# AI API クライアント
# ---------------------------------------------------------------------------

class ContentAIClient:
    """コンテンツ生成用AIクライアント（Claude優先 → Gemini フォールバック）"""

    def __init__(self):
        self.claude_key = os.environ.get("CLAUDE_API_KEY", "")
        self.gemini_key = os.environ.get("GEMINI_API_KEY", "")
        self._claude_client = None
        self._gemini_model = None
        self._mode = "mock"

        if self.claude_key:
            try:
                import anthropic
                self._claude_client = anthropic.Anthropic(api_key=self.claude_key)
                self._mode = "claude"
                logger.info("コンテンツ生成: Claude API（claude-sonnet-4-5）を使用")
            except Exception as e:
                logger.warning(f"Claude APIの初期化失敗: {e}")

        if self._mode == "mock" and self.gemini_key:
            try:
                import google.generativeai as genai
                genai.configure(api_key=self.gemini_key)
                self._gemini_model = genai.GenerativeModel("gemini-2.0-flash")
                self._mode = "gemini"
                logger.info("コンテンツ生成: Gemini API（gemini-2.0-flash）を使用")
            except Exception as e:
                logger.warning(f"Gemini APIの初期化失敗: {e}")

        if self._mode == "mock":
            logger.info("コンテンツ生成: APIキー未設定のためテンプレートモードを使用")

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        """AIにテキストを生成させる。失敗時は空文字を返す"""
        if self._mode == "claude":
            return self._call_claude(system_prompt, user_prompt)
        elif self._mode == "gemini":
            return self._call_gemini(system_prompt, user_prompt)
        return ""

    def _call_claude(self, system_prompt: str, user_prompt: str) -> str:
        try:
            response = self._claude_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=2048,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            time.sleep(0.5)
            return response.content[0].text
        except Exception as e:
            logger.warning(f"Claude API呼び出し失敗: {e}")
            return ""

    def _call_gemini(self, system_prompt: str, user_prompt: str) -> str:
        try:
            prompt = f"{system_prompt}\n\n{user_prompt}"
            response = self._gemini_model.generate_content(prompt)
            time.sleep(0.5)
            return response.text
        except Exception as e:
            logger.warning(f"Gemini API呼び出し失敗: {e}")
            return ""

    @property
    def is_ai_available(self) -> bool:
        return self._mode != "mock"


# ---------------------------------------------------------------------------
# コンテンツ生成（各種）
# ---------------------------------------------------------------------------

def _safe_str(value, default: str = "") -> str:
    """None や非文字列を安全に文字列化する"""
    if value is None:
        return default
    if isinstance(value, list):
        return " / ".join(str(v) for v in value)
    return str(value)


def generate_blog(article: dict, client: ContentAIClient) -> str:
    """ブログ記事を生成する（1500-2000字 Markdown）"""
    title = _safe_str(article.get("title"))
    source = _safe_str(article.get("source_name"))
    category = _safe_str(article.get("category"))
    summary = _safe_str(article.get("summary_raw"))
    url = _safe_str(article.get("url"))
    pub = _safe_str(article.get("published_at", ""))[:10]
    tags = _safe_str(article.get("tags", []))

    if client.is_ai_available:
        system_prompt = (
            "あなたは生成AIトレンドに精通した日本語ブログライターです。\n"
            "以下の記事情報を元に、日本のビジネスパーソン向けのブログ記事を作成してください。\n\n"
            "【要件】\n"
            "- 文字数：1500〜2000字\n"
            "- 構成：導入（なぜ重要か）→ 内容解説 → ビジネスへの示唆 → まとめ\n"
            "- 語調：ですます調、専門用語には簡単な補足説明を付ける\n"
            "- SEO：タイトルに主要キーワードを含める\n"
            "- 出力形式：Markdownで # タイトル から始める"
        )
        user_prompt = (
            f"【記事情報】\n"
            f"タイトル: {title}\n"
            f"ソース: {source}\n"
            f"公開日: {pub}\n"
            f"カテゴリ: {category}\n"
            f"要約: {summary}\n"
            f"タグ: {tags}\n\n"
            f"上記をもとに、1500〜2000字のブログ記事をMarkdown形式で作成してください。"
        )
        result = client.generate(system_prompt, user_prompt)
        if result:
            return result

    # フォールバック：テンプレートベース
    return (
        f"# {title}\n\n"
        f"> **カテゴリ**: {category} | **出典**: {source} | **公開日**: {pub}\n\n"
        f"## はじめに\n\n"
        f"{source}より、{category}に関する重要な情報をお届けします。\n\n"
        f"## 内容\n\n"
        f"{summary}\n\n"
        f"## ビジネスへの示唆\n\n"
        f"このトピックは今後のAI活用において重要な意味を持ちます。\n"
        f"詳細は元記事をご確認ください。\n\n"
        f"**出典**: [{title}]({url})\n"
        f"**タグ**: {tags}\n\n"
        f"---\n*本記事は ai-research システムにより自動生成されました。*\n"
    )


def generate_instagram(article: dict, client: ContentAIClient) -> str:
    """Instagram投稿文を生成する（本文300字以内＋ハッシュタグ30個）"""
    title = _safe_str(article.get("title"))
    category = _safe_str(article.get("category"))
    summary = _safe_str(article.get("summary_raw"))
    tags = article.get("tags", [])

    if client.is_ai_available:
        system_prompt = (
            "あなたはInstagramで生成AIトレンドを発信するSNSクリエイターです。\n"
            "ビジネス活用に関心のある30〜40代向けに、わかりやすく実用的な投稿文を作成してください。\n\n"
            "【要件】\n"
            "- 本文：300字以内（改行を多用して読みやすく）\n"
            "- ハッシュタグ：30個（日本語・英語混在）\n"
            "- 構成：キャッチ一文 → 内容3〜5点 → アクション促進\n"
            "- 絵文字：適切に使用（1投稿5〜10個程度）\n"
            "- 出力形式：本文とハッシュタグを空行で区切る"
        )
        user_prompt = (
            f"【記事情報】\n"
            f"タイトル: {title}\n"
            f"カテゴリ: {category}\n"
            f"要約: {summary}\n"
            f"タグ: {_safe_str(tags)}\n\n"
            f"上記をもとに、Instagram投稿文（本文300字以内＋ハッシュタグ30個）を作成してください。"
        )
        result = client.generate(system_prompt, user_prompt)
        if result:
            return result

    # フォールバック：テンプレートベース
    tag_str = " ".join(f"#{t.replace(' ', '')}" for t in tags[:5]) if tags else "#生成AI"
    return (
        f"【AI最新情報🤖】\n\n"
        f"{title}\n\n"
        f"{summary[:150]}...\n\n"
        f"詳細はプロフィールのリンクからご確認ください💡\n\n"
        f"#生成AI #AIビジネス #DX推進 #AI活用 #テクノロジー\n"
        f"#ChatGPT #人工知能 #機械学習 #デジタル化 #業務効率化\n"
        f"#AIツール #最新技術 #ビジネスAI #IT情報 #テックトレンド\n"
        f"#AIニュース #GenerativeAI #LLM #DX #イノベーション\n"
        f"#AI #MachineLearning #DeepLearning #DataScience #Tech\n"
        f"#Startup #ビジネス #経営 #マーケティング {tag_str}"
    )


def generate_youtube_script(article: dict, client: ContentAIClient) -> str:
    """YouTube動画台本を生成する（5分尺 Markdown）"""
    title = _safe_str(article.get("title"))
    category = _safe_str(article.get("category"))
    summary = _safe_str(article.get("summary_raw"))
    tags = _safe_str(article.get("tags", []))

    if client.is_ai_available:
        system_prompt = (
            "あなたはYouTubeでAIビジネス活用を解説するチャンネルの台本ライターです。\n"
            "視聴者はAIに興味があるが専門知識のないビジネスパーソンです。\n\n"
            "【要件】\n"
            "- 尺：5分（約1500〜2000字の話し言葉）\n"
            "- 構成：\n"
            "  00:00 フック（最初の15秒で視聴継続率を上げる問いかけ）\n"
            "  00:15 自己紹介・概要説明\n"
            "  01:00 メインコンテンツ（3つのポイント）\n"
            "  03:30 まとめ・ビジネスへの示唆\n"
            "  04:30 チャンネル登録・次回予告\n"
            "- 語調：話し言葉、「〜ですよね」「〜なんです」など\n"
            "- ト書き：[BGMフェードイン] [画面切り替え] などを適宜挿入\n"
            "- 出力形式：Markdown、各セクションに時間表示"
        )
        user_prompt = (
            f"【記事情報】\n"
            f"タイトル: {title}\n"
            f"カテゴリ: {category}\n"
            f"要約: {summary}\n"
            f"タグ: {tags}\n\n"
            f"上記をもとに、5分尺のYouTube動画台本をMarkdown形式で作成してください。"
        )
        result = client.generate(system_prompt, user_prompt)
        if result:
            return result

    # フォールバック：テンプレートベース
    return (
        f"# YouTube台本: {title}\n\n"
        f"**カテゴリ**: {category} | **推定尺**: 5分\n\n"
        f"---\n\n"
        f"## 00:00 フック\n\n"
        f"[BGMフェードイン]\n\n"
        f"「最近、{category}の分野でこんなことが起きているの、知っていますか？」\n\n"
        f"## 00:15 自己紹介・概要\n\n"
        f"このチャンネルでは、生成AIの最新情報をビジネス活用の視点でお届けしています。\n"
        f"今日は「{title}」についてお話しします。\n\n"
        f"## 01:00 メインコンテンツ\n\n"
        f"[画面切り替え]\n\n"
        f"### ポイント1\n\n{summary[:200]}\n\n"
        f"### ポイント2\n\nこの技術のビジネスへの活用可能性について考えてみましょう。\n\n"
        f"### ポイント3\n\n今後の展望と注意点についてお伝えします。\n\n"
        f"## 03:30 まとめ\n\n"
        f"今日のポイントをまとめると...\n\n"
        f"## 04:30 チャンネル登録・次回予告\n\n"
        f"[エンディングBGM]\n\n"
        f"チャンネル登録・高評価よろしくお願いします！\n"
        f"次回もAIの最新情報をお届けします。\n\n"
        f"---\n*本台本は ai-research システムにより自動生成されました。*\n"
    )


def generate_client_report(article: dict, client: ContentAIClient, industry_key: str, profile: dict) -> str:
    """クライアント向け業種別レポートを生成する"""
    title = _safe_str(article.get("title"))
    source = _safe_str(article.get("source_name"))
    category = _safe_str(article.get("category"))
    summary = _safe_str(article.get("summary_raw"))
    url = _safe_str(article.get("url"))
    pub = _safe_str(article.get("published_at", ""))[:10]

    industry_name = profile.get("name", industry_key)
    genres = " / ".join(profile.get("genres", []))
    language_level = profile.get("language_level", "ビジネス標準語")
    delivery_format = profile.get("delivery_format", "課題・解決策・効果の3段構成")

    if client.is_ai_available:
        system_prompt = (
            f"あなたはAIビジネス活用コンサルタントです。\n"
            f"{industry_name}向けに、最新のAI技術トレンドをビジネス活用の観点でレポートします。\n\n"
            f"【クライアント設定】\n"
            f"業種: {industry_name}\n"
            f"関心領域: {genres}\n"
            f"言語レベル: {language_level}\n"
            f"レポート形式: {delivery_format}\n\n"
            f"【要件】\n"
            f"- 専門用語には必ず平易な説明を付ける\n"
            f"- 「今すぐ使えるアクション」を必ず含める\n"
            f"- コスト感・工数感の目安を記載する\n"
            f"- 自社への置き換えイメージが湧く具体例を使う"
        )
        user_prompt = (
            f"【AI最新情報】\n"
            f"タイトル: {title}\n"
            f"カテゴリ: {category}\n"
            f"要約: {summary}\n"
            f"元記事URL: {url}\n\n"
            f"上記のAI情報を、{industry_name}のクライアント向けレポートとして作成してください。\n\n"
            f"【出力構成】\n"
            f"1. **概要**（3行以内でこのAIトレンドを説明）\n"
            f"2. **{industry_name}への影響**（具体的なシナリオを2〜3点）\n"
            f"3. **今すぐできるアクション**（難易度：易・中・難 の3段階で提示）\n"
            f"4. **導入コスト感**（無料〜月額費用の目安）\n"
            f"5. **注意点・リスク**（1〜2点）\n\n"
            f"Markdown形式で出力してください。"
        )
        result = client.generate(system_prompt, user_prompt)
        if result:
            return result

    # フォールバック：テンプレートベース
    return (
        f"# {industry_name}向け AI活用レポート\n\n"
        f"**記事**: {title}\n"
        f"**出典**: {source} | **公開日**: {pub}\n\n"
        f"---\n\n"
        f"## 1. 概要\n\n"
        f"{summary[:300]}\n\n"
        f"## 2. {industry_name}への影響\n\n"
        f"このAI技術は、{industry_name}における業務効率化・顧客体験向上に\n"
        f"応用できる可能性があります。\n\n"
        f"## 3. 今すぐできるアクション\n\n"
        f"- 【易】元記事を読んで最新動向を把握する\n"
        f"- 【中】類似ツールの無料トライアルを試してみる\n"
        f"- 【難】自社業務への導入可否を専門家に相談する\n\n"
        f"## 4. 導入コスト感\n\n"
        f"無料〜月額数千円から試せるツールが多数あります。\n\n"
        f"## 5. 注意点・リスク\n\n"
        f"- AIの出力は必ず人間が確認・検証する\n"
        f"- 個人情報・機密情報の入力には注意が必要\n\n"
        f"**参考リンク**: [{title}]({url})\n\n"
        f"---\n*本レポートは ai-research システムにより自動生成されました。*\n"
    )


def generate_x_post(article: dict, client: ContentAIClient) -> str:
    """X（Twitter）投稿文を生成する（140字以内）"""
    title = _safe_str(article.get("title"))
    category = _safe_str(article.get("category"))
    summary = _safe_str(article.get("summary_raw"))
    url = _safe_str(article.get("url"))

    if client.is_ai_available:
        system_prompt = (
            "あなたはAI・テクノロジートレンドをXで発信するインフルエンサーです。\n"
            "日本語で情報感度の高いビジネスパーソン向けに投稿文を作成してください。\n\n"
            "【要件】\n"
            "- 文字数：140字以内（日本語、URLは除く）\n"
            "- 構成：インパクトある一文 → 核心内容 → URL\n"
            "- ハッシュタグ：3個まで\n"
            "- 語調：端的・情報価値重視\n"
            "- 出力形式：投稿文のみ（説明不要）"
        )
        user_prompt = (
            f"【記事情報】\n"
            f"タイトル: {title}\n"
            f"カテゴリ: {category}\n"
            f"要約: {summary[:200]}\n\n"
            f"140字以内のX投稿文を作成してください（URLは {url} を末尾に含める）。"
        )
        result = client.generate(system_prompt, user_prompt)
        if result:
            # 長すぎる場合はトリムして URL を必ず付与
            lines = result.strip().split("\n")
            post = lines[0] if lines else result[:100]
            if url not in post:
                post = f"{post[:100]} {url}"
            return post

    # フォールバック：テンプレートベース（URLなしで140字以内）
    short_title = title[:30] + "..." if len(title) > 30 else title
    return f"【AI速報】{short_title}\n{url}\n#生成AI #AIビジネス #DX"


# ---------------------------------------------------------------------------
# ファイル書き出し
# ---------------------------------------------------------------------------

def _write_content(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_filename(target_date: date, article_id: str) -> str:
    return f"{target_date.isoformat()}-{article_id}"


# ---------------------------------------------------------------------------
# メイン生成処理
# ---------------------------------------------------------------------------

def generate_for_date(
    target_date: date,
    generate_blog_flag: bool = True,
    generate_instagram_flag: bool = True,
    generate_youtube_flag: bool = True,
    generate_client_flag: bool = True,
    generate_x_flag: bool = True,
    dry_run: bool = False,
) -> dict:
    """
    指定日のコンテンツ候補記事に対してコンテンツを生成する。

    Returns:
        生成結果サマリー dict
    """
    articles = load_articles_for_date(target_date)
    candidates = filter_content_candidates(articles)
    profiles = load_client_profiles()
    client = ContentAIClient()

    if not candidates:
        logger.info(f"{target_date} のコンテンツ候補記事はありません（スコア4.0以上の記事なし）")
        return {"date": str(target_date), "candidates": 0, "generated": 0}

    logger.info(f"{target_date}: コンテンツ候補 {len(candidates)} 件を処理します")

    generated = 0

    for article in candidates:
        article_id = article.get("id", "unknown")
        filename = _make_filename(target_date, article_id)
        title = article.get("title", "（タイトルなし）")
        logger.info(f"  生成中: [{article_id}] {title[:40]}...")

        # ブログ
        if generate_blog_flag:
            content = generate_blog(article, client)
            path = CONTENT_DIR / "blog" / f"{filename}.md"
            if not dry_run:
                _write_content(path, content)
                logger.info(f"    ブログ: {path.name}")

        # Instagram
        if generate_instagram_flag:
            content = generate_instagram(article, client)
            path = CONTENT_DIR / "instagram" / f"{filename}.txt"
            if not dry_run:
                _write_content(path, content)
                logger.info(f"    Instagram: {path.name}")

        # YouTube台本
        if generate_youtube_flag:
            content = generate_youtube_script(article, client)
            path = CONTENT_DIR / "youtube" / f"{filename}.md"
            if not dry_run:
                _write_content(path, content)
                logger.info(f"    YouTube台本: {path.name}")

        # クライアントレポート（全業種）
        if generate_client_flag and profiles:
            for industry_key, profile in profiles.items():
                content = generate_client_report(article, client, industry_key, profile)
                path = CONTENT_DIR / "client_reports" / industry_key / f"{filename}.md"
                if not dry_run:
                    _write_content(path, content)
            if not dry_run:
                logger.info(f"    クライアントレポート: {len(profiles)} 業種")

        # X投稿文
        if generate_x_flag:
            content = generate_x_post(article, client)
            path = CONTENT_DIR / "x_posts" / f"{filename}.txt"
            if not dry_run:
                _write_content(path, content)
                logger.info(f"    X投稿文: {path.name}")

        generated += 1

    mode_label = "(dry-run)" if dry_run else ""
    logger.info(f"[完了] コンテンツ生成完了{mode_label}: {generated}/{len(candidates)} 件")
    return {"date": str(target_date), "candidates": len(candidates), "generated": generated}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="生成AIニュース コンテンツ自動生成")
    parser.add_argument(
        "--date",
        default=str(date.today()),
        help="対象日 YYYY-MM-DD（デフォルト: 今日）",
    )
    parser.add_argument("--dry-run", action="store_true", help="ファイル出力せずに確認のみ")
    parser.add_argument("--no-blog", action="store_true", help="ブログ生成をスキップ")
    parser.add_argument("--no-instagram", action="store_true", help="Instagram生成をスキップ")
    parser.add_argument("--no-youtube", action="store_true", help="YouTube台本生成をスキップ")
    parser.add_argument("--no-client", action="store_true", help="クライアントレポート生成をスキップ")
    parser.add_argument("--no-x", action="store_true", help="X投稿文生成をスキップ")
    args = parser.parse_args()

    try:
        target_date = date.fromisoformat(args.date)
    except ValueError:
        logger.error(f"日付の形式が不正です: {args.date}（例: 2026-05-07）")
        sys.exit(1)

    result = generate_for_date(
        target_date=target_date,
        generate_blog_flag=not args.no_blog,
        generate_instagram_flag=not args.no_instagram,
        generate_youtube_flag=not args.no_youtube,
        generate_client_flag=not args.no_client,
        generate_x_flag=not args.no_x,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(f"\n[dry-run] {result['date']}: 候補 {result['candidates']} 件 → 生成予定 {result['generated']} 件")


if __name__ == "__main__":
    main()
