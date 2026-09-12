"""Replay observed runtime routes, not expected labels, for downstream A/B."""
from collections import defaultdict, deque
import json
from pathlib import Path


def install(path):
    from app.agent.entry import workflow
    from app.core.schemas import RouteDecision
    observed = defaultdict(deque)
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        trace = json.loads(line)
        events = trace.get("events", [])
        route = next((e["message"] for e in events if e["event_type"] == "route"), None)
        context = next((e["message"] for e in events if e["event_type"] == "pending_task"), {})
        if route is not None:
            query = context.get("effective_user_message") or trace["user_message"]
            observed[query].append(route)
    def replay(query):
        if not observed.get(query):
            raise RuntimeError("No observed runtime route for replay input")
        return RouteDecision(**observed[query].popleft())
    workflow.route_user_request = replay
    def verify_consumed():
        if any(observed.values()):
            raise RuntimeError("Not all observed runtime routes were consumed")
    return verify_consumed
