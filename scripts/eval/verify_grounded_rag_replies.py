"""Generate checked replies from frozen Dev evidence; keep retrieval scores separate."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from app.agent.policies.evidence_guardrail import apply_policy_evidence_guardrail
from app.agent.response.grounded import checked_reply, policy_excerpts, render_grounded_reply
from app.agent.response.prompt_builder import build_model_messages
from app.core.config import get_settings
from app.core.schemas import ToolResult
from app.llm.llm_client import call_zhipu_chat
from app.observability.tracing import start_trace, finish_trace
from app.observability.usage import usage_scope
from scripts.eval.rag_dev_b01_replay import ROOT, DATASETS, ev


def quoted_chunks(excerpts, original):
    grouped = {}
    for excerpt in excerpts:
        grouped.setdefault(excerpt["chunk_id"], []).append(excerpt["text"])
    return [{**chunk, "text": "\n\n".join(grouped[chunk["chunk_id"]])}
            for chunk in original if chunk["chunk_id"] in grouped]


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay-from", type=Path, help="Decode saved actual model proposals; no new generation calls")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    settings = get_settings()
    bridge_path = ROOT / "reports/system_improvement/policy_evidence_bridge.json"
    bridge = json.loads(bridge_path.read_text(encoding="utf-8"))
    frozen = json.loads((ROOT / "reports/rag_dev_b01_optimization/candidates.json").read_text(encoding="utf-8"))
    source = [*sorted((ROOT / "app").rglob("*.py")), Path(__file__).resolve(), ROOT / "scripts/eval/eval_rag.py"]
    source_bytes = {str(p.relative_to(ROOT)): p.read_bytes() for p in source}
    with zipfile.ZipFile(args.output / "source_snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for name, content in source_bytes.items():
            archive.writestr(name, content)
    manifest = {"scope": "frozen complementary Top5 -> actual prompt -> live model evidence selection -> checked reply",
                "not_a_new_retrieval_result": True, "model": settings.zhipu_model, "temperature": 0.2,
                "bridge_sha256": hashlib.sha256(bridge_path.read_bytes()).hexdigest(), "datasets": {},
                "proposal_replay_from": str(args.replay_from) if args.replay_from else None,
                "source_hashes": {name: hashlib.sha256(content).hexdigest() for name, content in source_bytes.items()}}
    for label, path in DATASETS.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == frozen["datasets"][label]["sha256"]
        cases = ev.load_jsonl(path)
        if label == "v2":
            assert len(cases) == 6 and sum(len(c["expected"]["evidence_requirements"]) for c in cases) == 19
        else:
            assert len(cases) == 20
        rows = []
        for case, entry in zip(cases, bridge["datasets"][label]["records"]):
            assert case["case_id"] == entry["case_id"]
            original = entry["selected"]
            tool_result = apply_policy_evidence_guardrail(case["query"], ToolResult(tool_name="policy_search", success=True, result=original))
            assert tool_result.success
            tools = [tool_result]
            excerpts = policy_excerpts(tools)
            messages = build_model_messages(case["query"], [], tools)
            assert all(c["text"].strip() in messages[-1]["content"] for c in original)
            trace = start_trace(case["query"], "grounded-" + case["case_id"])
            error = None
            try:
                if excerpts:
                    if args.replay_from:
                        recorded = json.loads((args.replay_from / (case["case_id"] + ".json")).read_text(encoding="utf-8"))
                        assert messages == recorded["messages"]
                        assert not recorded["selection_check"].get("proposal_truncated")
                        raw = recorded["selection_check"]["proposal"]
                    else:
                        with usage_scope(trace):
                            raw = call_zhipu_chat(messages, settings)
                    reply, check = checked_reply(raw, tools)
                else:
                    reply, check = render_grounded_reply(tools, []), {"passed": True, "selected": [], "no_offered_evidence": True}
            except Exception as exc:
                error = {"type": type(exc).__name__, "stage": "generation_dependency_or_runtime"}
                reply, check = "", {"passed": False, "selected": []}
            trace = finish_trace(trace, reply, success=error is None)
            offered = quoted_chunks(excerpts, original)
            rendered = quoted_chunks(check["selected"], original)
            # Expected is used only after selection/rendering. These are coverage
            # diagnostics of quoted text, never substitute RAG Top5 outputs.
            if label == "v2":
                coverage = {name: ev.evidence_requirements_report(chunks, case["expected"]["evidence_requirements"])
                            for name, chunks in (("retrieved", original), ("offered", offered), ("rendered", rendered))}
            else:
                coverage = {name: ev.keyword_evidence_report(chunks, case.get("expected_keywords", []))
                            for name, chunks in (("retrieved", original), ("offered", offered), ("rendered", rendered))}
            row = {"case_id": case["case_id"], "reply": reply, "selection_check": check, "coverage_diagnostic": coverage,
                   "original_top5_ids": [c["chunk_id"] for c in original], "offered_excerpts": excerpts,
                   "messages": messages, "trace": trace, "error": error}
            with (args.output / (case["case_id"] + ".json")).open("x", encoding="utf-8") as out:
                json.dump(row, out, ensure_ascii=False, indent=2)
            rows.append({"case_id": case["case_id"], "selection_valid": check["passed"], "partial": check.get("partial", False), "error": error, "coverage": coverage})
            manifest["datasets"][label] = rows
            (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            print(case["case_id"], "selection_valid", check["passed"], "selected", len(check["selected"]), flush=True)
    manifest["source_inputs_unchanged"] = all((ROOT / name).read_bytes() == content for name, content in source_bytes.items())
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
