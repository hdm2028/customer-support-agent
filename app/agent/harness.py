"""Bounded task harness inspired by Deep Agents' planning/feedback pattern.

The planner consumes validated route fields, never executable model-written
instructions. Agents still use the existing tool middleware and business guards.
"""
from copy import deepcopy

from app.agent.policies.guardrails import check_user_input
from app.core.schemas import ToolResult
from app.observability.tracing import add_trace_event
from app.tools.registry import can_agent_use_tool
from app.tools.middleware import task_tool_scope


# Task identity, business objective and responsible agent.
TASKS = (
    ("order_lookup", "核实订单归属、支付及售后状态", "after_sales_agent"),
    ("policy_search", "取得当前诉求所需的政策证据", "customer_agent"),
    ("risk_check", "评估风险并决定是否需要人工审核", "risk_agent"),
    ("create_manual_review", "提交人工审核，保留处理上下文", "after_sales_agent"),
    ("refund_apply", "创建退款申请，确认受理状态", "after_sales_agent"),
    ("create_ticket", "创建后续处理工单", "after_sales_agent"),
    ("transfer_to_human", "移交人工并保留业务上下文", "after_sales_agent"),
)


class AgentHarness:
    def __init__(self, orchestrator, *, max_steps=12):
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.orchestrator = orchestrator
        self.max_steps = max_steps

    def emit(self, state, event):
        if state.trace:
            add_trace_event(state.trace, event, deepcopy(state.harness))

    def refresh(self, state):
        route = state.route
        required = {
            "order_lookup": route.need_order and bool(state.order_id),
            "policy_search": route.need_policy,
            "risk_check": route.need_risk_check,
            "create_manual_review": bool(state.order_id) and (
                route.manual_review_required or self.orchestrator.needs_manual_review(state)
                or state.manual_review is not None),
            "refund_apply": route.need_refund_request,
            "create_ticket": route.need_ticket,
            "transfer_to_human": route.need_handoff,
        }
        old = {task["id"]: task for task in state.harness["tasks"]}
        tasks, dependencies = [], []
        review_blocks = self.orchestrator.manual_review_blocks_auto_refund(state)
        for tool, objective, owner in TASKS:
            if not required[tool] and tool not in old:
                continue
            task = old.get(tool, {"id": tool, "objective": objective, "agent": owner,
                                  "status": "pending", "result_refs": [], "reason": None})
            task["depends_on"] = list(dependencies)
            if task["status"] not in {"completed", "failed"}:
                if not required[tool]:
                    task.update(status="skipped", reason="route_updated_after_observation")
                elif review_blocks and tool in {"refund_apply", "create_ticket"}:
                    task.update(status="blocked", reason="waiting_for_manual_review")
                else:
                    task.update(status="pending", reason=None)
            tasks.append(task)
            if required[tool] and task["status"] not in {"blocked", "skipped"}:
                dependencies.append(tool)
        state.harness["tasks"] = tasks
        self.emit(state, "harness_plan")

    def stop(self, state, reason, *, error=None):
        self.orchestrator.block_execution(state, reason)
        if error:
            state.add_tool_result(ToolResult(tool_name="agent_execution", success=False, result={
                "error_type": type(error).__name__, "error_message": str(error),
                "fallback_action": "handoff_to_human", "reason": reason,
            }))
        for task in state.harness["tasks"]:
            if task["status"] in {"pending", "running"}:
                task.update(status="blocked", reason=reason)

    def finish(self, state, status=None):
        tasks = state.harness["tasks"]
        pending_review = bool(state.manual_review and state.manual_review.get("status")
                              in {"pending", "pending_review", "pending_manual_review"})
        state.harness["status"] = status or (
            "blocked" if state.blocked else
            "waiting_for_review" if pending_review or any(t["reason"] == "waiting_for_manual_review" for t in tasks) else
            "completed")
        state.harness["context"] = {
            "conversation_id": state.conversation_id, "order_id": state.order_id,
            "history_messages": len(state.history), "has_pending_task": bool(state.pending_task),
            "available_results": [item.tool_name for item in state.tool_results if item.success],
        }
        self.emit(state, "harness_finished")
        return state

    def run(self, state):
        state.harness = {"version": "task-harness-v1", "goal": state.user_message,
                         "status": "running", "steps_used": 0, "max_steps": self.max_steps, "tasks": []}
        passed, reason = check_user_input(state.user_message)
        if not passed:
            state.route.blocked_by_guardrail = True
            state.route.guardrail_reason = reason
        if self.orchestrator.should_skip_agents(state.route):
            return self.finish(state, "blocked" if state.route.blocked_by_guardrail else "waiting_for_input")
        invalid = self.orchestrator.validate_route_plan(state.route, state.trace)
        if invalid:
            state.add_tool_result(invalid)
            self.stop(state, "invalid_task_plan")
            return self.finish(state)

        self.refresh(state)
        for _ in range(self.max_steps):
            if state.blocked:
                self.stop(state, state.block_reason)
                break
            tasks = state.harness["tasks"]
            completed = {t["id"] for t in tasks if t["status"] == "completed"}
            task = next((t for t in tasks if t["status"] == "pending"
                         and set(t["depends_on"]) <= completed), None)
            if task is None:
                if any(t["status"] == "pending" for t in tasks):
                    self.stop(state, "task_dependencies_unresolved", error=RuntimeError("No ready task"))
                break
            if not can_agent_use_tool(task["agent"], task["id"]):
                self.stop(state, "task_permission_denied", error=PermissionError("Task owner cannot use tool"))
                break
            task["status"] = "running"
            state.next_agent = None  # Only the dependency plan may choose the next owner.
            state.harness["steps_used"] += 1
            self.emit(state, "harness_task_started")
            start = len(state.tool_results)
            try:
                allowed = {task["id"]}
                if task["id"] == "create_ticket":
                    allowed.add("ticket_decision")  # Existing read-only eligibility check.
                with task_tool_scope(allowed):
                    result = self.orchestrator.dispatch_agent(task["agent"], state)
            except Exception as error:
                task.update(status="failed", reason=type(error).__name__)
                self.stop(state, "agent_execution_failed", error=error)
                break
            task["result_refs"] = list(range(start, len(state.tool_results)))
            observed = next((r for r in result.tool_results if r.tool_name == task["id"]), None)
            if observed and observed.success and result.success:
                task.update(status="completed", reason=None)
            elif (task["id"] == "refund_apply" and observed and isinstance(observed.result, dict)
                  and observed.result.get("review_required")):
                task.update(status="blocked", reason="waiting_for_manual_review")
            else:
                task.update(status="failed", reason=result.error or "task_produced_no_successful_result")
                has_failure = any(not r.success for r in result.tool_results)
                self.stop(state, state.block_reason or "task_failed",
                          error=None if has_failure else RuntimeError(task["reason"]))
                break
            self.emit(state, "harness_task_result")
            self.refresh(state)
        else:
            if any(t["status"] == "pending" for t in state.harness["tasks"]):
                self.stop(state, "agent_loop_guard_reached", error=RuntimeError("Task budget exhausted"))
        self.orchestrator.append_chain_validation_result(state)
        if any(r.tool_name == "tool_chain_validation" and not r.success for r in state.tool_results):
            self.stop(state, "invalid_tool_chain")
        return self.finish(state)
