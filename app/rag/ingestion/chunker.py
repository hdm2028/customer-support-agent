import json

from app.rag.models import (
    ChunkStrategy,
    DocumentChunk,
    RawDocument,
    content_hash_text,
)


CHUNKER_VERSION = "section-char-v1"

CHUNK_STRATEGIES: dict[str, ChunkStrategy] = {
    "pdf_page_section": ChunkStrategy("pdf_page_section", 900, 150),
    "qa_case_short": ChunkStrategy("qa_case_short", 420, 60),
    "sop_step": ChunkStrategy("sop_step", 500, 80),
    "product_manual": ChunkStrategy("product_manual", 520, 80),
    "policy_clause": ChunkStrategy("policy_clause", 650, 100),
    "default": ChunkStrategy("default", 700, 120),
}


def split_markdown_sections(text: str) -> list[tuple[str, str]]:
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


def split_text_with_overlap(
    text: str,
    max_chars: int,
    overlap: int,
) -> list[tuple[int, int, str]]:
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")

    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be non-negative and smaller than max_chars")

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

        start = end - overlap

    return chunks


def choose_chunk_strategy(document: RawDocument) -> ChunkStrategy:
    configured_strategy = document.metadata.get("chunk_strategy")

    if configured_strategy:
        if configured_strategy not in CHUNK_STRATEGIES:
            raise ValueError(f"Unknown chunk strategy: {configured_strategy}")

        return CHUNK_STRATEGIES[configured_strategy]

    source = document.source

    if document.file_type == "pdf":
        return CHUNK_STRATEGIES["pdf_page_section"]

    strategy_rules = (
        ("qa_case_short", ("FAQ", "历史问题", "案例")),
        ("sop_step", ("SOP", "客服")),
        ("product_manual", ("商品说明",)),
        ("policy_clause", ("退款", "退换货", "售后规则", "物流")),
    )

    for strategy_name, keywords in strategy_rules:
        if any(keyword in source for keyword in keywords):
            return CHUNK_STRATEGIES[strategy_name]

    return CHUNK_STRATEGIES["default"]


def _build_chunk_id(
    document: RawDocument,
    *,
    section_index: int,
    chunk_index: int,
    section: str,
    start_char: int,
    end_char: int,
    content_hash: str,
    strategy: ChunkStrategy,
    chunker_version: str,
) -> str:
    identity = json.dumps(
        {
            "document_id": document.document_id,
            "page": document.page,
            "section_index": section_index,
            "section": section,
            "chunk_index": chunk_index,
            "start_char": start_char,
            "end_char": end_char,
            "content_hash": content_hash,
            "strategy": strategy.name,
            "max_chars": strategy.max_chars,
            "overlap": strategy.overlap,
            "chunker_version": chunker_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{document.document_id}:chunk:{content_hash_text(identity)[:24]}"


def chunk_document(
    document: RawDocument,
    *,
    chunker_version: str = CHUNKER_VERSION,
) -> list[DocumentChunk]:
    if not document.text.strip():
        return []

    strategy = choose_chunk_strategy(document)
    sections = (
        split_markdown_sections(document.text)
        if document.file_type == "md"
        else [(document.section or "正文", document.text)]
    )
    chunks = []

    for section_index, (section_title, section_text) in enumerate(sections, start=1):
        split_chunks = split_text_with_overlap(
            section_text,
            max_chars=strategy.max_chars,
            overlap=strategy.overlap,
        )

        for chunk_index, (start, end, chunk_text) in enumerate(split_chunks, start=1):
            chunk_hash = content_hash_text(chunk_text)
            chunk_id = _build_chunk_id(
                document,
                section_index=section_index,
                chunk_index=chunk_index,
                section=section_title,
                start_char=start,
                end_char=end,
                content_hash=chunk_hash,
                strategy=strategy,
                chunker_version=chunker_version,
            )
            chunks.append(
                DocumentChunk(
                    chunk_id=chunk_id,
                    document_id=document.document_id,
                    source=document.source,
                    text=chunk_text,
                    file_type=document.file_type,
                    page=document.page,
                    section=section_title,
                    start_char=start,
                    end_char=end,
                    content_hash=chunk_hash,
                    chunker_version=chunker_version,
                    metadata={
                        **document.metadata,
                        "chunk_strategy": strategy.name,
                        "max_chars": strategy.max_chars,
                        "overlap": strategy.overlap,
                    },
                )
            )

    return chunks


def chunk_documents(
    documents: list[RawDocument],
    *,
    chunker_version: str = CHUNKER_VERSION,
) -> list[DocumentChunk]:
    chunks = []

    for document in documents:
        chunks.extend(chunk_document(document, chunker_version=chunker_version))

    return chunks
