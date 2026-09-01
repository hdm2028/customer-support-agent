from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path


DOCUMENT_ID_VERSION = "knowledge-document-v1"


def content_hash_bytes(content: bytes) -> str:
    return sha256(content).hexdigest()


def content_hash_text(text: str) -> str:
    return content_hash_bytes(text.encode("utf-8"))


def normalize_source(source: str) -> str:
    normalized = source.replace("\\", "/")

    while normalized.startswith("./"):
        normalized = normalized[2:]

    return normalized.strip("/")


def build_document_id(source: str) -> str:
    normalized = normalize_source(source)
    identity = f"{DOCUMENT_ID_VERSION}\0{normalized}"
    return "doc_" + content_hash_text(identity)[:24]


@dataclass(frozen=True)
class KnowledgeSource:
    path: Path
    source: str
    file_type: str
    content_hash: str
    size: int

    @property
    def document_id(self) -> str:
        return build_document_id(self.source)


@dataclass(frozen=True)
class UnsupportedKnowledgeSource:
    path: Path
    source: str
    file_type: str
    reason: str = "unsupported_file_type"


@dataclass
class FileDiscoveryResult:
    sources: list[KnowledgeSource] = field(default_factory=list)
    unsupported: list[UnsupportedKnowledgeSource] = field(default_factory=list)

    @property
    def signature(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (source.document_id, source.content_hash)
            for source in self.sources
        )


@dataclass
class RawDocument:
    document_id: str
    source: str
    text: str
    file_type: str
    content_hash: str
    page: int | None = None
    section: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DocumentChunk:
    chunk_id: str
    document_id: str
    source: str
    text: str
    file_type: str
    page: int | None
    section: str
    start_char: int
    end_char: int
    content_hash: str
    chunker_version: str
    metadata: dict = field(default_factory=dict)

    @property
    def citation(self) -> str:
        page_text = f" 第 {self.page} 页" if self.page else ""
        section_text = f" - {self.section}" if self.section else ""
        return f"{self.source}{page_text}{section_text}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["citation"] = self.citation
        return data


@dataclass(frozen=True)
class ChunkStrategy:
    name: str
    max_chars: int
    overlap: int
