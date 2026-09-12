"""Check policy tool -> guardrail -> model evidence against frozen Dev pools."""
import json
import hashlib
from pathlib import Path
from unittest.mock import patch

from app.tools import policy
from app.rag.query_context import RetrievalQuery
from app.rag.retriever import HybridRetriever
from app.rag.ranking import build_evidence_constraint
from app.agent.policies.evidence_guardrail import apply_policy_evidence_guardrail
from app.agent.response.prompt_builder import build_policy_evidence
from scripts.eval import eval_rag as ev

ROOT = Path(__file__).resolve().parents[2]


def main():
    frozen = json.loads((ROOT / 'reports/rag_dev_b01_optimization/candidates.json').read_text(encoding='utf-8'))
    original = json.loads((ROOT / 'reports/rag_dev_b01_followup/r13_guardrail_compatible.json').read_text(encoding='utf-8'))
    output = {"profile": "opt_in_complementary_policy_tool", "candidate_k": 20, "top_k": 5,
              "scope": "frozen candidates -> policy tool -> production guardrail -> model evidence; no new recall or generated-answer scoring", "datasets": {}}
    with patch.dict('os.environ', {"RAG_EVIDENCE_SELECTION": "complementary"}):
        for label in ("v2", "v1"):
            data = frozen['datasets'][label]
            path = Path(data['path'])
            assert hashlib.sha256(path.read_bytes()).hexdigest() == data['sha256']
            cases = ev.load_jsonl(path)
            records, scores = [], []
            for case, entry, previous in zip(cases, data['entries'], original['datasets'][label]['traces']):
                assert case['case_id'] == entry['case_id'] == previous['case_id']
                pool = entry['candidates']
                query = RetrievalQuery(**entry['query'])
                retriever = HybridRetriever(index_manager=object())
                def candidates(actual_query, *, candidate_k):
                    assert actual_query == query and candidate_k == 20
                    return pool
                retriever.retrieve_candidates = candidates
                with patch.object(policy, '_RETRIEVER', retriever), policy.policy_query_scope(query):
                    result = policy.policy_search(query.semantic_query, query.lexical_query, top_k=5)
                final = result.result
                assert [c['chunk_id'] for c in final] == previous['final_chunk_ids']
                guarded = apply_policy_evidence_guardrail(case['query'], result)
                model_evidence = build_policy_evidence([guarded])
                all_text_in_prompt = guarded.success and all(c['text'].strip() in model_evidence for c in final)
                constraint = build_evidence_constraint(ev.case_query_context(case))
                score = ev.score_mode(case, pool, final, constraint, mode='hybrid_rule', top_k=5)
                scores.append(score)
                records.append({"case_id": case['case_id'], "selected": final, "guardrail_passed": guarded.success,
                                "all_selected_text_in_model_context": all_text_in_prompt, "model_evidence": model_evidence})
            output['datasets'][label] = {"report": ev.build_mode_report(scores, mode='hybrid_rule', top_k=5), "records": records}
    path = ROOT / 'reports/system_improvement/policy_evidence_bridge.json'
    with path.open('x',encoding='utf-8') as handle:
        json.dump(output,handle,ensure_ascii=False,indent=2)
    for label, data in output['datasets'].items():
        print(label, data['report']['passed_count'], '/', data['report']['total_cases'],
              'all_selected_text_in_model_context', all(r['all_selected_text_in_model_context'] for r in data['records']))


if __name__ == '__main__':
    main()
