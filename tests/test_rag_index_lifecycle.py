import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.rag.index_manager import RAGIndexManager
from app.rag.ingestion.manifest import load_manifest
from app.rag.ingestion.service import KnowledgeIngestionService
from app.rag.models import DocumentChunk
from app.rag.query_context import RetrievalQuery
from app.rag.retrieval_text import build_retrieval_text, retrieval_text_hash
from app.rag.retriever import HybridRetriever
from app.rag.vector_store import InMemoryVectorStore


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self.document_calls: list[str] = []
        self.fail_on: str | None = None

    def document_embedding_identity(self) -> str:
        return "fake|model-v1|2|document"

    def embed_document(self, text: str) -> list[float]:
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("embedding failed")
        self.document_calls.append(text)
        return [1.0, 0.0]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


def make_chunk(chunk_id: str, text: str) -> DocumentChunk:
    return DocumentChunk(
        chunk_id=chunk_id,
        document_id="doc-test",
        source="规则.md",
        text=text,
        file_type="md",
        page=None,
        section="规则",
        start_char=0,
        end_char=len(text),
        content_hash=f"hash-{chunk_id}",
        chunker_version="section-char-v1",
        metadata={
            "knowledge_category": "policy",
            "business_domain": "support",
            "source_type": "policy",
        },
    )


class InMemoryVectorStoreTests(unittest.TestCase):
    def test_crud_search_and_embedding_text_identity(self) -> None:
        first = make_chunk("first", "退款规则")
        second = make_chunk("second", "物流规则")
        store = InMemoryVectorStore()
        store.upsert(
            first,
            [1.0, 0.0],
            embedding_text_hash=retrieval_text_hash(first),
            embedding_identity="fake-v1",
        )
        store.upsert(
            second,
            [0.0, 1.0],
            embedding_text_hash=retrieval_text_hash(second),
            embedding_identity="fake-v1",
        )

        self.assertEqual(store.size(), 2)
        self.assertEqual(store.get("first").chunk, first)
        self.assertEqual(store.search([1.0, 0.0], top_k=1)[0][0].chunk, first)
        self.assertIsNotNone(
            store.get_by_embedding_text_hash(
                retrieval_text_hash(first),
                embedding_identity="fake-v1",
            )
        )
        self.assertIsNone(
            store.get_by_embedding_text_hash(
                retrieval_text_hash(first),
                embedding_identity="fake-v2",
            )
        )

        store.delete("first")
        self.assertIsNone(store.get("first"))
        store.clear()
        self.assertEqual(store.size(), 0)

    def test_retrieval_text_is_canonical(self) -> None:
        chunk = make_chunk("canonical", "规则正文")
        self.assertEqual(
            build_retrieval_text(chunk),
            "规则.md\n规则\npolicy\nsupport\npolicy\n规则正文",
        )


class RAGIndexManagerTests(unittest.TestCase):
    def test_refresh_reuses_unchanged_retrieval_text_vectors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            manifest_path = Path(temp_dir) / "cache" / "manifest.json"
            root.mkdir()
            source = root / "规则.txt"
            source.write_text("稳定规则", encoding="utf-8")
            provider = FakeEmbeddingProvider()
            manager = RAGIndexManager(
                KnowledgeIngestionService(root, manifest_path=manifest_path),
                provider,
            )

            first = manager.refresh()
            first_call_count = len(provider.document_calls)
            self.assertEqual(
                provider.document_calls,
                [item["retrieval_text"] for item in first.active_index.items],
            )
            source.write_text("稳定规则\n", encoding="utf-8")
            second = manager.refresh()

            self.assertNotEqual(first.kb_version, second.kb_version)
            self.assertEqual(len(provider.document_calls), first_call_count)
            self.assertEqual(second.embedded_count, 0)
            self.assertEqual(second.reused_count, second.chunk_count)

    def test_failed_refresh_keeps_old_active_index_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            manifest_path = Path(temp_dir) / "cache" / "manifest.json"
            root.mkdir()
            source = root / "规则.txt"
            source.write_text("稳定规则", encoding="utf-8")
            provider = FakeEmbeddingProvider()
            manager = RAGIndexManager(
                KnowledgeIngestionService(root, manifest_path=manifest_path),
                provider,
            )
            manager.refresh()
            active_before = manager.get_active_index()
            manifest_before = load_manifest(manifest_path)

            source.write_text("FAIL 新规则", encoding="utf-8")
            provider.fail_on = "FAIL"

            with self.assertRaisesRegex(RuntimeError, "embedding failed"):
                manager.refresh()

            self.assertIs(manager.get_active_index(), active_before)
            self.assertEqual(manager.active_kb_version, active_before.kb_version)
            self.assertEqual(load_manifest(manifest_path).to_dict(), manifest_before.to_dict())

    def test_online_reads_do_not_scan_or_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            root.mkdir()
            (root / "规则.txt").write_text("退款规则", encoding="utf-8")
            service = KnowledgeIngestionService(
                root,
                manifest_path=Path(temp_dir) / "manifest.json",
            )
            manager = RAGIndexManager(service, FakeEmbeddingProvider())
            manager.refresh()
            retriever = HybridRetriever(manager)
            query = RetrievalQuery("退款问题", "退款 规则")

            with patch.object(service, "scan", side_effect=AssertionError("online scan")):
                self.assertTrue(retriever.retrieve(query, top_k=1))
                self.assertTrue(retriever.list_chunks())
                self.assertEqual(retriever.catalog()["summary"]["chunk_count"], 1)

    def test_ranking_modes_share_the_same_candidate_pool(self) -> None:
        manager = RAGIndexManager.__new__(RAGIndexManager)
        retriever = HybridRetriever(manager)
        query = RetrievalQuery("退款问题", "退款 规则")
        candidates = [
            {"chunk_id": "a", "source": "A", "text": "退款", "score": 0.8},
            {"chunk_id": "b", "source": "B", "text": "规则", "score": 0.7},
        ]

        with patch.object(
            retriever,
            "retrieve_candidates",
            return_value=candidates,
        ) as retrieve_candidates:
            raw = retriever.retrieve(
                query,
                top_k=2,
                candidate_k=10,
                mode="hybrid",
            )
            ruled = retriever.retrieve(
                query,
                top_k=2,
                candidate_k=10,
                mode="hybrid_rule",
            )

        self.assertEqual(retrieve_candidates.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["candidate_k"] == 10
                for call in retrieve_candidates.call_args_list
            )
        )
        self.assertEqual({item["chunk_id"] for item in raw}, {"a", "b"})
        self.assertEqual({item["chunk_id"] for item in ruled}, {"a", "b"})

    def test_candidate_cache_key_is_versioned_by_kb_and_embedding(self) -> None:
        manager = RAGIndexManager.__new__(RAGIndexManager)
        retriever = HybridRetriever(manager)
        query = RetrievalQuery("退款问题", "退款 规则")
        first = retriever.candidate_cache_key(query, 10, "kb-v1", "embed-v1")
        next_kb = retriever.candidate_cache_key(query, 10, "kb-v2", "embed-v1")
        next_embedding = retriever.candidate_cache_key(
            query,
            10,
            "kb-v1",
            "embed-v2",
        )

        self.assertNotEqual(first, next_kb)
        self.assertNotEqual(first, next_embedding)


if __name__ == "__main__":
    unittest.main()
