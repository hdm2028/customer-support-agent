from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from app.rag.query_context import RetrievalQuery
from app.rag.retriever import HybridRetriever
from app.rag.semantic_reranker import CrossEncoderSemanticReranker, SemanticRerankerError


class RerankerLifecycleTests(unittest.TestCase):
    def test_all_semantic_modes_reuse_one_instance(self):
        query = RetrievalQuery('refund', 'refund')
        for mode in ('hybrid_semantic', 'hybrid_semantic_constraint', 'hybrid_semantic_fusion'):
            with self.subTest(mode=mode):
                retriever = HybridRetriever(index_manager=object())
                retriever.retrieve_candidates = lambda *a, **kw: []
                model = SimpleNamespace(rerank=lambda *a: [])
                with patch('app.rag.retriever.build_semantic_reranker', return_value=model) as build, \
                     patch('app.rag.ranking.build_semantic_reranker', return_value=model) as uncached:
                    retriever.retrieve(query, mode=mode, top_k=5, candidate_k=20)
                    retriever.retrieve(query, mode=mode, top_k=5, candidate_k=20)
                self.assertEqual(build.call_count, 1)
                self.assertEqual(uncached.call_count, 0)

    def test_concurrent_first_requests_build_only_once(self):
        retriever = HybridRetriever(index_manager=object())
        retriever.retrieve_candidates = lambda *a, **kw: []
        barrier = Barrier(4)
        def build():
            sleep(0.05)
            return SimpleNamespace(rerank=lambda *a: [])
        def retrieve(_):
            barrier.wait(timeout=5)
            return retriever.retrieve(RetrievalQuery('refund', 'refund'), mode='hybrid_semantic')
        with patch('app.rag.retriever.build_semantic_reranker', side_effect=build) as factory:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(retrieve, range(4)))
        self.assertEqual(factory.call_count, 1)

    def test_concurrent_lazy_weight_load_happens_once_and_can_retry_failure(self):
        reranker = CrossEncoderSemanticReranker()
        barrier = Barrier(4)
        def model_factory(*a, **kw):
            sleep(0.05)
            return SimpleNamespace(model=SimpleNamespace(eval=lambda: None))
        def load(_):
            barrier.wait(timeout=5)
            return reranker._load_model()
        # Test only the lazy lifecycle; real model quality is measured by the
        # separate frozen Dev comparison, never by these doubles.
        with patch.dict('sys.modules', {
            'torch': SimpleNamespace(nn=SimpleNamespace(Sigmoid=lambda: None)),
            'sentence_transformers': SimpleNamespace(CrossEncoder=model_factory),
        }):
            with patch('sentence_transformers.CrossEncoder', side_effect=model_factory) as factory:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    models = list(pool.map(load, range(4)))
            self.assertEqual(factory.call_count, 1)
            self.assertTrue(all(model is models[0] for model in models))
            other = CrossEncoderSemanticReranker()
            with patch('sentence_transformers.CrossEncoder', side_effect=RuntimeError('load failed')):
                with self.assertRaises(SemanticRerankerError):
                    other._load_model()
            self.assertIsNotNone(other._load_model())
