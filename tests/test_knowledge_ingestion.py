import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.rag.ingestion.chunker import (
    CHUNKER_VERSION,
    chunk_document,
    choose_chunk_strategy,
    split_markdown_sections,
)
from app.rag.ingestion.discovery import discover_files
from app.rag.ingestion.loader import load_pdf_document
from app.rag.ingestion.manifest import diff_manifests, load_manifest
from app.rag.ingestion.metadata import MetadataEnricher
from app.rag.ingestion.service import KnowledgeIngestionService
from app.rag.models import (
    KnowledgeSource,
    RawDocument,
    build_document_id,
    content_hash_text,
)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def raw_document(
    source: str,
    text: str = "测试知识正文",
    *,
    file_type: str = "md",
    metadata: dict | None = None,
) -> RawDocument:
    return RawDocument(
        document_id=build_document_id(source),
        source=source,
        text=text,
        file_type=file_type,
        content_hash=content_hash_text(text),
        metadata=metadata or {},
    )


class KnowledgeIdentityTests(unittest.TestCase):
    def test_repeated_ingestion_has_stable_document_and_chunk_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            manifest_path = Path(temp_dir) / "cache" / "manifest.json"
            write_text(root / "退款政策.md", "# 退款政策\n\n这是稳定的退款政策正文。")
            service = KnowledgeIngestionService(root, manifest_path=manifest_path)

            first = service.build(compare_with_stored=False)
            second = service.build(compare_with_stored=False)

            self.assertEqual(first.documents[0].document_id, second.documents[0].document_id)
            self.assertEqual(first.documents[0].content_hash, second.documents[0].content_hash)
            self.assertEqual(
                [chunk.chunk_id for chunk in first.chunks],
                [chunk.chunk_id for chunk in second.chunks],
            )
            self.assertEqual(
                [chunk.content_hash for chunk in first.chunks],
                [chunk.content_hash for chunk in second.chunks],
            )
            self.assertEqual(first.manifest.kb_version, second.manifest.kb_version)

    def test_content_change_keeps_document_id_and_changes_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            file_path = root / "规则.txt"
            write_text(file_path, "第一版规则")
            service = KnowledgeIngestionService(root)
            first = service.build(compare_with_stored=False)

            write_text(file_path, "第二版规则")
            second = service.build(compare_with_stored=False)

            self.assertEqual(first.documents[0].document_id, second.documents[0].document_id)
            self.assertNotEqual(first.documents[0].content_hash, second.documents[0].content_hash)


class KnowledgeDiffTests(unittest.TestCase):
    def test_added_modified_deleted_and_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            file_a = root / "a.txt"
            file_b = root / "b.txt"
            write_text(file_a, "版本一")
            service = KnowledgeIngestionService(root)
            initial = service.build(compare_with_stored=False).manifest

            unchanged = service.build(compare_with_stored=False).manifest
            self.assertEqual(diff_manifests(initial, unchanged).unchanged, ["a.txt"])

            write_text(file_a, "版本二")
            modified = service.build(compare_with_stored=False).manifest
            modified_diff = diff_manifests(initial, modified)
            self.assertEqual(modified_diff.modified, ["a.txt"])
            self.assertEqual(modified_diff.added, [])

            write_text(file_b, "新增知识")
            added = service.build(compare_with_stored=False).manifest
            added_diff = diff_manifests(modified, added)
            self.assertEqual(added_diff.added, ["b.txt"])
            self.assertEqual(added_diff.unchanged, ["a.txt"])

            file_a.unlink()
            deleted = service.build(compare_with_stored=False).manifest
            deleted_diff = diff_manifests(added, deleted)
            self.assertEqual(deleted_diff.deleted, ["a.txt"])
            self.assertEqual(deleted_diff.unchanged, ["b.txt"])

    def test_parser_or_chunker_version_change_marks_documents_modified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_text(root / "规则.txt", "规则内容")
            first = KnowledgeIngestionService(root).build(
                compare_with_stored=False
            ).manifest
            second = KnowledgeIngestionService(
                root,
                chunker_version="section-char-v2",
            ).build(compare_with_stored=False).manifest

            self.assertEqual(diff_manifests(first, second).modified, ["规则.txt"])


