import unittest

from app.rag.ingestion.chunker import CHUNK_STRATEGIES
from app.rag.ranking import HYBRID_MODE, RANKING_MODES
from scripts.eval.eval_rag_chunk_strategy_ablation import (
    STRATEGY_ORDER,
    UncachedExperimentRetriever,
    compact_movement,
    strategy_definition,
    validate_candidate_pool_identity,
)


class FakeIndex:
    def __init__(self) -> None:
        self.calls = []

    def search(self, query, candidate_k):
        self.calls.append((query, candidate_k))
        return [{"chunk_id": "fresh"}]


class FakeManager:
    def __init__(self, index) -> None:
        self.index = index

    def get_active_index(self):
        return self.index


class ChunkStrategyAblationTests(unittest.TestCase):
    def test_experiment_uses_exact_registered_strategy_names(self) -> None:
        self.assertEqual(STRATEGY_ORDER, tuple(CHUNK_STRATEGIES))
        self.assertEqual(
            STRATEGY_ORDER,
            ("fixed_128", "fixed_256", "fixed_512", "markdown", "type_aware"),
        )

        for name in STRATEGY_ORDER:
            with self.subTest(strategy=name):
                definition = strategy_definition(name)
                self.assertEqual(definition["chunk_strategy"], name)
                self.assertEqual(
                    definition["registered_limit"],
                    CHUNK_STRATEGIES[name].max_chars,
                )

    def test_experiment_retriever_bypasses_candidate_cache(self) -> None:
        index = FakeIndex()
        retriever = UncachedExperimentRetriever(FakeManager(index))
        query = object()

        self.assertEqual(
            retriever.retrieve_candidates(query, candidate_k=20),
            [{"chunk_id": "fresh"}],
        )
        self.assertEqual(index.calls, [(query, 20)])

    def test_all_modes_must_share_identical_candidate_ids(self) -> None:
        mode_reports = {
            mode: {
                "results": [
                    {
                        "case_id": "case-1",
                        "candidate_chunk_ids": ["a", "b"],
                    }
                ]
            }
            for mode in RANKING_MODES
        }
        validate_candidate_pool_identity(mode_reports)

        mode_reports[next(mode for mode in RANKING_MODES if mode != HYBRID_MODE)][
            "results"
        ][0]["candidate_chunk_ids"] = ["b", "a"]

        with self.assertRaisesRegex(ValueError, "Candidate Top20 differs"):
            validate_candidate_pool_identity(mode_reports)

    def test_rank_movement_summary_keeps_all_required_counts(self) -> None:
        compact = compact_movement(
            {
                "counts": {
                    "promoted": 2,
                    "demoted": 3,
                },
                "net_top5_change": -1,
            }
        )

        self.assertEqual(compact["promoted"], 2)
        self.assertEqual(compact["demoted"], 3)
        self.assertEqual(compact["unchanged"], 0)
        self.assertEqual(compact["net_top5"], -1)


if __name__ == "__main__":
    unittest.main()
