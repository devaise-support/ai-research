"""
stitch_sync.py - Stitch × DESIGN.md 連携パッチスクリプト

publisher.py の _stitch_head() に以下3つの DESIGN.md 改善を適用する：
  1. 日本語フォントスタック（Zenn DESIGN.md）
  2. body line-height: 1.8（Zenn DESIGN.md）
  3. ウォームグレー背景 #f8f7f6（SmartHR DESIGN.md Stone 01）

使用方法:
  python src/stitch_sync.py              # パッチ適用
  python src/stitch_sync.py --check      # 適用状態確認
  python src/stitch_sync.py --dry-run    # 変更プレビュー（未適用）

設定ファイル: config/stitch_config.yaml
"""

import argparse
import logging
import os
import re
import sys
import io
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# ログ設定（Windows cp932対策）
# ---------------------------------------------------------------------------

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
PUBLISHER_PATH = ROOT / "src" / "publisher.py"
CONFIG_PATH = ROOT / "config" / "stitch_config.yaml"


# ---------------------------------------------------------------------------
# 設定読み込み
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """config/stitch_config.yaml を読み込む"""
    if not CONFIG_PATH.exists():
        logger.error(f"設定ファイルが見つかりません: {CONFIG_PATH}")
        sys.exit(1)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# パッチ定義
# ---------------------------------------------------------------------------

JAPANESE_FONTS = [
    "Hiragino Kaku Gothic ProN",
    "Hiragino Sans",
    "Meiryo",
]

# テキスト系フォントロール（日本語フォントを追加する対象）
TEXT_FONT_ROLES = ["body-md", "body-lg", "h1", "h2", "h3", "label-sm"]
# コード系（英語フォントのみ維持）
CODE_FONT_ROLES = ["data-mono"]

WARM_GRAY_BG = "#f8f7f6"       # SmartHR Stone 01
ORIGINAL_BG = "#f9f9ff"         # Stitch デフォルト背景

LINE_HEIGHT_CSS = "body { line-height: 1.8; }\n  p, li, td, th { line-height: 1.8; }"
MATERIAL_SYMBOLS_STYLE = "font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;"


# ---------------------------------------------------------------------------
# 適用状態チェック
# ---------------------------------------------------------------------------

def check_patches(content: str) -> dict[str, bool]:
    """publisher.py の内容から各パッチの適用状態を返す"""
    results = {}

    # 1. 日本語フォントスタック
    results["japanese_fonts"] = "Hiragino Kaku Gothic ProN" in content

    # 2. line-height: 1.8
    results["line_height"] = "line-height: 1.8" in content

    # 3. ウォームグレー背景
    results["warm_gray_bg"] = f'"background": "{WARM_GRAY_BG}"' in content

    return results


def print_check_results(results: dict[str, bool]) -> None:
    """チェック結果を表示する"""
    labels = {
        "japanese_fonts": "日本語フォントスタック（Zenn DESIGN.md）",
        "line_height": "line-height: 1.8（Zenn DESIGN.md）",
        "warm_gray_bg": f"background: {WARM_GRAY_BG}（SmartHR Stone 01）",
    }
    print("\n=== DESIGN_SYNC パッチ適用状態 ===")
    all_ok = True
    for key, applied in results.items():
        icon = "✓" if applied else "✗"
        status = "適用済み" if applied else "未適用"
        print(f"  {icon} {labels[key]}: {status}")
        if not applied:
            all_ok = False
    print()
    if all_ok:
        print("  ✓ すべてのパッチが適用されています")
    else:
        print("  ! 未適用のパッチがあります。`python src/stitch_sync.py` で適用してください")
    print()


# ---------------------------------------------------------------------------
# パッチ適用
# ---------------------------------------------------------------------------

def patch_font_family(content: str) -> tuple[str, int]:
    """fontFamily セクションに日本語フォントスタックを追加する"""
    count = 0
    ja_stack = json_font_list(["Inter"] + JAPANESE_FONTS + ["sans-serif"])
    mono_stack = json_font_list(["Inter", "sans-serif"])

    for role in TEXT_FONT_ROLES:
        # "role": ["Inter","sans-serif"] または ["Inter", "sans-serif"] にマッチ
        pattern = rf'("{role}":\s*)\["Inter"[^\]]*\]'
        replacement = rf'\g<1>{ja_stack}'
        new_content, n = re.subn(pattern, replacement, content)
        if n > 0:
            content = new_content
            count += n
            logger.debug(f"  fontFamily[{role}] を日本語スタックに更新 ({n}箇所)")

    for role in CODE_FONT_ROLES:
        pattern = rf'("{role}":\s*)\["Inter"[^\]]*\]'
        replacement = rf'\g<1>{mono_stack}'
        new_content, n = re.subn(pattern, replacement, content)
        if n > 0:
            content = new_content
            count += n
            logger.debug(f"  fontFamily[{role}] を英語スタックに維持 ({n}箇所)")

    return content, count


def json_font_list(fonts: list[str]) -> str:
    """フォントリストを Python コード内の JSON 配列文字列に変換する"""
    items = ", ".join(f'"{f}"' for f in fonts)
    return f"[{items}]"


