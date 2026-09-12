"""Durable graph runner shared by authenticated HTTP and SSE entrypoints."""
from contextlib import contextmanager
from filelock import FileLock
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sqlite3
from threading import Lock
from time import perf_counter
from uuid import uuid4

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START

from app.agent.entry.workflow import build_agent_workflow, build_initial_state, dump_model


def checkpoint_node(name, node):
    """Turn legacy in-place mutations into explicit, checkpointed state updates."""
    def execute(state):
        working = deepcopy(state)
        trace = working["trace"]
        # perf_counter belongs to the current process. Rebase it from durable
        # wall time before every node; reported duration includes time suspended.
        started = datetime.fromisoformat(trace["start_at"])
        elapsed = max(0.0, (datetime.now(tz=started.tzinfo) - started).total_seconds())
        trace["_start_perf"] = perf_counter() - elapsed
        trace["duration_basis"] = "wall_clock_including_recovery_pause"
        trace.setdefault("graph_nodes", []).append(name)
        updates = node(working)
        return {**working, **updates}
    return execute


class DurableGraphRuntime:
    """One graph.stream path for buffered responses, SSE and resume.

    A run is one user turn; conversation_id remains the business history key.
    Thread and file locks serialize workers sharing a local checkpoint path.
    Recovery is at graph-node boundaries, not inside the tool execution loop.
    """
    def __init__(self, checkpoint_path: Path):
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._file_lock = FileLock(str(checkpoint_path.resolve()) + ".lock", timeout=30, thread_local=False)
        self.connection = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
        self.connection.execute("PRAGMA synchronous=FULL")
        self.saver = SqliteSaver(self.connection, serde=JsonPlusSerializer(
            allowed_msgpack_modules=[
                ("app.core.schemas", "RouteDecision"),
                ("app.core.schemas", "ToolResult"),
            ],
        ))
        with self._locked():
            self.saver.setup()
            self.connection.execute("CREATE TABLE IF NOT EXISTS run_owners (run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, conversation_id TEXT NOT NULL)")
            self.connection.commit()
        self.graph = build_agent_workflow(checkpointer=self.saver, node_adapter=checkpoint_node)

    @contextmanager
    def _locked(self):
        with self._lock, self._file_lock:
            yield

    def owner(self, run_id):
        with self._locked():
            row = self.connection.execute("SELECT owner_id, conversation_id FROM run_owners WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    @staticmethod
    def config(run_id):
        return {"configurable": {"thread_id": run_id}}

    def create(self, message, conversation_id=None, *, use_llm=False, stream_tokens=False, owner_id=None):
        run_id = str(uuid4())
        initial = build_initial_state(message, conversation_id, use_llm)
        initial.update(stream_tokens=stream_tokens, durable_turn=True)
        with self._locked():
            if owner_id is not None:
                previous = self.connection.execute("SELECT run_id FROM run_owners WHERE conversation_id = ?",
                                                   (initial["real_conversation_id"],)).fetchall()
                for (previous_id,) in previous:
                    saved = self.graph.get_state(self.config(previous_id))
                    if saved.values and saved.next:
                        raise ValueError(f"Conversation has an unfinished run: {previous_id}")
                self.connection.execute("INSERT INTO run_owners VALUES (?, ?, ?)",
                                        (run_id, owner_id, initial["real_conversation_id"]))
                self.connection.commit()
            self.graph.update_state(
                self.config(run_id), initial,
                as_node=START,
            )
        return run_id

    def _snapshot(self, run_id):
        snapshot = self.graph.get_state(self.config(run_id))
        if not snapshot.values:
            raise KeyError(run_id)
        return snapshot

    @staticmethod
    def _view(run_id, snapshot):
        values = snapshot.values
        errors = [str(task.error) for task in snapshot.tasks if task.error]
        completed = not snapshot.next and "result" in values
        return {
            "run_id": run_id,
            "conversation_id": values["real_conversation_id"],
            "status": "completed" if completed else "failed" if errors else "suspended",
            "next_nodes": list(snapshot.next),
            "completed_nodes": values["trace"].get("graph_nodes", []),
            "checkpoint_id": snapshot.config["configurable"].get("checkpoint_id"),
            "trace_id": values["trace"]["trace_id"],
            "errors": errors,
            "tool_results": [dump_model(item) for item in values.get("tool_results", [])],
            "result": values.get("result") if completed else None,
        }

    def status(self, run_id):
        with self._locked():
            return self._view(run_id, self._snapshot(run_id))

    def events(self, run_id, *, pause_after_tools=False):
        with self._locked():
            snapshot = self._snapshot(run_id)
            if not snapshot.next and "result" in snapshot.values:
                yield {"type": "done", "content": self._view(run_id, snapshot)}
                return
            yield {"type": "run", "content": self._view(run_id, snapshot)}
            # None resumes saved work. Never re-submit the original input.
            for mode, update in self.graph.stream(
                None, self.config(run_id), stream_mode=["updates", "custom"], durability="sync",
                interrupt_after=["orchestrate_agents"] if pause_after_tools else [],
            ):
                if mode == "custom":
                    yield {**update, "run_id": run_id}
                    continue
                for name, state in update.items():
                    if name == "__interrupt__":
                        continue
                    yield {"type": "node", "node": name, "run_id": run_id}
                    if name == "route":
                        yield {"type": "route", "content": dump_model(state["route"])}
                    elif name == "orchestrate_agents":
                        yield {"type": "task_plan", "content": state["orchestration"].get("harness", {})}
                        for item in state["tool_results"]:
                            yield {"type": "tool_result", "content": dump_model(item)}
                    elif name == "generate_reply":
                        yield {"type": "message", "content": state["reply"]}
            # Only the terminal notification asserts a committed checkpoint.
            view = self._view(run_id, self._snapshot(run_id))
            yield {"type": "done" if view["status"] == "completed" else "paused", "content": view}

    def run(self, run_id, *, pause_after_tools=False):
        final = None
        for event in self.events(run_id, pause_after_tools=pause_after_tools):
            if event["type"] in {"done", "paused"}:
                final = event["content"]
        return final

    def close(self):
        with self._lock:
            self.connection.close()
