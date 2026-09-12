"""Audited manual follow-up; closing a ticket does not execute business actions."""
import json
import base64
import binascii
from datetime import datetime

from app.core.security import current_principal
from app.services.review_service import decode_payload, require_operator
from app.storage.transactions import execute, lock_record, transaction


PENDING_STATUSES = {"pending_human_review", "pending_manual_review", "pending_human_takeover"}


def list_tickets_page(limit=50, status="all", cursor=None):
    principal = current_principal.get()
    if principal is None:
        raise PermissionError("缺少请求身份")
    if not 1 <= limit <= 100 or status not in {"all", "open", "resolved"}:
        raise ValueError("工单筛选参数无效")
    clauses, parameters = [], []
    if principal.role != "admin":
        clauses.append("user_id = ?")
        parameters.append(principal.user_id)
    if status == "open":
        clauses.append("status IN (?, ?, ?, ?)")
        parameters.extend(["pending_human_review", "pending_manual_review", "pending_human_takeover", "in_progress"])
    elif status == "resolved":
        clauses.append("status = ?")
        parameters.append(status)
    if cursor is not None:
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
                raise ValueError()
            point = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if (not isinstance(point, list) or len(point) != 2
                    or any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in point)):
                raise ValueError()
            datetime.fromisoformat(point[0])
        except (ValueError, TypeError, binascii.Error, UnicodeError):
            raise ValueError("工单分页位置无效，请刷新列表") from None
        clauses.append("(created_at < ? OR (created_at = ? AND ticket_id < ?))")
        parameters.extend([point[0], point[0], point[1]])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with transaction():
        rows = execute("SELECT payload, created_at, ticket_id FROM tickets" + where
                       + " ORDER BY created_at DESC, ticket_id DESC LIMIT ?",
                       (*parameters, limit + 1), fetch=True)
    page, next_cursor = rows[:limit], None
    if len(rows) > limit:
        last = page[-1]
        point = [str(last["created_at"]), last["ticket_id"]]
        next_cursor = base64.urlsafe_b64encode(json.dumps(point).encode()).decode()
    return {"success": True, "count": len(page), "data": [decode_payload(row) for row in page],
            "next_cursor": next_cursor}


def _read(ticket_id):
    row = lock_record("tickets", "ticket_id", ticket_id)
    if row is None:
        raise LookupError("工单不存在")
    return decode_payload(row)


def ticket_details(ticket_id):
    principal = current_principal.get()
    if principal is None:
        raise PermissionError("缺少请求身份")
    with transaction():
        ticket = _read(ticket_id)
        if principal.role != "admin" and ticket.get("user_id") != principal.user_id:
            raise PermissionError("工单不可访问")
        return ticket


def _save(ticket, operator, action, note, **audit_details):
    now = datetime.now().isoformat(timespec="microseconds")
    ticket["updated_at"] = now
    ticket.setdefault("audit", []).append({**audit_details, "action": action, "operator_id": operator.user_id,
                                          "note": note, "at": now})
    execute("UPDATE tickets SET status = ?, payload = ? WHERE ticket_id = ?",
            (ticket["status"], json.dumps(ticket, ensure_ascii=False), ticket["ticket_id"]))
    return {"success": True, "idempotent_replay": False, "ticket": ticket}


def claim_ticket(ticket_id):
    operator = require_operator()
    with transaction():
        ticket = _read(ticket_id)
        if ticket["status"] == "in_progress" and ticket.get("assigned_to") == operator.user_id:
            return {"success": True, "idempotent_replay": True, "ticket": ticket}
        if ticket["status"] not in PENDING_STATUSES or ticket.get("assigned_to"):
            raise ValueError("工单已被认领或结案，请刷新状态")
        ticket.update(status="in_progress", assigned_to=operator.user_id,
                      next_step="已由人工认领，等待处理结果。")
        return _save(ticket, operator, "claim", "认领工单")


def resolve_ticket(ticket_id, outcome, note):
    operator = require_operator()
    if outcome not in {"answered", "rejected", "withdrawn"} or not isinstance(note, str) or not 1 <= len(note.strip()) <= 2000:
        raise ValueError("请选择处理结果并填写 1 至 2000 字的结案说明")
    note = note.strip()
    with transaction():
        ticket = _read(ticket_id)
        if ticket.get("assigned_to") != operator.user_id:
            raise ValueError("只有认领此工单的管理员可以结案")
        resolution = {"outcome": outcome, "note": note, "operator_id": operator.user_id}
        if ticket["status"] == "resolved" and ticket.get("resolution") == resolution:
            return {"success": True, "idempotent_replay": True, "ticket": ticket}
        if ticket["status"] != "in_progress":
            raise ValueError("工单不在处理中，不能覆盖已有结案结果")
        ticket.update(status="resolved", resolution=resolution,
                      next_step="人工跟进已结案；订单及退款进度请查看对应业务记录。")
        return _save(ticket, operator, "resolve", note)


def reassign_ticket(ticket_id, target_user_id, expected_assignee, note):
    from app.core.security import configured_operators
    operator = require_operator()
    if target_user_id not in configured_operators():
        raise ValueError("目标处理人不是已配置的管理员")
    if not isinstance(note, str) or not 1 <= len(note.strip()) <= 2000:
        raise ValueError("请填写 1 至 2000 字的交接说明")
    note = note.strip()
    with transaction():
        ticket = _read(ticket_id)
        previous = ticket.get("assigned_to")
        last = (ticket.get("audit") or [{}])[-1]
        replay = (last.get("action") == "reassign" and last.get("operator_id") == operator.user_id
                  and last.get("from_operator_id") == expected_assignee and last.get("assigned_to") == target_user_id
                  and last.get("note") == note and previous == target_user_id)
        if ticket["status"] == "in_progress" and replay:
            return {"success": True, "idempotent_replay": True, "ticket": ticket}
        if ticket["status"] != "in_progress" or previous != expected_assignee:
            raise ValueError("工单状态或处理人已变化，请刷新后再转派")
        if previous == target_user_id:
            raise ValueError("工单已经由此管理员处理")
        ticket.update(assigned_to=target_user_id, next_step="已交接给新的处理人，等待继续处理。")
        return _save(ticket, operator, "reassign", note, from_operator_id=previous, assigned_to=target_user_id)
