from dataclasses import asdict, dataclass
from pathlib import Path


SUPPORTED_TEXT_SUFFIXES = {".md", ".txt"}
SUPPORTED_PDF_SUFFIXES = {".pdf"}
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


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
    """读取 PDF 文件。安装 pymupdf 后，会按页提取文本并保留页码。"""

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

    for page_index, page in enumerate(pdf, start=1):
        text = page.get_text("text").strip()
        documents.append(
            RawDocument(
                source=file_path.name,
                text=text,
                file_type="pdf",
                page=page_index,
                section=None,
                metadata={"loader": "pymupdf"},
            )
        )

    return documents


def load_image_file(file_path: Path) -> list[RawDocument]:
    """图片 OCR 预留入口，后续可以接 PaddleOCR、Tesseract 或多模态模型。"""

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


def chunk_raw_document(
    document: RawDocument,
    max_chars: int = 700,
    overlap: int = 120,
) -> list[DocumentChunk]:
    """把原始文档切成可以进入检索系统的 DocumentChunk。"""

    if not document.text.strip():
        return []

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
                    metadata=document.metadata or {},
                )
            )

    return chunks


def build_chunks_from_dir(
    directory: Path,
    max_chars: int = 700,
    overlap: int = 120,
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
