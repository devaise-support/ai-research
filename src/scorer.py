"""
scorer.py - 記事のAIスコアリングモジュール

スコアラー優先順位:
  1. CLAUDE_API_KEY が設定されている → ClaudeScorer (claude-haiku-4-5)
     ├ 呼び出し失敗（レート制限・タイムアウト）→ 最大2回リトライ
     └ リトライ後も失敗 → GeminiScorer に自動フォールバック
  2. GEMINI_API_KEY が設定されている → GeminiScorer (gemini-2.0-flash)
  3. どちらも未設定 → MockScorer（ランダムスコアで動作継続）

Phase 6 変更点:
  - FallbackScorer: Claude 呼び出し失敗時に Gemini へ透過的に切り替え
  - リトライロジック: 429/503 等のレート制限に対して指数バックオフ
  - 使用モデルをログに記録（コスト把握）

単一責任: スコアリングのみ。収集は fetcher.py、出力は writer.py が担当。
"""

import json
import logging
import os
import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from random import uniform

import yaml
from dotenv import load_dotenv

from fetcher import Article

load_dotenv()

logger = logging.getLogger(__name__)

# レート制限リトライ設定
_MAX_RETRIES = 2
_RETRY_BACKOFF = [2.0, 5.0]  # 秒（1回目・2回目の待機時間）
_RATE_LIMIT_KEYWORDS = ("rate_limit", "429", "quota", "resource_exhausted", "overloaded")

ROOT = Path(__file__).parent.parent
CONFIG_DIR = ROOT / "config"
SKILLS_DIR = ROOT / "skills"


# ---------------------------------------------------------------------------
# 設定ロード
# ---------------------------------------------------------------------------

def _load_scoring_weights() -> dict:
    path = CONFIG_DIR / "scoring_weights.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_scoring_prompt() -> str:
    path = SKILLS_DIR / "SCORING.md"
    if path.exists():
        text = path.read_text(encoding="utf-8")
        # AIへのプロンプト（システムプロンプト）セクションだけを抽出
        match = re.search(r'```\n(あなたは生成AI.*?)\n```', text, re.DOTALL)
        if match:
            return match.group(1)
    # フォールバック: 最小プロンプト
    return (
        "あなたは生成AI分野の記事評価専門家です。"
        "以下の記事を5つの観点（importance/novelty/business_value/learning_value/primary_source_score）"
        "で各1〜5の整数で評価し、JSONのみで返してください。"
        '{"importance":N,"novelty":N,"business_value":N,"learning_value":N,'
        '"primary_source_score":N,"summary_ja":"...","tags":[],"key_terms":[],'
        '"target_clients":[],"is_primary":true,"primary_reason":"...","category":"...",'
        '"content_potential":{"blog":false,"instagram":false,"youtube":false,"client_report":false}}'
    )


def _build_user_prompt(article: Article) -> str:
    return (
        f"タイトル: {article.title}\n"
        f"ソース: {article.source_name}\n"
        f"URL: {article.url}\n"
        f"言語: {article.language}\n"
        f"公開日: {article.published_at.strftime('%Y-%m-%d')}\n"
        f"要約:\n{article.summary_raw}"
    )


def _parse_score_response(text: str) -> dict | None:
    """JSON応答を安全にパースする。失敗時はNoneを返す。"""
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _default_score_data() -> dict:
    """APIエラー時のデフォルトスコアデータ"""
    return {
        "importance": 3, "novelty": 3, "business_value": 3,
        "learning_value": 3, "primary_source_score": 3,
        "summary_ja": "（スコアリングAPIエラー）",
        "tags": [], "key_terms": [], "target_clients": [],
        "is_primary": False, "primary_reason": "エラーのためデフォルト値を使用",
        "category": "ニュース・動向",
        "content_potential": {
            "blog": False, "instagram": False,
            "youtube": False, "client_report": False
        }
    }


