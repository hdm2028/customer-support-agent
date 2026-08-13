import textwrap
from pathlib import Path

import fitz


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_ROOT / "data" / "knowledge_sources"
OUTPUT_PATH = PROJECT_ROOT / "data" / "knowledge" / "知识库文档1.pdf"
FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/simsun.ttc"),
    Path("C:/Windows/Fonts/Deng.ttf"),
]

PAGE_WIDTH = 595
PAGE_HEIGHT = 842
MARGIN_X = 56
MARGIN_TOP = 54
MARGIN_BOTTOM = 54
FONT_NAME = "support-kb-cjk"
TITLE_SIZE = 18
H1_SIZE = 15
H2_SIZE = 12
BODY_SIZE = 10.5
LINE_HEIGHT = 16


def find_chinese_font() -> Path:
    for font_path in FONT_CANDIDATES:
        if font_path.exists():
            return font_path

    raise FileNotFoundError(
        "未找到可嵌入的中文字体，请安装微软雅黑、黑体、宋体或等价 CJK 字体。"
    )


def clean_line(line: str) -> tuple[str, str]:
    stripped = line.strip()

    if not stripped:
        return "blank", ""

    if stripped.startswith("# "):
        return "title", stripped[2:].strip()

    if stripped.startswith("## "):
        return "h1", stripped[3:].strip()

    if stripped.startswith("### "):
        return "h2", stripped[4:].strip()

    if stripped.startswith("- "):
        return "bullet", stripped[2:].strip()

    return "body", stripped


def wrap_text(text: str, width: int) -> list[str]:
    return textwrap.wrap(
        text,
        width=width,
        break_long_words=True,
        replace_whitespace=False,
        drop_whitespace=True,
    ) or [""]


def line_style(kind: str) -> tuple[int, int, int, str]:
    if kind == "title":
        return TITLE_SIZE, 24, 18, ""
    if kind == "h1":
        return H1_SIZE, 22, 13, ""
    if kind == "h2":
        return H2_SIZE, 18, 11, ""
    if kind == "bullet":
        return BODY_SIZE, LINE_HEIGHT, 8, "- "
    return BODY_SIZE, LINE_HEIGHT, 8, ""


def add_text(
    page,
    x: float,
    y: float,
    text: str,
    font_size: float,
    font_path: Path,
) -> None:
    page.insert_text(
        (x, y),
        text,
        fontsize=font_size,
        fontname=FONT_NAME,
        fontfile=str(font_path),
        fill=(0.08, 0.08, 0.08),
    )


def render_pdf(source_path: Path, output_path: Path) -> None:
    markdown = source_path.read_text(encoding="utf-8").replace("\u00a0", " ")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    font_path = find_chinese_font()

    document = fitz.open()
    page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    y = MARGIN_TOP
    text_width = 34

    for raw_line in markdown.splitlines():
        kind, text = clean_line(raw_line)

        if kind == "blank":
            y += 8
            continue

        font_size, line_height, wrap_width, prefix = line_style(kind)
        lines = wrap_text(text, text_width if kind in {"body", "bullet"} else wrap_width)

        needed_height = line_height * len(lines) + (8 if kind in {"title", "h1"} else 2)
        if y + needed_height > PAGE_HEIGHT - MARGIN_BOTTOM:
            page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
            y = MARGIN_TOP

        if kind in {"title", "h1", "h2"}:
            y += 4

        for index, wrapped_line in enumerate(lines):
            line_prefix = prefix if index == 0 else "  " if kind == "bullet" else ""
            add_text(
                page,
                MARGIN_X,
                y,
                f"{line_prefix}{wrapped_line}",
                font_size,
                font_path,
            )
            y += line_height

        if kind in {"title", "h1", "h2"}:
            y += 4

    document.save(output_path)
    document.close()


def main() -> None:
    source_files = sorted(SOURCE_DIR.glob("*.md"))

    if not source_files:
        raise FileNotFoundError(f"未找到知识库源文档: {SOURCE_DIR}")

    render_pdf(source_files[0], OUTPUT_PATH)
    print(f"generated: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
