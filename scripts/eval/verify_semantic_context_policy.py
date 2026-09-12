"""Replay the recorded A/B pools through the actual policy tool and prompt builder."""
import argparse
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from app.agent.policies.evidence_guardrail import apply_policy_evidence_guardrail
from app.agent.response.prompt_builder import build_model_messages
from app.rag.query_context import RetrievalQuery
from app.rag.retriever import HybridRetriever
from app.tools import policy


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    experiment = json.loads(args.input.read_text(encoding="utf-8"))
    output = {"input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
              "scope": "recorded pools -> production policy tool -> guardrail -> model messages; no generated answers", "datasets": {}}
    for label, data in experiment["datasets"].items():
        rows = []
        for trace, scored in zip(data["traces"], data["A"]["results"]):
            assert trace["case_id"] == scored["case_id"]
            query = RetrievalQuery(**trace["query_contract"])
            row = {"case_id": trace["case_id"]}
            for arm, mode in (("A", ""), ("B", "route")):
                retriever = HybridRetriever(index_manager=object())
                def candidates(actual_query, *, candidate_k):
                    assert actual_query == query and candidate_k == 20
                    return trace[arm]["candidates"]
                retriever.retrieve_candidates = candidates
                with patch.dict("os.environ", {"RAG_EVIDENCE_SELECTION": "complementary", "RAG_SEMANTIC_CONTEXT": mode}), \
                     patch.object(policy, "_RETRIEVER", retriever), policy.policy_query_scope(query):
                    result = policy.policy_search(query.semantic_query, query.lexical_query, top_k=5)
                selected = result.result
                assert [c["chunk_id"] for c in selected] == trace[arm]["final_chunk_ids"]
                guarded = apply_policy_evidence_guardrail(scored["query"], result)
                messages = build_model_messages(scored["query"], [], [guarded])
                prompt = messages[-1]["content"]
                row[arm] = {"final_ids_match_recorded_run": True, "guardrail_passed": guarded.success,
                            "all_selected_text_in_model_context": guarded.success and all(c["text"].strip() in prompt for c in selected),
                            "messages": messages}
            rows.append(row)
        output["datasets"][label] = rows
    with args.output.open("x", encoding="utf-8") as out:
        json.dump(output, out, ensure_ascii=False, indent=2)
    print({label: {arm: all(row[arm]["all_selected_text_in_model_context"] for row in rows) for arm in ("A", "B")} for label, rows in output["datasets"].items()})


if __name__ == "__main__":
    main()
