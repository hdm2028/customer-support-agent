from dataclasses import dataclass
from threading import RLock

from app.rag.embedding_client import EmbeddingProvider, get_embedding_provider
from app.rag.hybrid_index import HybridRAGIndex
from app.rag.ingestion.manifest import KnowledgeDiff, save_manifest
from app.rag.ingestion.service import KnowledgeIngestionService
from app.rag.models import DocumentChunk
from app.rag.retrieval_text import build_retrieval_text, retrieval_text_hash
from app.rag.vector_store import InMemoryVectorStore


@dataclass(frozen=True)
class IndexRefreshResult:
    kb_version: str
    diff: KnowledgeDiff
    chunk_count: int
    embedded_count: int
    reused_count: int
    active_index: HybridRAGIndex


class RAGIndexManager:
    """Build a complete candidate index before atomically activating it."""

    def __init__(
        self,
        ingestion: KnowledgeIngestionService | None = None,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.ingestion = ingestion or KnowledgeIngestionService()
        self.embedding_provider = embedding_provider or get_embedding_provider()
        self._active_index: HybridRAGIndex | None = None
        self._active_embedding_identity: str | None = None
        self._lock = RLock()
        self._refresh_lock = RLock()

    @property
    def active_kb_version(self) -> str | None:
        index = self.get_active_index()
        return index.kb_version if index else None

    def get_active_index(self) -> HybridRAGIndex | None:
        with self._lock:
            return self._active_index

    def refresh(self) -> IndexRefreshResult:
        with self._refresh_lock:
            return self._refresh()

    def _refresh(self) -> IndexRefreshResult:
        build = self.ingestion.build(save=False, compare_with_stored=True)
        embedding_identity = self.embedding_provider.document_embedding_identity()
        previous_index = self.get_active_index()

        if (
            previous_index is not None
            and previous_index.kb_version == build.manifest.kb_version
            and self._active_embedding_identity == embedding_identity
            and self._active_inputs_match(previous_index, build.chunks)
            and not build.diff.added
            and not build.diff.modified
            and not build.diff.deleted
        ):
            return IndexRefreshResult(
                kb_version=previous_index.kb_version,
                diff=build.diff,
                chunk_count=len(previous_index.chunks),
                embedded_count=0,
                reused_count=len(previous_index.chunks),
                active_index=previous_index,
            )

        previous_store = previous_index.vector_store if previous_index else None
        candidate_store = InMemoryVectorStore()
        embedded_count = 0
        reused_count = 0

        for chunk in build.chunks:
            text_hash = retrieval_text_hash(chunk)
            reusable = (
                previous_store.get_by_embedding_text_hash(
                    text_hash,
                    embedding_identity=embedding_identity,
                )
                if previous_store
                else None
            )

            if reusable is not None:
                vector = reusable.vector
                reused_count += 1
            else:
                vector = self.embedding_provider.embed_document(
                    build_retrieval_text(chunk)
                )
                embedded_count += 1

            candidate_store.upsert(
                chunk,
                vector,
                embedding_text_hash=text_hash,
                embedding_identity=embedding_identity,
            )

        candidate_index = HybridRAGIndex(
            build.chunks,
            candidate_store,
            kb_version=build.manifest.kb_version,
            embedding_provider=self.embedding_provider,
        )
        self._validate_candidate(candidate_index)

        # Persist only after the full candidate index has built and validated.
        save_manifest(build.manifest, self.ingestion.manifest_path)

        with self._lock:
            self._active_index = candidate_index
            self._active_embedding_identity = embedding_identity

        return IndexRefreshResult(
            kb_version=candidate_index.kb_version,
            diff=build.diff,
            chunk_count=len(build.chunks),
            embedded_count=embedded_count,
            reused_count=reused_count,
            active_index=candidate_index,
        )

    @staticmethod
    def _validate_candidate(index: HybridRAGIndex) -> None:
        if index.vector_store.size() != len(index.chunks):
            raise ValueError("VectorStore size does not match the candidate chunk set")

        dimensions = {
            len(record.vector)
            for record in index.vector_store.all_records()
        }
        if 0 in dimensions or len(dimensions) > 1:
            raise ValueError("Candidate index contains invalid embedding dimensions")

    @staticmethod
    def _active_inputs_match(
        index: HybridRAGIndex,
        chunks: list[DocumentChunk],
    ) -> bool:
        if index.vector_store.size() != len(chunks):
            return False

        for chunk in chunks:
            record = index.vector_store.get(chunk.chunk_id)
            if (
                record is None
                or record.embedding_text_hash != retrieval_text_hash(chunk)
            ):
                return False

        return True


_DEFAULT_INDEX_MANAGER: RAGIndexManager | None = None
_DEFAULT_MANAGER_LOCK = RLock()


def get_rag_index_manager() -> RAGIndexManager:
    global _DEFAULT_INDEX_MANAGER

    with _DEFAULT_MANAGER_LOCK:
        if _DEFAULT_INDEX_MANAGER is None:
            _DEFAULT_INDEX_MANAGER = RAGIndexManager()

        return _DEFAULT_INDEX_MANAGER