def patch_background_color(content: str) -> tuple[str, int]:
    """background カラートークンをウォームグレーに変更する"""
    pattern = rf'"background":\s*"{re.escape(ORIGINAL_BG)}"'
    replacement = f'"background": "{WARM_GRAY_BG}"'
    new_content, n = re.subn(pattern, replacement, content)
    return new_content, n


def patch_line_height(content: str) -> tuple[str, int]:
    """<style> タグに line-height: 1.8 の CSS を追加する"""
    if "line-height: 1.8" in content:
        return content, 0  # 既に適用済み

    # .material-symbols-outlined スタイルの後に追記
    old_style = (
        "<style>.material-symbols-outlined {{ font-variation-settings: "
        "'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24; }}</style>"
    )
    new_style = (
        "<style>"
        ".material-symbols-outlined {{ font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24; }} "
        "body {{ line-height: 1.8; }} "
        "p, li, td, th {{ line-height: 1.8; }}"
        "</style>"
    )

    if old_style in content:
        new_content = content.replace(old_style, new_style, 1)
        return new_content, 1

    # フォールバック：</head> の直前に <style> タグを挿入
    old_head_end = "</head>\"\"\""
    if old_head_end in content:
        insert_style = (
            "<style>body {{ line-height: 1.8; }} p, li, td, th {{ line-height: 1.8; }}</style>\n"
        )
        new_content = content.replace(
            old_head_end,
            insert_style + old_head_end,
            1,
        )
        return new_content, 1

    logger.warning("  line-height パッチの挿入箇所が見つかりませんでした")
    return content, 0


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def apply_patches(dry_run: bool = False) -> None:
    """publisher.py に全パッチを適用する"""
    if not PUBLISHER_PATH.exists():
        logger.error(f"publisher.py が見つかりません: {PUBLISHER_PATH}")
        sys.exit(1)

    original = PUBLISHER_PATH.read_text(encoding="utf-8")
    content = original

    # 事前チェック
    before = check_patches(content)
    already_applied = all(before.values())
    if already_applied:
        print("\n=== DESIGN_SYNC パッチ（適用済み） ===")
        print_check_results(before)
        print("  すべてのパッチは既に適用されています。変更なし。")
        return

    # パッチ適用
    total_changes = 0

    content, n = patch_font_family(content)
    total_changes += n
    if n:
        logger.info(f"  [適用] 日本語フォントスタック: {n}箇所")
    else:
        logger.warning("  [スキップ] 日本語フォントスタック: 対象箇所が見つかりません")

    content, n = patch_background_color(content)
    total_changes += n
    if n:
        logger.info(f"  [適用] ウォームグレー背景 {WARM_GRAY_BG}: {n}箇所")
    else:
        if before["warm_gray_bg"]:
            logger.info(f"  [スキップ] ウォームグレー背景: 適用済み")
        else:
            logger.warning("  [スキップ] ウォームグレー背景: 対象箇所が見つかりません")

    content, n = patch_line_height(content)
    total_changes += n
    if n:
        logger.info("  [適用] line-height: 1.8")
    else:
        if before["line_height"]:
            logger.info("  [スキップ] line-height: 適用済み")
        else:
            logger.warning("  [スキップ] line-height: 対象箇所が見つかりません")

    # 差分表示（dry-run）
    if dry_run:
        print("\n=== ドライラン：変更プレビュー ===")
        _show_diff(original, content)
        print(f"\n  合計 {total_changes} 箇所の変更（未適用）")
        return

    # ファイル書き込み
    if content != original:
        PUBLISHER_PATH.write_text(content, encoding="utf-8")
        after = check_patches(content)
        print("\n=== DESIGN_SYNC パッチ適用完了 ===")
        print_check_results(after)
        logger.info(f"publisher.py を更新しました（{total_changes} 箇所）")
    else:
        print("\n変更なし（パッチが見つかりませんでした）")


def _show_diff(original: str, patched: str) -> None:
    """変更箇所を簡易表示する"""
    orig_lines = original.splitlines()
    new_lines = patched.splitlines()
    changes = 0
    for i, (a, b) in enumerate(zip(orig_lines, new_lines), start=1):
        if a != b:
            print(f"  行 {i}:")
            print(f"    - {a.strip()[:100]}")
            print(f"    + {b.strip()[:100]}")
            changes += 1
            if changes >= 20:
                print("  ... (以下省略)")
                break
    # 長さが違う場合
    if len(new_lines) > len(orig_lines):
        for line in new_lines[len(orig_lines):len(orig_lines) + 5]:
            print(f"    + {line.strip()[:100]}")


# ---------------------------------------------------------------------------
# CLI エントリーポイント
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stitch × DESIGN.md 連携パッチを publisher.py に適用する"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="パッチの適用状態を確認するのみ（変更なし）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="変更内容をプレビューするのみ（未適用）",
    )
    args = parser.parse_args()

    config = load_config()
    logger.info(
        f"Stitch プロジェクト: {config.get('project_id', '（未設定）')}"
    )

    if args.check:
        content = PUBLISHER_PATH.read_text(encoding="utf-8")
        results = check_patches(content)
        print_check_results(results)
        return

    apply_patches(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