class LoaderAndMetadataTests(unittest.TestCase):
    def test_markdown_sections_and_front_matter_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            source = "refund/政策.md"
            write_text(
                root / source,
                "---\nknowledge_category: front_matter\ntags: [\"refund\"]\n---\n"
                "# 总则\n第一部分\n## 例外\n第二部分",
            )
            service = KnowledgeIngestionService(
                root,
                path_metadata={"refund": {"knowledge_category": "path"}},
                explicit_metadata={source: {"knowledge_category": "explicit"}},
                chunk_strategy="markdown",
            )
            result = service.build(compare_with_stored=False)

            self.assertEqual(result.documents[0].metadata["knowledge_category"], "explicit")
            self.assertEqual(result.documents[0].metadata["tags"], ["refund"])
            self.assertEqual(
                split_markdown_sections(result.documents[0].text),
                [("总则", "# 总则\n第一部分"), ("例外", "## 例外\n第二部分")],
            )
            self.assertEqual([chunk.section for chunk in result.chunks], ["总则", "例外"])

    def test_txt_empty_image_and_unsupported_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_text(root / "normal.txt", "普通 TXT 知识")
            write_text(root / "empty.txt", "")
            (root / "image.png").write_bytes(b"not-an-image")
            write_text(root / "ignored.csv", "unsupported")
            result = KnowledgeIngestionService(root).build(compare_with_stored=False)

            documents = {document.source: document for document in result.documents}
            self.assertEqual(documents["normal.txt"].text, "普通 TXT 知识")
            self.assertEqual(documents["empty.txt"].metadata["status"], "empty")
            self.assertEqual(documents["image.png"].metadata["status"], "not_implemented")
            self.assertEqual(result.empty_sources, ["empty.txt", "image.png"])
            self.assertEqual(
                [item.source for item in result.discovery.unsupported],
                ["ignored.csv"],
            )

    def test_recursive_discovery_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            write_text(root / "z.txt", "z")
            write_text(root / "nested" / "a.md", "# A\na")

            discovery = discover_files(root)

            self.assertEqual(
                [source.source for source in discovery.sources],
                ["nested/a.md", "z.txt"],
            )

    def test_missing_knowledge_directory_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            discovery = discover_files(Path(temp_dir) / "missing")

            self.assertEqual(discovery.sources, [])
            self.assertEqual(discovery.unsupported, [])

    def test_pdf_heading_hierarchy_is_preserved(self) -> None:
        class FakePage:
            def get_text(self, mode: str) -> dict:
                self.assert_mode = mode
                lines = [
                    ("测试文档", 22),
                    ("一、退款政策", 16),
                    ("这是退款政策正文，长度足够用于测试。", 11),
                    ("1.1 申请条件", 12),
                    ("这是申请条件正文，长度同样足够测试。", 11),
                ]
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {"spans": [{"text": text, "size": size}]}
                                for text, size in lines
                            ],
                        }
                    ]
                }

        class FakePdf(list):
            def __init__(self) -> None:
                super().__init__([FakePage()])
                self.closed = False

            def close(self) -> None:
                self.closed = True

        fake_pdf = FakePdf()
        fake_fitz = SimpleNamespace(open=lambda path: fake_pdf)
        source = KnowledgeSource(
            path=Path("fake.pdf"),
            source="fake.pdf",
            file_type="pdf",
            content_hash="file-hash",
            size=1,
        )

        with patch.dict(sys.modules, {"fitz": fake_fitz}):
            documents = load_pdf_document(source)

        self.assertTrue(fake_pdf.closed)
        self.assertEqual(
            [document.section for document in documents],
            ["一、退款政策", "一、退款政策 / 1.1 申请条件"],
        )
        self.assertTrue(all(document.page == 1 for document in documents))


