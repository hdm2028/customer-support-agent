"""Read-only validation of B01 candidates and their append-only Dev integration.

Run from the repository root: python reports/answer_e2e_dev_expansion_b01/validate_data.py
No model calls, database initialization, or changes to evaluator scoring.
"""
import ast
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.core.schemas import RouteDecision
from app.tools.registry import TOOL_HANDLERS

OUT = Path(__file__).resolve().parent


def digest(data):
    return hashlib.sha256(data).hexdigest()


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main():
    manifest_path = OUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {
        "role": "dev", "promotion": "append only after source/contract review; expectations frozen before live execution",
        "datasets": {}, "unchanged_inputs": {},
    }
    counts, mapping = {}, []
    known_tools = set(TOOL_HANDLERS) | {"refund_decision", "ticket_decision"}
    tree = ast.parse((ROOT / "scripts/eval/eval_e2e.py").read_text(encoding="utf-8"))
    actions = {node.value.value for node in ast.walk(tree) if isinstance(node, ast.Return)
               and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)}
    knowledge = {path.name: path.read_text(encoding="utf-8") for path in (ROOT / "data/knowledge").glob("*.md")}
    for kind,expected in (("answer", 16), ("e2e", 12)):
        original = ROOT / f"data/eval/{kind}_eval.jsonl"
        candidate = ROOT / f"data/eval/candidates/{kind}_dev_candidates_b01.jsonl"
        new = rows(candidate)
        assert len(new) == expected
        if kind not in manifest["datasets"]:
            manifest["datasets"][kind] = {
                "original": original.relative_to(ROOT).as_posix(), "original_sha256": digest(original.read_bytes()),
                "original_bytes": original.stat().st_size, "original_rows": len(rows(original)),
                "candidate": candidate.relative_to(ROOT).as_posix(), "candidate_sha256": digest(candidate.read_bytes()),
                "candidate_rows": len(new),
            }
        metadata = manifest["datasets"][kind]
        raw = original.read_bytes()
        prefix = raw[:metadata["original_bytes"]]
        assert digest(prefix) == metadata["original_sha256"], "original rows changed"
        assert digest(candidate.read_bytes()) == metadata["candidate_sha256"], "frozen candidates changed"
        old = [json.loads(line) for line in prefix.decode("utf-8").splitlines() if line.strip()]
        assert rows(original) in (old, old + new), "unexpected appended cases"
        assert len({case["id"] for case in old + new}) == len(old) + len(new)
        allowed = ({"id", "query", "expected_keywords", "forbidden_keywords", "require_citation", "notes"} if kind == "answer"
                   else {"id", "intent", "user_message", "expected_order_id", "expected_route", "expected_tools",
                         "expected_tool_success", "expected_final_action", "forbidden_tools", "must_include", "must_not_include", "notes"})
        for case in new:
            required = allowed if kind == "answer" else allowed - {"expected_tool_success"}
            assert required <= set(case) <= allowed, (case["id"], "unsupported/missing fields")
            assert isinstance(case["id"], str) and isinstance(case.get("query", case.get("user_message")), str)
            required_terms = case.get("expected_keywords", case.get("must_include"))
            forbidden = case.get("forbidden_keywords", case.get("must_not_include"))
            assert required_terms and all(isinstance(term, str) and term for term in required_terms + forbidden)
            assert not any(bad in term for term in required_terms for bad in forbidden), "contradictory terms"
            assert not {"MQ", "知识库"} & set(required_terms)
            if kind == "answer":
                assert type(case["require_citation"]) is bool
            else:
                assert case["expected_order_id"] is None or re.fullmatch(r"\d+", case["expected_order_id"])
                assert set(case["expected_route"]) <= set(RouteDecision.model_fields)
                RouteDecision(**case["expected_route"])
                assert len(set(case["expected_tools"])) == len(case["expected_tools"])
                assert (set(case["expected_tools"]) | set(case["forbidden_tools"])) <= known_tools
                assert not set(case["expected_tools"]) & set(case["forbidden_tools"])
                assert set(case.get("expected_tool_success", {})) <= set(case["expected_tools"])
                assert all(type(value) is bool for value in case.get("expected_tool_success", {}).values())
                assert case["expected_final_action"] in actions
            for path in re.findall(r"(?:app|tests|data)/[A-Za-z0-9_./-]+\.(?:py|json)", case["notes"]):
                assert (ROOT / path).is_file(), (case["id"], path)
            sources = []
            for filename, section in re.findall(r"([^/、；，。\s:]+\.md)/([^；，、。]+)", case["notes"]):
                assert filename in knowledge, (case["id"], filename)
                headings = [line.lstrip("#").strip() for line in knowledge[filename].splitlines() if line.startswith("#")]
                assert section in headings, (case["id"], filename, section)
                sources.append({"source": filename, "section": section})
            mapping.append({"case_id": case["id"], "layer": kind, "scenario_id": re.search(r"AE\d{2}", case["notes"])[0],
                            "knowledge_sources": sources, "ground_truth": case["notes"],
                            "review": "source_and_consumer_contract_checked", "regression": "回归案例" in case["notes"]})
        near = []
        normalize = lambda case: re.sub(r"\W", "", case.get("query", case.get("user_message", "")))
        for index, case in enumerate(old + new):
            if index < len(old):
                continue
            for prior in (old + new)[:index]:
                ratio = SequenceMatcher(None, normalize(case), normalize(prior)).ratio()
                if ratio >= .8:
                    near.append({"a": case["id"], "b": prior["id"], "similarity": ratio})
        assert not near, near
        counts[kind] = {"existing": len(old), "new": len(new), "after_append": len(old) + len(new),
                        "promoted": rows(original) == old + new, "near_duplicate_pairs_at_080": near}
    if not manifest["unchanged_inputs"]:
        for folder in ("app", "scripts", ".agents/skills/eval-dataset-builder", "data/knowledge"):
            for path in (ROOT / folder).rglob("*"):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    manifest["unchanged_inputs"][path.relative_to(ROOT).as_posix()] = digest(path.read_bytes())
        for name in ("routing_eval.jsonl", "tool_eval.jsonl", "rag_eval.jsonl", "rag_eval_dev_v2_batch01.jsonl"):
            path = ROOT / "data/eval" / name
            manifest["unchanged_inputs"][path.relative_to(ROOT).as_posix()] = digest(path.read_bytes())
        manifest["unchanged_inputs"]["data/orders.json"] = digest((ROOT / "data/orders.json").read_bytes())
    for relative, expected_hash in manifest["unchanged_inputs"].items():
        assert digest((ROOT / relative).read_bytes()) == expected_hash, (relative, "unrelated input changed")
    assert len({item["scenario_id"] for item in mapping}) == 18
    report = {"passed": True, "checks": ["JSON/types and consumed fields", "unique IDs and near-duplicates per layer",
              "route/tool/action names", "noncontradictory expected/forbidden labels", "knowledge source/section existence",
              "source/code review recorded separately from live results", "original rows and frozen candidates unchanged"],
              "counts": counts, "cross_layer_scenarios": 18,
              "regression_cases": [item["case_id"] for item in mapping if item["regression"]],
              "requires_human_review_candidates": [],
              "limits": ["lexical scoring does not prove complete semantic correctness", "not a live-system pass",
                         "multi-turn, failure injection and production payment excluded"]}
    for name, value in (("manifest.json", manifest), ("validation.json", report), ("case_sources.json", mapping)):
        (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
