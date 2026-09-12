from app.core.schemas import ToolResult
from app.storage.database import save_manual_review_to_db, get_active_refund_request_by_order_id_from_db
from app.storage.store import get_order_by_id
from app.storage.transactions import execute, lock_record, transaction
from app.core.security import authorize_order
from contextvars import ContextVar
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import sqlite3
from uuid import uuid4


_review_context = ContextVar("review_context", default=None)


def current_review_context():
    """Return a detached snapshot for a persisted human work item."""
    return deepcopy(_review_context.get())


@contextmanager
def review_context_scope(state):
    evidence = [deepcopy(item.result) for item in state.tool_results
                if item.tool_name == "policy_search" and item.success]
    token = _review_context.set({"conversation_id": state.conversation_id,
                                "route": state.route.model_dump(),
                                "trace_id": (state.trace or {}).get("trace_id"),
                                "history": deepcopy(state.history[-8:]),
                                "order_snapshot": deepcopy(state.order),
                                "policy_evidence": evidence})
    try:
        yield
    finally:
        _review_context.reset(token)


def create_manual_review(
    order_id: str | None,
    review_type: str,
    risk_level: str,
    risk_flags: list[str],
    user_request: str,
    related_id: str | None = None,
) -> ToolResult:
    """创建人工审核单，用于大额退款、异常账号、投诉升级等高风险动作。"""

    order = get_order_by_id(order_id) if order_id else None
    payload = {
            "order_id": order_id,
            "user_id": order.get("user_id") if order else None,
            "review_type": review_type,
            "risk_level": risk_level,
            "risk_flags": risk_flags,
            "user_request": user_request,
            "related_id": related_id,
            "context": current_review_context(),
            "next_step": "由人工客服复核订单、用户凭证、风控原因和政策依据后处理。",
        }
    if review_type == "refund" and order_id:
        # All creators serialize on the stable order row. Read pending reviews
        # without locking them: resolution locks review -> refund -> order, so
        # taking a review lock here would introduce a reverse-order deadlock.
        with transaction():
            row = lock_record("orders", "order_id", order_id)
            if row is None:
                raise ValueError("订单不存在，不能创建退款审核")
            locked_order = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
            authorize_order(locked_order)
            payload["user_id"] = locked_order.get("user_id")
            rows = execute("SELECT payload FROM manual_reviews WHERE order_id = ? AND review_type = 'refund' "
                           "AND status = 'pending_review' ORDER BY created_at, review_id LIMIT 1", (order_id,), fetch=True)
            if rows:
                saved = rows[0]["payload"] if isinstance(rows[0]["payload"], dict) else json.loads(rows[0]["payload"])
                if related_id and saved.get("related_id") not in {None, related_id}:
                    raise ValueError("已有审核关联其他退款申请，请先核实关联关系")
                review = {**saved, "idempotent_replay": True}
            else:
                refund = get_active_refund_request_by_order_id_from_db(order_id)
                if refund and refund["status"] != "pending_manual_review":
                    review = {"status": "not_required", "idempotent_replay": True,
                              "existing_refund": {key: refund.get(key) for key in ("refund_id", "order_id", "status")}}
                else:
                    if refund:
                        if related_id and related_id != refund["refund_id"]:
                            raise ValueError("当前退款与审核关联信息不一致")
                        payload["related_id"] = refund["refund_id"]
                    review = {**save_manual_review_to_db(payload), "idempotent_replay": False}
    else:
        review = save_manual_review_to_db(payload)

    if review.get("idempotent_replay") and review.get("review_id"):
        # Never append under the order lock: supplementary material locks only
        # the review row and may arrive while an operator is resolving it.
        from app.services.review_service import append_review_supplement
        trace_id = (payload.get("context") or {}).get("trace_id")
        submission_id = "chat_" + hashlib.sha256(str(trace_id).encode()).hexdigest() if trace_id else uuid4().hex
        recorded = append_review_supplement(review["review_id"], user_request, submission_id, context=payload.get("context"))
        review.update(status=recorded["review_status"], supplement_version=recorded["supplement_version"],
                      supplement_recorded=True)
        if review["status"] != "pending_review":
            refund = get_active_refund_request_by_order_id_from_db(order_id)
            if refund:
                review = {"status": "not_required", "idempotent_replay": True, "supplement_recorded": True,
                          "existing_refund": {key: refund.get(key) for key in ("refund_id", "order_id", "status")}}

    return ToolResult(
        tool_name="create_manual_review",
        success=True,
        result=review,
    )


def transfer_to_human(reason: str, user_request: str, priority: str = "normal") -> ToolResult:
    """Persist a claimable handoff; no external contact or business execution."""
    from app.core.security import current_principal
    from app.storage.database import save_ticket_to_db
    from app.storage.transactions import execute, transaction
    context = current_review_context()
    principal = current_principal.get()
    order_id = ((context or {}).get("order_snapshot") or {}).get("order_id")
    order = get_order_by_id(order_id) if order_id else None
    ticket = {
            "action": "transfer_to_human",
            "status": "pending_human_takeover",
            "issue_type": "人工接管",
            "order_id": order_id,
            "user_id": order.get("user_id") if order else principal.user_id if principal else None,
            "context": context,
            "reason": reason,
            "priority": priority,
            "user_request": user_request,
            "handoff_summary": f"用户诉求：{user_request}；转人工原因：{reason}。",
            "next_step": "已进入人工待办，等待客服认领并继续处理。",
    }
    if context and context.get("trace_id"):
        # The request trace identifies this action, never the question/answer.
        key = json.dumps([ticket["user_id"], ticket["order_id"], context.get("conversation_id"), context["trace_id"]], ensure_ascii=False)
        ticket["ticket_id"] = "T-H-" + hashlib.sha256(key.encode()).hexdigest()[:40]
    try:
        with transaction():
            saved = save_ticket_to_db(ticket)
    except Exception as error:
        duplicate = isinstance(error, sqlite3.IntegrityError) and getattr(error, "sqlite_errorcode", None) in {
            sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY, sqlite3.SQLITE_CONSTRAINT_UNIQUE}
        if not duplicate:
            try:
                from pymysql.err import IntegrityError
                duplicate = isinstance(error, IntegrityError) and error.args[0] == 1062
            except ImportError:
                pass
        if not duplicate or not ticket.get("ticket_id"):
            raise
        with transaction():
            rows = execute("SELECT payload FROM tickets WHERE ticket_id = ?", (ticket["ticket_id"],), fetch=True)
        if not rows:
            raise
        saved = rows[0]["payload"] if isinstance(rows[0]["payload"], dict) else json.loads(rows[0]["payload"])
        if saved.get("user_id") != ticket["user_id"] or saved.get("action") != "transfer_to_human":
            raise
    return ToolResult(tool_name="transfer_to_human", success=True, result=saved)
