from dataclasses import asdict, dataclass
from pathlib import Path
import re


SUPPORTED_TEXT_SUFFIXES = {".md", ".txt"}
SUPPORTED_PDF_SUFFIXES = {".pdf"}
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

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


@dataclass
class RawDocument:
    """刚从文件中解析出来的原始文档，一般还没有切成小块。"""

    source: str
    text: str
    file_type: str
    page: int | None = None
    section: str | None = None
    metadata: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DocumentChunk:
    """进入向量索引的最小知识片段，保留来源、页码、章节等引用信息。"""

    chunk_id: str
    source: str
    text: str
    file_type: str
    page: int | None
    section: str
    start_char: int
    end_char: int
    metadata: dict

    @property
    def citation(self) -> str:
        page_text = f" 第 {self.page} 页" if self.page else ""
        section_text = f" - {self.section}" if self.section else ""
        return f"{self.source}{page_text}{section_text}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["citation"] = self.citation
        return data


def load_text_file(file_path: Path) -> list[RawDocument]:
    """读取 Markdown/TXT 这类纯文本文件。"""

    text = file_path.read_text(encoding="utf-8")

    return [
        RawDocument(
            source=file_path.name,
            text=text,
            file_type=file_path.suffix.lower().lstrip("."),
            page=None,
            section=None,
            metadata={"loader": "text"},
        )
    ]


def load_pdf_file(file_path: Path) -> list[RawDocument]:
    """读取 PDF 文件，按页抽取文本，并尽量识别章节标题用于 citation。"""

    try:
        import fitz
    except ImportError:
        return [
            RawDocument(
                source=file_path.name,
                text="",
                file_type="pdf",
                page=None,
                section=None,
                metadata={
                    "loader": "pymupdf",
                    "status": "missing_dependency",
                    "message": "需要安装 pymupdf 才能解析 PDF：py -3.13 -m pip install pymupdf",
                },
            )
        ]

    documents = []
    pdf = fitz.open(file_path)
    current_top_heading = ""
    current_numbered_heading = ""
    current_section = "正文"

    def clean_text(text: str) -> str:
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
        return text.replace("R A G", "RAG")

    def read_page_lines(page) -> list[tuple[str, float]]:
        lines = []

        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue

            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = clean_text("".join(span.get("text", "") for span in spans))

                if not text:
                    continue

                max_size = max((span.get("size", 0) for span in spans), default=0)
                lines.append((text, max_size))

        return lines

    def is_document_title(text: str, font_size: float) -> bool:
        return font_size >= 20 and len(text) <= 30

    def is_top_heading(text: str, font_size: float) -> bool:
        if TOP_HEADING_RE.match(text):
            return True

        return (
            font_size >= 14
            and len(text) <= 32
            and any(text.endswith(suffix) for suffix in MINOR_HEADING_SUFFIXES)
        )

    def parse_numbered_heading(text: str) -> str | None:
        match = NUMBERED_HEADING_RE.match(text)

        if not match:
            return None

        heading = match.group(1).strip()

        if "：" in heading:
            heading = heading.split("：", 1)[0].strip()

        if ":" in heading:
            heading = heading.split(":", 1)[0].strip()

        return heading

    def is_minor_heading(text: str) -> bool:
        if len(text) > 24:
            return False

        stripped = text.rstrip("：:")
        return any(stripped.endswith(suffix) for suffix in MINOR_HEADING_SUFFIXES)

    def build_section(*parts: str) -> str:
        return " / ".join(part for part in parts if part) or "正文"

    def append_document(page_number: int, section: str, lines: list[str]) -> None:
        text = "\n".join(lines).strip()
        section_tail = section.split(" / ")[-1]

        if len(text) < 10 or text.rstrip("：:") == section_tail.rstrip("：:"):
            return

        documents.append(
            RawDocument(
                source=file_path.name,
                text=text,
                file_type="pdf",
                page=page_number,
                section=section,
                metadata={"loader": "pymupdf", "section_source": "pdf_heading"},
            )
        )

    for page_index, page in enumerate(pdf, start=1):
        page_lines = []

        for line_text, font_size in read_page_lines(page):
            if page_index == 1 and is_document_title(line_text, font_size):
                continue

            numbered_heading = parse_numbered_heading(line_text)

            if is_top_heading(line_text, font_size):
                append_document(page_index, current_section, page_lines)
                page_lines = []
                current_top_heading = line_text.rstrip("：:")
                current_numbered_heading = ""
                current_section = build_section(current_top_heading)
                page_lines.append(line_text)
                continue

            if numbered_heading:
                append_document(page_index, current_section, page_lines)
                page_lines = []
                current_numbered_heading = numbered_heading.rstrip("：:")
                current_section = build_section(
                    current_top_heading,
                    current_numbered_heading,
                )
                page_lines.append(line_text)
                continue

            if is_minor_heading(line_text):
                append_document(page_index, current_section, page_lines)
                page_lines = []
                current_section = build_section(
                    current_top_heading,
                    current_numbered_heading,
                    line_text.rstrip("：:"),
                )
                page_lines.append(line_text)
                continue

            page_lines.append(line_text)

        append_document(page_index, current_section, page_lines)

    return documents


