import re
from collections.abc import Callable

from app.rag.ingestion.discovery import (
    SUPPORTED_IMAGE_SUFFIXES,
    SUPPORTED_PDF_SUFFIXES,
    SUPPORTED_TEXT_SUFFIXES,
)
from app.rag.ingestion.metadata import parse_markdown_front_matter
from app.rag.models import KnowledgeSource, RawDocument, content_hash_text


PARSER_VERSION = "knowledge-parser-v1"
TOP_HEADING_RE = re.compile(r"^[一二三四五六七八九十]+、\S+")
NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)+\s+[^：:。]+)(?:[：:].*)?$")
MINOR_HEADING_SUFFIXES = (
    "政策",
    "范围",
    "要求",
    "流程",
    "场景",
    "规则",
    "控制",
    "话术",
    "模板",
    "示例",
)


class UnsupportedKnowledgeSourceError(ValueError):
    pass


def _raw_document(
    source: KnowledgeSource,
    text: str,
    *,
    page: int | None = None,
    section: str | None = None,
    metadata: dict | None = None,
) -> RawDocument:
    return RawDocument(
        document_id=source.document_id,
        source=source.source,
        text=text,
        file_type=source.file_type,
        page=page,
        section=section,
        content_hash=content_hash_text(text),
        metadata=metadata or {},
    )


def load_text_document(source: KnowledgeSource) -> list[RawDocument]:
    text = source.path.read_text(encoding="utf-8")
    metadata = {"loader": "text"}

    if source.file_type == "md":
        text, front_matter = parse_markdown_front_matter(text)

        if front_matter:
            metadata["front_matter"] = front_matter

    if not text.strip():
        metadata["status"] = "empty"

    return [_raw_document(source, text, metadata=metadata)]


def _clean_pdf_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
    return text.replace("R A G", "RAG")


def _read_page_lines(page) -> list[tuple[str, float]]:
    lines = []

    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = _clean_pdf_text(
                "".join(span.get("text", "") for span in spans)
            )

            if text:
                lines.append(
                    (
                        text,
                        max((span.get("size", 0) for span in spans), default=0),
                    )
                )

    return lines


def _is_document_title(text: str, font_size: float) -> bool:
    return font_size >= 20 and len(text) <= 30


def _is_top_heading(text: str, font_size: float) -> bool:
    return bool(
        TOP_HEADING_RE.match(text)
        or (
            font_size >= 14
            and len(text) <= 32
            and any(text.endswith(suffix) for suffix in MINOR_HEADING_SUFFIXES)
        )
    )


def _parse_numbered_heading(text: str) -> str | None:
    match = NUMBERED_HEADING_RE.match(text)

    if not match:
        return None

    heading = match.group(1).strip()

    if "：" in heading:
        heading = heading.split("：", 1)[0].strip()

    if ":" in heading:
        heading = heading.split(":", 1)[0].strip()

    return heading


def _is_minor_heading(text: str) -> bool:
    if len(text) > 24:
        return False

    stripped = text.rstrip("：:")
    return any(stripped.endswith(suffix) for suffix in MINOR_HEADING_SUFFIXES)


def _build_section(*parts: str) -> str:
    return " / ".join(part for part in parts if part) or "正文"


def load_pdf_document(source: KnowledgeSource) -> list[RawDocument]:
    try:
        import fitz
    except ImportError:
        return [
            _raw_document(
                source,
                "",
                metadata={
                    "loader": "pymupdf",
                    "status": "missing_dependency",
                    "message": "需要安装 pymupdf 才能解析 PDF。",
                },
            )
        ]

    documents = []
    pdf = fitz.open(source.path)
    current_top_heading = ""
    current_numbered_heading = ""
    current_section = "正文"

    def append_document(
        page_number: int,
        section: str,
        lines: list[str],
    ) -> None:
        text = "\n".join(lines).strip()
        section_tail = section.split(" / ")[-1]

        if len(text) < 10 or text.rstrip("：:") == section_tail.rstrip("：:"):
            return

        documents.append(
            _raw_document(
                source,
                text,
                page=page_number,
                section=section,
                metadata={
                    "loader": "pymupdf",
                    "section_source": "pdf_heading",
                },
            )
        )

    try:
        for page_index, page in enumerate(pdf, start=1):
            page_lines = []

            for line_text, font_size in _read_page_lines(page):
                if page_index == 1 and _is_document_title(line_text, font_size):
                    continue

                numbered_heading = _parse_numbered_heading(line_text)

                if _is_top_heading(line_text, font_size):
                    append_document(page_index, current_section, page_lines)
                    page_lines = []
                    current_top_heading = line_text.rstrip("：:")
                    current_numbered_heading = ""
                    current_section = _build_section(current_top_heading)
                    page_lines.append(line_text)
                    continue

                if numbered_heading:
                    append_document(page_index, current_section, page_lines)
                    page_lines = []
                    current_numbered_heading = numbered_heading.rstrip("：:")
                    current_section = _build_section(
                        current_top_heading,
                        current_numbered_heading,
                    )
                    page_lines.append(line_text)
                    continue

                if _is_minor_heading(line_text):
                    append_document(page_index, current_section, page_lines)
                    page_lines = []
                    current_section = _build_section(
                        current_top_heading,
                        current_numbered_heading,
                        line_text.rstrip("：:"),
                    )
                    page_lines.append(line_text)
                    continue

                page_lines.append(line_text)

            append_document(page_index, current_section, page_lines)
    finally:
        pdf.close()

    if documents:
        return documents

    return [
        _raw_document(
            source,
            "",
            metadata={"loader": "pymupdf", "status": "empty"},
        )
    ]


def load_image_placeholder(source: KnowledgeSource) -> list[RawDocument]:
    return [
        _raw_document(
            source,
            "",
            metadata={
                "loader": "image_placeholder",
                "status": "not_implemented",
                "message": "图片 OCR 尚未实现，当前不会生成知识 chunk。",
            },
        )
    ]


Loader = Callable[[KnowledgeSource], list[RawDocument]]
LOADER_BY_SUFFIX: dict[str, Loader] = {
    **{suffix: load_text_document for suffix in SUPPORTED_TEXT_SUFFIXES},
    **{suffix: load_pdf_document for suffix in SUPPORTED_PDF_SUFFIXES},
    **{suffix: load_image_placeholder for suffix in SUPPORTED_IMAGE_SUFFIXES},
}


def load_source(source: KnowledgeSource) -> list[RawDocument]:
    suffix = "." + source.file_type.lower().lstrip(".")
    loader = LOADER_BY_SUFFIX.get(suffix)

    if loader is None:
        raise UnsupportedKnowledgeSourceError(
            f"No loader registered for knowledge source: {source.source}"
        )

    return loader(source)