class ChunkAndManifestTests(unittest.TestCase):
    def test_chunk_strategy_parameters_match_the_baseline(self) -> None:
        cases = [
            ("fixed_128", 128, 16),
            ("fixed_256", 256, 32),
            ("fixed_512", 512, 64),
            ("markdown", 700, 0),
            ("type_aware", 700, 0),
        ]

        for name, size, overlap in cases:
            document = raw_document(
                "规则.md",
                metadata={"chunk_strategy": name},
            )

            with self.subTest(strategy=name):
                strategy = choose_chunk_strategy(document)
                self.assertEqual(strategy.name, name)
                self.assertEqual((strategy.max_chars, strategy.overlap), (size, overlap))

    def test_fixed_chunking_ignores_markdown_boundaries(self) -> None:
        body_a = " ".join(["alpha"] * 100)
        body_b = " ".join(["bravo"] * 100)
        body_c = " ".join(["charlie"] * 100)
        document = raw_document(
            "规则.md",
            f"# 文档\n\n## Section A\n{body_a}\n\n## Section B\n{body_b}\n\n"
            f"## Section C\n{body_c}",
        )

        fixed_chunks = chunk_document(document, chunk_strategy="fixed_256")
        markdown_chunks = chunk_document(document, chunk_strategy="markdown")

        self.assertIn("## Section A", fixed_chunks[0].text)
        self.assertIn("## Section B", fixed_chunks[0].text)
        self.assertEqual(
            [chunk.section for chunk in markdown_chunks],
            ["Section A", "Section B", "Section C"],
        )
        self.assertTrue(
            all(
                not ("## Section A" in chunk.text and "## Section B" in chunk.text)
                for chunk in markdown_chunks
            )
        )
        self.assertTrue(all(chunk.metadata["token_count"] <= 256 for chunk in fixed_chunks))

    def test_fixed_256_and_512_are_different(self) -> None:
        sections = []
        for index in range(6):
            body = " ".join([f"word{index}"] * 100)
            sections.append(f"## Section {index}\n{body}")
        document = raw_document("规则.md", "# 文档\n\n" + "\n\n".join(sections))

        fixed_256 = chunk_document(document, chunk_strategy="fixed_256")
        fixed_512 = chunk_document(document, chunk_strategy="fixed_512")

        self.assertGreater(len(fixed_256), len(fixed_512))
        self.assertNotEqual(
            [chunk.text for chunk in fixed_256],
            [chunk.text for chunk in fixed_512],
        )

    def test_chunk_id_contains_document_identity_and_version_is_explicit(self) -> None:
        document = raw_document("政策.md", "内容" * 400)
        first = chunk_document(document)
        second = chunk_document(document)

        self.assertEqual(
            [chunk.chunk_id for chunk in first],
            [chunk.chunk_id for chunk in second],
        )
        self.assertTrue(
            all(chunk.chunk_id.startswith(document.document_id + ":chunk:") for chunk in first)
        )
        self.assertTrue(all(chunk.chunker_version == CHUNKER_VERSION for chunk in first))

    def test_manifest_can_be_saved_loaded_and_compared(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "knowledge"
            manifest_path = Path(temp_dir) / "cache" / "knowledge_manifest.json"
            write_text(root / "规则.md", "# 规则\n稳定内容")
            service = KnowledgeIngestionService(root, manifest_path=manifest_path)
            built = service.build(save=True, compare_with_stored=False)
            loaded = load_manifest(manifest_path)

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.to_dict(), built.manifest.to_dict())
            self.assertEqual(diff_manifests(loaded, built.manifest).unchanged, ["规则.md"])
            document = next(iter(loaded.documents.values()))
            self.assertEqual(document.chunk_ids, list(document.chunk_hashes))


if __name__ == "__main__":
    unittest.main()
