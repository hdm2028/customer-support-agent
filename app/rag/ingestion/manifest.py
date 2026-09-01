import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.rag.models import content_hash_text


MANIFEST_SCHEMA_VERSION = 2


@dataclass
class DocumentManifestEntry:
    document_id: str
    source: str
    content_hash: str
    file_type: str
    metadata: dict = field(default_factory=dict)
    chunk_ids: list[str] = field(default_factory=list)
    chunk_hashes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DocumentManifestEntry":
        return cls(
            document_id=data["document_id"],
            source=data["source"],
            content_hash=data["content_hash"],
            file_type=data["file_type"],
            metadata=dict(data.get("metadata", {})),
            chunk_ids=list(data.get("chunk_ids", [])),
            chunk_hashes=dict(data.get("chunk_hashes", {})),
        )


@dataclass
class KnowledgeManifest:
    schema_version: int
    generated_at: str
    kb_version: str
    parser_version: str
    chunker_version: str
    documents: dict[str, DocumentManifestEntry] = field(default_factory=dict)
    unsupported_sources: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "kb_version": self.kb_version,
            "parser_version": self.parser_version,
            "chunker_version": self.chunker_version,
            "documents": {
                document_id: document.to_dict()
                for document_id, document in sorted(self.documents.items())
            },
            "unsupported_sources": self.unsupported_sources,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeManifest":
        if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                "Unsupported knowledge manifest schema: "
                f"{data.get('schema_version')!r}"
            )

        return cls(
            schema_version=data["schema_version"],
            generated_at=data["generated_at"],
            kb_version=data["kb_version"],
            parser_version=data["parser_version"],
            chunker_version=data["chunker_version"],
            documents={
                document_id: DocumentManifestEntry.from_dict(document)
                for document_id, document in data.get("documents", {}).items()
            },
            unsupported_sources=list(data.get("unsupported_sources", [])),
        )


@dataclass
class KnowledgeDiff:
    added: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_kb_version(
    documents: dict[str, DocumentManifestEntry],
    *,
    parser_version: str,
    chunker_version: str,
) -> str:
    payload = {
        "parser_version": parser_version,
        "chunker_version": chunker_version,
        "documents": [
            {
                "document_id": document.document_id,
                "content_hash": document.content_hash,
                "metadata": document.metadata,
                "chunk_hashes": document.chunk_hashes,
            }
            for document in sorted(documents.values(), key=lambda item: item.document_id)
        ],
    }
    return "kb_" + content_hash_text(_canonical_json(payload))[:24]


def create_manifest(
    documents: dict[str, DocumentManifestEntry],
    *,
    parser_version: str,
    chunker_version: str,
    unsupported_sources: list[dict] | None = None,
) -> KnowledgeManifest:
    return KnowledgeManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        kb_version=build_kb_version(
            documents,
            parser_version=parser_version,
            chunker_version=chunker_version,
        ),
        parser_version=parser_version,
        chunker_version=chunker_version,
        documents=documents,
        unsupported_sources=unsupported_sources or [],
    )


def diff_manifests(
    previous: KnowledgeManifest | None,
    current: KnowledgeManifest,
) -> KnowledgeDiff:
    if previous is None:
        return KnowledgeDiff(
            added=sorted(document.source for document in current.documents.values())
        )

    previous_ids = set(previous.documents)
    current_ids = set(current.documents)
    pipeline_changed = bool(
        previous.parser_version != current.parser_version
        or previous.chunker_version != current.chunker_version
    )
    added = [current.documents[item].source for item in current_ids - previous_ids]
    deleted = [previous.documents[item].source for item in previous_ids - current_ids]
    modified = []
    unchanged = []

    for document_id in previous_ids & current_ids:
        old_document = previous.documents[document_id]
        new_document = current.documents[document_id]
        changed = bool(
            pipeline_changed
            or old_document.content_hash != new_document.content_hash
            or old_document.metadata != new_document.metadata
            or old_document.chunk_ids != new_document.chunk_ids
            or old_document.chunk_hashes != new_document.chunk_hashes
        )
        target = modified if changed else unchanged
        target.append(new_document.source)

    return KnowledgeDiff(
        added=sorted(added),
        modified=sorted(modified),
        deleted=sorted(deleted),
        unchanged=sorted(unchanged),
    )


def load_manifest(path: Path) -> KnowledgeManifest | None:
    if not path.exists():
        return None

    data = json.loads(path.read_text(encoding="utf-8"))

    if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return None

    return KnowledgeManifest.from_dict(data)


def save_manifest(manifest: KnowledgeManifest, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)