def _normalize_key_terms(raw: list) -> list[dict]:
    """key_terms を [{term, level, definition}] 形式に正規化する（旧フォーマット互換）"""
    result = []
    valid_levels = {"初級", "中級", "上級"}
    for item in raw:
        if isinstance(item, str):
            # 旧フォーマット互換：文字列 → dict に昇格
            term = item.strip()
            if term:
                result.append({"term": term, "level": "中級", "definition": ""})
        elif isinstance(item, dict):
            term = str(item.get("term", "")).strip()
            if not term:
                continue
            level = item.get("level", "中級")
            if level not in valid_levels:
                level = "中級"
            definition = str(item.get("definition", ""))
            result.append({"term": term, "level": level, "definition": definition})
    return result


def _apply_score(article: Article, score_data: dict, weights: dict) -> Article:
    """スコアデータをArticleに適用して重み付きスコアを計算する"""
    score_keys = ["importance", "novelty", "business_value", "learning_value", "primary_source_score"]
    weight_map = weights.get("weights", {})
    category_boost_map = weights.get("category_boost", {})
    thresholds = weights.get("thresholds", {})

    # 各スコア項目を取得（1〜5にクリップ）
    details = {}
    for key in score_keys:
        val = score_data.get(key, 3)
        try:
            details[key] = max(1, min(5, int(val)))
        except (TypeError, ValueError):
            details[key] = 3

    # 加重平均
    raw_score = sum(details[k] * weight_map.get(k, 0.2) for k in score_keys)

    # カテゴリboost（Articleのcategoryを優先、スコアデータのcategoryを次点）
    article_category = article.category or score_data.get("category", "ニュース・動向")
    boost = category_boost_map.get(article_category, 1.0)
    final_score = min(5.0, raw_score * boost)

    article.score = round(final_score, 2)
    article.score_details = details
    article.summary_ja = score_data.get("summary_ja", "")
    article.tags = score_data.get("tags", [])
    article.new_terms = _normalize_key_terms(score_data.get("key_terms", []))
    article.target_clients = score_data.get("target_clients", [])
    article.content_potential = score_data.get("content_potential", {})

    # カテゴリを更新（スコアリングAPIが判定した場合）
    if score_data.get("category"):
        article.category = score_data["category"]

    # 一次情報フラグを更新（スコアリングAPIの判定を優先）
    if score_data.get("is_primary") is not None:
        article.is_primary_source = bool(score_data["is_primary"])

    # フラグ設定
    article.is_weekly_candidate = final_score >= thresholds.get("weekly_candidate", 3.5)
    article.is_monthly_candidate = final_score >= thresholds.get("monthly_candidate", 4.0)
    article.is_content_candidate = final_score >= thresholds.get("content_candidate", 4.0)
    article.is_x_post_candidate = final_score >= thresholds.get("x_post_candidate", 4.5)

    return article


# ---------------------------------------------------------------------------
# 抽象基底クラス
# ---------------------------------------------------------------------------

class BaseScorer(ABC):
    """スコアラーの抽象基底クラス"""

    def __init__(self):
        self.weights = _load_scoring_weights()
        self.system_prompt = _load_scoring_prompt()

    @abstractmethod
    def _call_api(self, user_prompt: str) -> str:
        """APIを呼び出してテキストを返す"""

    def score(self, article: Article) -> Article:
        """
        記事をスコアリングしてArticleを更新して返す。
        レート制限エラーは指数バックオフでリトライ。
        それ以外のエラーはデフォルトスコアを使用して処理を継続する。
        """
        user_prompt = _build_user_prompt(article)
        score_data = None
        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response_text = self._call_api(user_prompt)
                score_data = _parse_score_response(response_text)
                if score_data is None:
                    logger.warning(f"JSONパース失敗 [{article.title[:30]}]: デフォルトスコアを使用")
                    score_data = _default_score_data()
                break  # 成功
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                is_rate_limit = any(kw in err_str for kw in _RATE_LIMIT_KEYWORDS)

                if is_rate_limit and attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF[attempt]
                    logger.warning(
                        f"レート制限エラー [{article.title[:30]}] "
                        f"({attempt+1}/{_MAX_RETRIES}回目): {wait}秒後にリトライ"
                    )
                    time.sleep(wait)
                else:
                    break  # リトライ不要なエラーまたはリトライ上限

        if score_data is None:
            logger.warning(
                f"スコアリングAPIエラー [{article.title[:30]}]: "
                f"{last_error} → デフォルトスコアを使用"
            )
            score_data = _default_score_data()

        time.sleep(0.5)  # API呼び出し間隔
        return _apply_score(article, score_data, self.weights)


