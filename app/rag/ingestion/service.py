from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import KNOWLEDGE_DIR, KNOWLEDGE_MANIFEST_PATH
from app.rag.ingestion.chunker import CHUNKER_VERSION, chunk_documents
from app.rag.ingestion.discovery import discover_files
from app.rag.ingestion.loader import PARSER_VERSION, load_source
from app.rag.ingestion.manifest import (
    DocumentManifestEntry,
    KnowledgeDiff,
    KnowledgeManifest,
    create_manifest,
    diff_manifests,
    load_manifest,
    save_manifest,
)
from app.rag.ingestion.metadata import MetadataEnricher
from app.rag.models import DocumentChunk, FileDiscoveryResult, RawDocument


@dataclass
class KnowledgeBuildResult:
    discovery: FileDiscoveryResult
    documents: list[RawDocument]
    chunks: list[DocumentChunk]
    manifest: KnowledgeManifest
    diff: KnowledgeDiff
    empty_sources: list[str] = field(default_factory=list)


class KnowledgeIngestionService:
    def __init__(
        self,
        knowledge_dir: Path = KNOWLEDGE_DIR,
        *,
        manifest_path: Path = KNOWLEDGE_MANIFEST_PATH,
        recursive: bool = True,
        explicit_metadata: dict[str, dict] | None = None,
        path_metadata: dict[str, dict] | None = None,
        parser_version: str = PARSER_VERSION,
        chunker_version: str = CHUNKER_VERSION,
        chunk_strategy: str | None = None,
    ) -> None:
        self.knowledge_dir = knowledge_dir
        self.manifest_path = manifest_path
        self.recursive = recursive
        self.parser_version = parser_version
        self.chunker_version = chunker_version
        self.chunk_strategy = chunk_strategy
        self.metadata_enricher = MetadataEnricher(
            explicit_metadata=explicit_metadata,
            path_metadata=path_metadata,
        )

    def scan(self) -> FileDiscoveryResult:
        return discover_files(self.knowledge_dir, recursive=self.recursive)

    def build(
        self,
        *,
        save: bool = False,
        compare_with_stored: bool = True,
        discovery: FileDiscoveryResult | None = None,
    ) -> KnowledgeBuildResult:
        discovery = discovery or self.scan()
        documents = []
        chunks = []
        manifest_documents = {}
        empty_sources = []

        for source in discovery.sources:
            loaded_documents = [
                self.metadata_enricher.enrich(document)
                for document in load_source(source)
            ]
            source_chunks = chunk_documents(
                loaded_documents,
                chunker_version=self.chunker_version,
                chunk_strategy=self.chunk_strategy,
            )
            documents.extend(loaded_documents)
            chunks.extend(source_chunks)

            if not source_chunks:
                empty_sources.append(source.source)

            document_metadata = (
                dict(loaded_documents[0].metadata)
                if loaded_documents
                else {"status": "empty"}
            )
            manifest_documents[source.document_id] = DocumentManifestEntry(
                document_id=source.document_id,
                source=source.source,
                content_hash=source.content_hash,
                file_type=source.file_type,
                metadata=document_metadata,
                chunk_ids=[chunk.chunk_id for chunk in source_chunks],
                chunk_hashes={
                    chunk.chunk_id: chunk.content_hash
                    for chunk in source_chunks
                },
            )

        manifest = create_manifest(
            manifest_documents,
            parser_version=self.parser_version,
            chunker_version=self.chunker_version,
            unsupported_sources=[
                {
                    "source": item.source,
                    "file_type": item.file_type,
                    "reason": item.reason,
                }
                for item in discovery.unsupported
            ],
        )
        previous_manifest = (
            load_manifest(self.manifest_path)
            if compare_with_stored
            else None
        )
        knowledge_diff = diff_manifests(previous_manifest, manifest)

        if save:
            save_manifest(manifest, self.manifest_path)

        return KnowledgeBuildResult(
            discovery=discovery,
            documents=documents,
            chunks=chunks,
            manifest=manifest,
            diff=knowledge_diff,
            empty_sources=sorted(empty_sources),
        )

    def diff(
        self,
        previous: KnowledgeManifest,
        current: KnowledgeManifest | None = None,
    ) -> KnowledgeDiff:
        if current is None:
            current = self.build(compare_with_stored=False).manifest

        return diff_manifests(previous, current)