def load_image_file(file_path: Path) -> list[RawDocument]:
    return [
        RawDocument(
            source=file_path.name,
            text="",
            file_type=file_path.suffix.lower().lstrip("."),
            page=None,
            section=None,
            metadata={
                "loader": "ocr_placeholder",
                "status": "not_implemented",
                "message": "图片 OCR 入口已预留，后续可接 PaddleOCR、Tesseract 或多模态模型。",
            },
        )
    ]


def load_document_file(file_path: Path) -> list[RawDocument]:
    """根据文件后缀选择对应 loader。"""

    suffix = file_path.suffix.lower()

    if suffix in SUPPORTED_TEXT_SUFFIXES:
        return load_text_file(file_path)

    if suffix in SUPPORTED_PDF_SUFFIXES:
        return load_pdf_file(file_path)

    if suffix in SUPPORTED_IMAGE_SUFFIXES:
        return load_image_file(file_path)

    return []


def load_documents_from_dir(directory: Path) -> list[RawDocument]:
    """遍历知识库目录，加载所有支持的文件类型。"""

    documents = []

    if not directory.exists():
        return documents

    for file_path in sorted(directory.iterdir()):
        if file_path.is_file():
            documents.extend(load_document_file(file_path))

    return documents


def split_markdown_sections(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题切分章节，避免不同政策条款混在一个 chunk 里。"""

    sections = []
    current_title = "正文"
    current_lines = []

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith("#"):
            if current_lines:
                sections.append((current_title, "\n".join(current_lines).strip()))
                current_lines = []

            current_title = stripped.lstrip("#").strip() or "未命名章节"
            continue

        current_lines.append(line)

    if current_lines:
        sections.append((current_title, "\n".join(current_lines).strip()))

    return [(title, body) for title, body in sections if body]


def split_text_with_overlap(text: str, max_chars: int, overlap: int) -> list[tuple[int, int, str]]:
    """按固定长度切分文本，并保留重叠区域，减少切分边界造成的信息丢失。"""

    chunks = []
    text = text.strip()

    if not text:
        return chunks

    start = 0

    while start < len(text):
        end = min(start + max_chars, len(text))
        chunk_text = text[start:end].strip()

        if chunk_text:
            chunks.append((start, end, chunk_text))

        if end == len(text):
            break

        start = max(end - overlap, start + 1)

    return chunks


def classify_document(source: str) -> dict:
    if any(keyword in source for keyword in ["退款", "退换货", "商品售后规则"]):
        return {
            "knowledge_category": "refund_policy",
            "business_domain": "after_sales",
            "source_type": "policy",
        }

    if any(keyword in source for keyword in ["物流", "配送"]):
        return {
            "knowledge_category": "logistics_policy",
            "business_domain": "fulfillment",
            "source_type": "policy",
        }

    if any(keyword in source for keyword in ["商品说明", "保修"]):
        return {
            "knowledge_category": "product_manual",
            "business_domain": "product_support",
            "source_type": "manual",
        }

    if "客服SOP" in source:
        return {
            "knowledge_category": "customer_sop",
            "business_domain": "customer_service",
            "source_type": "sop",
        }

    if any(keyword in source for keyword in ["FAQ", "历史问题", "案例"]):
        return {
            "knowledge_category": "historical_case",
            "business_domain": "customer_service",
            "source_type": "case",
        }

    return {
        "knowledge_category": "general_policy",
        "business_domain": "customer_service",
        "source_type": "knowledge",
    }


def choose_chunk_strategy(document: RawDocument) -> dict:
    """按文档类型选择 chunk 策略，避免所有知识都被同一种长度切坏。"""

    source = document.source

    if document.file_type == "pdf":
        return {"name": "pdf_page_section", "max_chars": 900, "overlap": 150}

    if any(keyword in source for keyword in ["FAQ", "历史问题", "案例"]):
        return {"name": "qa_case_short", "max_chars": 420, "overlap": 60}

    if any(keyword in source for keyword in ["SOP", "客服"]):
        return {"name": "sop_step", "max_chars": 500, "overlap": 80}

    if "商品说明" in source:
        return {"name": "product_manual", "max_chars": 520, "overlap": 80}

    if any(keyword in source for keyword in ["退款", "退换货", "售后规则", "物流"]):
        return {"name": "policy_clause", "max_chars": 650, "overlap": 100}

    if any(keyword in source for keyword in ["FAQ", "历史问题", "案例"]):
        return {"name": "qa_case_short", "max_chars": 420, "overlap": 60}

    if any(keyword in source for keyword in ["SOP", "客服"]):
        return {"name": "sop_step", "max_chars": 500, "overlap": 80}

    if "商品说明" in source:
        return {"name": "product_manual", "max_chars": 520, "overlap": 80}

    if any(keyword in source for keyword in ["退款", "退换货", "售后规则", "物流"]):
        return {"name": "policy_clause", "max_chars": 650, "overlap": 100}

    return {"name": "default", "max_chars": 700, "overlap": 120}


def chunk_raw_document(
    document: RawDocument,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> list[DocumentChunk]:
    """把原始文档切成可以进入检索系统的 DocumentChunk。"""

    if not document.text.strip():
        return []

    strategy = choose_chunk_strategy(document)
    max_chars = max_chars or strategy["max_chars"]
    overlap = overlap if overlap is not None else strategy["overlap"]
    chunks = []

    if document.file_type == "md":
        sections = split_markdown_sections(document.text)
    else:
        sections = [(document.section or "正文", document.text)]

    for section_index, (section_title, section_text) in enumerate(sections, start=1):
        for chunk_index, (start, end, chunk_text) in enumerate(
            split_text_with_overlap(section_text, max_chars=max_chars, overlap=overlap),
            start=1,
        ):
            chunk_id = (
                f"{document.source}::p{document.page or 0}"
                f"::s{section_index}::c{chunk_index}"
            )
            chunks.append(
                DocumentChunk(
                    chunk_id=chunk_id,
                    source=document.source,
                    text=chunk_text,
                    file_type=document.file_type,
                    page=document.page,
                    section=section_title,
                    start_char=start,
                    end_char=end,
                    metadata={
                        **(document.metadata or {}),
                        **classify_document(document.source),
                        "chunk_strategy": strategy["name"],
                        "max_chars": max_chars,
                        "overlap": overlap,
                    },
                )
            )

    return chunks


def build_chunks_from_dir(
    directory: Path,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> list[DocumentChunk]:
    """文档工程总入口：读取目录中的文件，并统一切成 chunk。"""

    chunks = []

    for document in load_documents_from_dir(directory):
        chunks.extend(
            chunk_raw_document(
                document,
                max_chars=max_chars,
                overlap=overlap,
            )
        )

    return chunks