# ---------------------------------------------------------------------------
# Claude スコアラー
# ---------------------------------------------------------------------------

class ClaudeScorer(BaseScorer):
    """Claude API を使用するスコアラー"""

    def __init__(self, model: str = "claude-haiku-4-5"):
        super().__init__()
        self.model = model
        try:
            import anthropic
            self.client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
            logger.info(f"ClaudeScorer 初期化完了: モデル={model}")
        except ImportError:
            raise RuntimeError("anthropic パッケージがインストールされていません: pip install anthropic")

    def _call_api(self, user_prompt: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=800,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return response.content[0].text


# ---------------------------------------------------------------------------
# Gemini スコアラー
# ---------------------------------------------------------------------------

class GeminiScorer(BaseScorer):
    """Gemini API を使用するスコアラー"""

    def __init__(self, model: str = "gemini-2.0-flash"):
        super().__init__()
        self.model = model
        try:
            import google.generativeai as genai
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            from google.generativeai.types import GenerationConfig
            self.genai = genai
            self.generation_config = GenerationConfig(
                response_mime_type="application/json",
                max_output_tokens=800,
            )
            logger.info(f"GeminiScorer 初期化完了: モデル={model}")
        except ImportError:
            raise RuntimeError(
                "google-generativeai がインストールされていません: pip install google-generativeai"
            )

    def _call_api(self, user_prompt: str) -> str:
        model = self.genai.GenerativeModel(
            model_name=self.model,
            system_instruction=self.system_prompt,
            generation_config=self.generation_config,
        )
        response = model.generate_content(user_prompt)
        return response.text


# ---------------------------------------------------------------------------
# Mock スコアラー（APIキー未設定時）
# ---------------------------------------------------------------------------

class MockScorer(BaseScorer):
    """APIキー未設定時のモックスコアラー。システムが動作継続できるようにする。"""

    def __init__(self):
        super().__init__()
        logger.warning(
            "APIキー（CLAUDE_API_KEY / GEMINI_API_KEY）が未設定のため"
            "MockScorerを使用します。スコアはランダム値です。"
        )

    def _call_api(self, user_prompt: str) -> str:
        # ランダムなスコアデータをJSONで返す
        data = {
            "importance": round(uniform(2.5, 4.0)),
            "novelty": round(uniform(2.5, 4.0)),
            "business_value": round(uniform(2.5, 4.0)),
            "learning_value": round(uniform(2.5, 4.0)),
            "primary_source_score": round(uniform(2.5, 4.0)),
            "summary_ja": "（APIキー未設定のためモックスコアを使用）",
            "tags": ["mock", "test"],
            "key_terms": [],
            "target_clients": [],
            "is_primary": False,
            "primary_reason": "モックスコア",
            "category": "ニュース・動向",
            "content_potential": {
                "blog": False, "instagram": False,
                "youtube": False, "client_report": False
            }
        }
        return json.dumps(data)


# ---------------------------------------------------------------------------
# フォールバックスコアラー（Claude → Gemini 透過切り替え）
# ---------------------------------------------------------------------------

class FallbackScorer(BaseScorer):
    """
    Claude API を優先使用し、呼び出し失敗時に Gemini へ透過的にフォールバックする。

    Phase 6 追加: 初期化時だけでなく呼び出し単位でも切り替えを行う。
    """

    def __init__(self, primary: BaseScorer, fallback: BaseScorer):
        # 基底クラスの __init__ は呼ばず、scorer/weights は primary から取得
        self.weights = primary.weights
        self.system_prompt = primary.system_prompt
        self._primary = primary
        self._fallback = fallback
        self._using_fallback = False
        logger.info(
            f"FallbackScorer 初期化: {type(primary).__name__} → "
            f"{type(fallback).__name__} フォールバック"
        )

    def _call_api(self, user_prompt: str) -> str:
        """プライマリ呼び出し → 失敗時にフォールバック"""
        if self._using_fallback:
            return self._fallback._call_api(user_prompt)

        try:
            return self._primary._call_api(user_prompt)
        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = any(kw in err_str for kw in _RATE_LIMIT_KEYWORDS)

            if is_rate_limit:
                # レート制限は BaseScorer のリトライに任せる
                raise

            # その他のエラー（認証失敗・モデル廃止等）→ 永続的にフォールバック切り替え
            logger.warning(
                f"Claude API 呼び出し失敗: {e}\n"
                f"→ {type(self._fallback).__name__} に切り替えます"
            )
            self._using_fallback = True
            return self._fallback._call_api(user_prompt)


# ---------------------------------------------------------------------------
# スコアラーファクトリ
# ---------------------------------------------------------------------------

def create_scorer(prefer_claude: bool = False) -> BaseScorer:
    """
    環境変数の設定に応じて適切なスコアラーを返す。

    優先順位:
      CLAUDE_API_KEY あり かつ GEMINI_API_KEY あり  → FallbackScorer(Claude→Gemini)
      CLAUDE_API_KEY あり のみ                       → ClaudeScorer
      GEMINI_API_KEY あり のみ                       → GeminiScorer
      どちらもなし                                   → MockScorer
    """
    claude_key = os.environ.get("CLAUDE_API_KEY", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")

    claude_scorer: BaseScorer | None = None
    gemini_scorer: BaseScorer | None = None

    if claude_key:
        try:
            claude_scorer = ClaudeScorer()
        except Exception as e:
            logger.warning(f"ClaudeScorer 初期化失敗: {e}")

    if gemini_key:
        try:
            gemini_scorer = GeminiScorer()
        except Exception as e:
            logger.warning(f"GeminiScorer 初期化失敗: {e}")

    # 両方あれば FallbackScorer、片方だけなら単体、どちらもなければ Mock
    if claude_scorer and gemini_scorer:
        return FallbackScorer(primary=claude_scorer, fallback=gemini_scorer)
    if claude_scorer:
        return claude_scorer
    if gemini_scorer:
        return gemini_scorer

    return MockScorer()


# ---------------------------------------------------------------------------
# 全記事スコアリング
# ---------------------------------------------------------------------------

def score_all(articles: list[Article], prefer_claude: bool = False) -> list[Article]:
    """
    全記事をスコアリングして返す。
    1件失敗してもスキップして次の記事に進む。

    Args:
        articles: 収集済み記事リスト
        prefer_claude: True の場合 Claude API を優先

    Returns:
        スコアリング済み記事リスト（スコア降順）
    """
    if not articles:
        logger.info("スコアリング対象記事がありません")
        return []

    scorer = create_scorer(prefer_claude=prefer_claude)
    scorer_name = type(scorer).__name__
    logger.info(f"スコアリング開始: {len(articles)}件 / スコアラー={scorer_name}")

    scored = []
    error_count = 0

    for i, article in enumerate(articles, 1):
        try:
            scored_article = scorer.score(article)
            scored.append(scored_article)
            logger.info(
                f"[{i}/{len(articles)}] {article.title[:40]}... "
                f"→ スコア {scored_article.score:.1f}"
            )
        except Exception as e:
            error_count += 1
            logger.warning(f"スコアリングスキップ [{article.title[:30]}]: {e}")
            # エラーでもデフォルトスコアを付けてリストに追加
            article.score = 2.5
            article.summary_ja = "（スコアリングエラー）"
            scored.append(article)

    # スコア降順でソート
    scored.sort(key=lambda a: a.score, reverse=True)

    weekly_count = sum(1 for a in scored if a.is_weekly_candidate)
    content_count = sum(1 for a in scored if a.is_content_candidate)
    logger.info(
        f"スコアリング完了: {len(scored)}件 / エラー {error_count}件 / "
        f"週次候補 {weekly_count}件 / コンテンツ候補 {content_count}件"
    )
    return scored
