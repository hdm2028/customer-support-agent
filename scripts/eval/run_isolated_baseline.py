"""Run existing development evaluators in a disposable copy, never the live DB.

No evaluator or expected labels are changed. Each suite starts with a fresh
SQLite database, demo seed, process-local cache and a private knowledge copy.
By default only the semantic router calls the configured LLM. --live-replies
explicitly enables generated replies without changing evaluator scoring.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
SUITES = {
    "routing_v2": "routing_eval.jsonl",
    "tools": "tool_eval.jsonl",
    "answer": "answer_eval.jsonl",
    "e2e": "e2e_eval.jsonl",
}


def fingerprint(root: Path) -> dict:
    paths = list((root / "app").rglob("*.py"))
    paths += list((root / "data" / "knowledge").rglob("*"))
    paths += [root / "data" / "orders.json"]
    paths += [root / "data" / "eval" / name for name in SUITES.values()]
    paths += [root / "scripts" / "eval" / f"eval_{name}.py" for name in SUITES]
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths) if p.is_file()}


def classify_fallback(reason: str) -> str:
    reason = reason.lower()
    if any(s in reason for s in ("timeout", "timed out", "connection", "http", "api key", "api_key", "401", "403", "429", "insufficient", "balance", "未配置")):
        return "dependency_unavailable"
    if any(s in reason for s in ("validationerror", "jsondecode", "invalid json", "valueerror")):
        return "model_output_contract"
    return "unclassified_fallback"


def diagnostics(report: dict, traces: list[dict]) -> dict:
    """Separate observed dependency errors; never relabel other failures as proven logic bugs."""
    fallbacks = []
    def visit(value, case_id=None):
        if isinstance(value, dict):
            case_id = value.get("case_id", case_id)
            if value.get("source") == "fallback":
                reason = str(value.get("reason", ""))
                item = {"case_id": case_id, "reason": reason, "category": classify_fallback(reason)}
                if item not in fallbacks:
                    fallbacks.append(item)
            routing_reason = str(value.get("routing_reason", ""))
            if routing_reason.startswith("LLM semantic routing failed:"):
                item = {"case_id": case_id, "reason": routing_reason, "category": classify_fallback(routing_reason)}
                if item not in fallbacks:
                    fallbacks.append(item)
            for child in value.values():
                visit(child, case_id)
        elif isinstance(value, list):
            for child in value:
                visit(child, case_id)
    visit(report.get("results", []))
    for trace in traces:
        visit(trace, trace.get("conversation_id"))
    return {"fallbacks": fallbacks,
            "note": "Original scores are unchanged. A dependency-affected failure is not proof of a routing or business logic defect. Other failures require stage-level inspection."}


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--suites", nargs="+", choices=SUITES, default=list(SUITES))
    parser.add_argument("--route-replay-from", help="Freeze observed routes for downstream checks; no new semantic-router measurement")
    parser.add_argument("--live-replies", action="store_true", help="Enable use_llm=True for answer/e2e; preserve the original scoring")
    parser.add_argument("--record-semantic-responses", action="store_true")
    parser.add_argument("--record-case-results", action="store_true", help="Save every unchanged case result, including routing successes")
    parser.add_argument("--semantic-replay-from", help="Reuse raw first responses; contract repairs still call the model")
    parser.add_argument("--semantic-repair", choices=("off", "once"), default="off")
    parser.add_argument("--semantic-related-topics", choices=("strict", "discard_invalid"), default="strict")
    parser.add_argument("--policy-evidence", choices=("baseline", "complementary"), default="baseline")
    args = parser.parse_args()
    if args.route_replay_from and (args.semantic_replay_from or args.record_semantic_responses or args.semantic_repair != "off"):
        parser.error("Final-route replay cannot measure semantic response repair")
    if args.route_replay_from and "routing_v2" in args.suites:
        parser.error("Route replay only supports tools/answer/e2e")
    if args.live_replies and any(s not in {"answer", "e2e"} for s in args.suites):
        parser.error("Live replies only support answer/e2e")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(ROOT))
    from app.core.config import get_settings
    settings = get_settings()  # Credentials are passed in memory, never written to artifacts.
    env = os.environ.copy()
    overrides = {
        "DATABASE_BACKEND": "sqlite", "MYSQL_DSN": "", "MYSQL_DATABASE": "", "MYSQL_USER": "",
        "REDIS_URL": "", "REDIS_HOST": "", "MQ_BACKEND": "sqlite", "SEED_DEMO_DATA": "true",
        "RAG_EMBEDDING_PROVIDER": "local", "EMBEDDING_DIMENSIONS": "256",
        "CHUNK_STRATEGY": "fixed_256", "RAG_RANKING_MODE": "hybrid_rule",
        "RAG_SEMANTIC_WEIGHT": "0.62", "RAG_BM25_WEIGHT": "0.28", "RAG_KEYWORD_WEIGHT": "0.10",
        "RAG_CANDIDATE_MULTIPLIER": "4", "PYTHONIOENCODING": "utf-8",
        "RAG_EVIDENCE_SELECTION": "complementary" if args.policy_evidence == "complementary" else "", "RAG_SEMANTIC_CONTEXT": "",
        "SEMANTIC_RELATED_TOPICS": "discard_invalid" if args.semantic_related_topics == "discard_invalid" else "",
        "SEMANTIC_ROUTE_REPAIR": "once" if args.semantic_repair == "once" else "",
        "PAYMENT_ADAPTER": "", "AUTH_TOKENS": "",
        "LLM_TIMEOUT_SECONDS": str(settings.llm_timeout_seconds),
        "ZHIPU_MODEL": settings.zhipu_model, "ZHIPU_BASE_URL": settings.zhipu_base_url,
    }
    env.update(overrides)
    before = fingerprint(ROOT)
    # Freeze source bytes once, so ongoing application edits cannot mix versions
    # between suites. No holdout data, existing business DB, trace or .env copied.
    source_files = {}
    for folder in ("app", "scripts", "data/knowledge"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                source_files[path.relative_to(ROOT)] = path.read_bytes()
    for relative in [Path("data/orders.json"), *[Path("data/eval") / name for name in SUITES.values()]]:
        source_files[relative] = (ROOT / relative).read_bytes()
    with zipfile.ZipFile(output / "source_snapshot.zip", "x", zipfile.ZIP_DEFLATED) as archive:
        for relative, content in source_files.items():
            archive.writestr(str(relative).replace("\\", "/"), content)
    manifest = {"profile": "isolated_sqlite_local_rag_live_semantic_router",
                "configuration": {k:v for k,v in overrides.items() if k != "ZHIPU_BASE_URL"},
                "llm_key_present": settings.has_llm_key, "inputs": before, "suites": {},
                "startup": "init_database and RAG refresh, matching API startup in private copy",
                "immutable_source_snapshot": True,
                "reply_execution": "use_llm=True; production forced fallbacks remain active" if args.live_replies else "use_llm=False; template replies"}
    if args.semantic_replay_from:
        manifest["semantic_replay_from"] = str(Path(args.semantic_replay_from).resolve())
        manifest["profile"] = "isolated_sqlite_frozen_first_semantic_response_live_contract_repair"
    if args.route_replay_from:
        manifest["route_replay_from"] = str(Path(args.route_replay_from).resolve())
        manifest["llm_calls"] = ("generated replies only; observed runtime routes, no expected labels used" if args.live_replies else
                                 "none; observed runtime route replay, no expected labels used")
    def save_manifest():
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    save_manifest()
    for suite in args.suites:
        destination = output / suite
        destination.mkdir()
        with tempfile.TemporaryDirectory(prefix="support-baseline-") as tmp:
            sandbox = Path(tmp)
            for relative, content in source_files.items():
                if relative.parent == Path("data/eval") and relative.name != SUITES[suite]:
                    continue
                target = sandbox / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            child_env = dict(env, DATABASE_PATH=str(sandbox / "data" / "baseline.db"), PYTHONPATH=str(sandbox))
            bootstrap = (
                "import json,runpy,sys; from pathlib import Path; "
                "from app.storage.database import init_database; init_database(); "
                "from app.rag.index_manager import get_rag_index_manager; "
                "r=get_rag_index_manager().refresh(); "
                "Path('reports').mkdir(exist_ok=True); "
                "Path('reports/startup.json').write_text(json.dumps({'kb_version':r.kb_version,'chunk_count':r.chunk_count,'embedded_count':r.embedded_count}),encoding='utf-8'); "
            )
            if args.route_replay_from:
                observed_trace = Path(args.route_replay_from) / suite / "trace.jsonl"
                shutil.copy2(observed_trace, sandbox / "observed_routes.jsonl")
                bootstrap += "from scripts.eval.frozen_route_replay import install; verify=install('observed_routes.jsonl'); "
            if args.record_semantic_responses or args.semantic_replay_from:
                replay = None
                if args.semantic_replay_from:
                    shutil.copy2(Path(args.semantic_replay_from) / suite / "reports/semantic_responses.jsonl", sandbox / "observed_semantic.jsonl")
                    replay = "observed_semantic.jsonl"
                bootstrap += ("from scripts.eval.semantic_response_replay import install as install_semantic; "
                              f"verify_semantic=install_semantic('reports/semantic_responses.jsonl', {replay!r}); ")
            bootstrap += (f"sys.argv=['eval_{suite}']+sys.argv[1:]; "
                          "import importlib; "
                          f"e=importlib.import_module('scripts.eval.eval_{suite}'); ")
            if args.live_replies:
                bootstrap += "from scripts.eval.live_reply_mode import install as enable_live_replies; enable_live_replies(e); "
            if args.record_case_results:
                bootstrap += "from scripts.eval.evaluation_recording import install as record_cases; record_cases(e); "
            bootstrap += "\ntry:\n e.main()\nexcept SystemExit as error:\n exit_code=error.code\nelse:\n exit_code=0\n"
            if args.route_replay_from:
                bootstrap += "verify()\n"
            if args.record_semantic_responses or args.semantic_replay_from:
                bootstrap += "verify_semantic()\n"
            bootstrap += "raise SystemExit(exit_code)\n"
            command = [sys.executable, "-X", "utf8", "-c", bootstrap]
            if suite == "tools":
                command.append("--execute-writes")
            start = time.perf_counter()
            print(f"Starting {suite}: private DB/cache/knowledge", flush=True)
            with (destination / "run.log").open("w", encoding="utf-8") as log:
                result = subprocess.run(command, cwd=sandbox, env=child_env, stdout=log, stderr=subprocess.STDOUT)
            if (sandbox / "reports").exists():
                shutil.copytree(sandbox / "reports", destination / "reports")
            trace_file = sandbox / "data" / "traces" / "agent_trace.jsonl"
            traces = []
            if trace_file.exists():
                shutil.copy2(trace_file, destination / "trace.jsonl")
                traces = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line]
            report_path = destination / "reports" / f"eval_{suite}.json"
            report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
            summary = {k: report[k] for k in ("total_cases", "passed_count", "failed_count", "skipped", "skip_reason") if k in report}
            summary.update(exit_code=result.returncode, duration_seconds=round(time.perf_counter()-start, 2))
            calls = [call for trace in traces for call in trace.get("token_usage", {}).get("llm_calls", [])]
            reported = [call["provider_usage"] for call in calls if call.get("provider_usage") is not None]
            summary["actual_chat_usage"] = {"calls": len(calls), "reported_calls": len(reported),
                "failed_calls": sum(not call.get("success") for call in calls),
                "tokens": {k: sum(r[k] for r in reported) for k in ("prompt_tokens", "completion_tokens", "total_tokens")} if reported else None}
            summary["reply_modes"] = {}
            for trace in traces:
                for event in trace.get("events", []):
                    if event.get("event_type") == "reply":
                        mode = event.get("message", {}).get("reply_mode", "unknown")
                        summary["reply_modes"][mode] = summary["reply_modes"].get(mode, 0) + 1
            (destination / "diagnostics.json").write_text(json.dumps(diagnostics(report, traces), ensure_ascii=False, indent=2), encoding="utf-8")
            manifest["suites"][suite] = summary
            save_manifest()
            print(f"Finished {suite}: {summary}", flush=True)
    manifest["source_inputs_unchanged"] = before == fingerprint(ROOT)
    save_manifest()


if __name__ == "__main__":
    main()
