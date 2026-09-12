"""Record raw router responses; optionally replay only the first model call."""
from collections import deque
import json
from pathlib import Path
from time import perf_counter


def install(output, replay_from=None):
    from app.agent.routing import llm_router
    real_call = llm_router.call_zhipu_chat
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    observed = deque()
    replay_errors = []
    if replay_from:
        observed.extend(json.loads(line) for line in Path(replay_from).read_text(encoding="utf-8").splitlines())
        if any(len(row["messages"]) != 2 or row.get("error") for row in observed):
            raise ValueError("First-response replay requires a successful unrepaired A recording")
    def call(messages):
        record = {"messages": messages, "source": "live", "response": None, "error": None}
        started = perf_counter()
        try:
            if replay_from and len(messages) == 2:
                if not observed:
                    replay_errors.append("missing_first_response")
                    raise RuntimeError("No first response left for semantic replay")
                first = observed.popleft()
                if first["messages"] != messages:
                    replay_errors.append("input_or_prompt_mismatch")
                    raise RuntimeError("Semantic replay input/prompt differs from recorded A")
                record.update(source="recorded_first_response", response=first["response"])
                return first["response"]
            record["response"] = real_call(messages)
            return record["response"]
        except Exception as error:
            record["error"] = type(error).__name__
            raise
        finally:
            record["duration_seconds"] = round(perf_counter() - started, 4)
            with target.open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
    llm_router.call_zhipu_chat = call
    def verify():
        valid = not observed and not replay_errors
        (target.parent / "semantic_replay_verification.json").write_text(
            json.dumps({"valid": valid, "remaining": len(observed), "errors": replay_errors}), encoding="utf-8")
        if not valid:
            raise RuntimeError("Semantic first-response replay was not exact or fully consumed")
    return verify
