"""Audited human decisions and continuation of an approved refund intent."""
import json
from datetime import datetime
from copy import deepcopy
import re

from app.core.security import authorize_order, current_principal
from app.mq.queue import REFUND_CREATED_TOPIC, publish_message
from app.storage.database import (get_active_refund_request_by_order_id_from_db,
                                  get_refund_request_from_db, save_refund_request_to_db,
                                  update_refund_request_in_db)
from app.storage.transactions import execute, lock_record, transaction


def require_operator():
    principal = current_principal.get()
    if principal is None or principal.role != "admin":
        raise PermissionError("需要管理员权限")
    return principal


def decode_payload(row):
    return row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])


def review_details(review_id: str):
    require_operator()
    with transaction():
        row = lock_record("manual_reviews", "review_id", review_id)
        if row is None:
            raise LookupError("审核单不存在")
        return decode_payload(row)


def append_review_supplement(review_id: str, text: str, submission_id: str, *, context: dict | None = None) -> dict:
    """Append material under the review lock without changing its decision.

    HTTP callers cannot supply context; tool callers may attach observed context.
    This must run after releasing any order lock held during review creation.
    """
    text = text.strip()
    if not 1 <= len(text) <= 2000 or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", submission_id):
        raise ValueError("请提供 1–2000 字补充说明及有效提交编号")
    with transaction():
        row = lock_record("manual_reviews", "review_id", review_id)
        if row is None:
            raise LookupError("审核单不存在")
        review = decode_payload(row)
        authorize_order({"user_id": review.get("user_id")})
        actor = current_principal.get()
        actor_id = actor.user_id if actor else review.get("user_id")
        supplements = list(review.get("supplements") or [])
        previous = next((item for item in supplements if item["submission_id"] == submission_id
                         and item["submitted_by"] == actor_id), None)
        if previous:
            if previous["text"] != text:
                raise ValueError("提交编号已用于其他补充内容")
            return {"success": True, "idempotent_replay": True, "review_id": review_id,
                    "supplement_version": review.get("supplement_version", 0),
                    "review_status": review["status"], "submission": previous}
        version = int(review.get("supplement_version", 0)) + 1
        now = datetime.now().isoformat(timespec="seconds")
        submission = {"submission_id": submission_id, "submitted_by": actor_id,
                      "submitted_at": now, "text": text, "version": version,
                      "review_status_at_submission": review["status"], "context": deepcopy(context)}
        supplements.append(submission)
        review.update(supplements=supplements, supplement_version=version, updated_at=now)
        execute("UPDATE manual_reviews SET payload = ?, updated_at = ? WHERE review_id = ?",
                (json.dumps(review, ensure_ascii=False), now, review_id))
        return {"success": True, "idempotent_replay": False, "review_id": review_id,
                "supplement_version": version, "review_status": review["status"], "submission": submission}


def resolve_review(review_id: str, decision: str, note: str, *, new_address: str | None = None,
                   supplement_version: int | None = None) -> dict:
    operator = require_operator()
    if decision not in {"approve", "reject"} or not note.strip():
        raise ValueError("审核决定和审核说明不能为空")
    now = datetime.now().isoformat(timespec="seconds")
    with transaction() as state:
        row = lock_record("manual_reviews", "review_id", review_id)
        if row is None:
            raise LookupError("审核单不存在")
        review = decode_payload(row)
        status = "approved" if decision == "approve" else "rejected"
        if review["status"] != "pending_review":
            if review["status"] == status:
                return {"success": True, "idempotent_replay": True, "review": review}
            raise ValueError("审核单已处理，不能覆盖已有决定")

        current_version = int(review.get("supplement_version", 0))
        if (supplement_version is not None and supplement_version != current_version) or (current_version and supplement_version is None):
            raise ValueError("审核材料已有更新，请重新查看补充内容后提交决定")

        refund = None
        continuation = None
        if review.get("review_type") == "refund":
            if review.get("related_id"):
                lock_record("refund_requests", "refund_id", review["related_id"])
                refund = get_refund_request_from_db(review["related_id"])
                if refund is None or refund["order_id"] != review.get("order_id"):
                    raise ValueError("审核单关联的退款申请无效")
            else:
                refund = get_active_refund_request_by_order_id_from_db(review.get("order_id"))
                if refund:
                    lock_record("refund_requests", "refund_id", refund["refund_id"])
                    refund = get_refund_request_from_db(refund["refund_id"])
            if refund and refund["status"] != "pending_manual_review":
                raise ValueError("退款申请不处于待审核状态")

            if decision == "approve":
                order_row = lock_record("orders", "order_id", review.get("order_id"))
                if order_row is None:
                    raise ValueError("订单不存在，不能继续退款")
                order = decode_payload(order_row)
                if order.get("payment_status") != "paid":
                    raise ValueError("订单尚未确认支付成功，不能批准退款执行")
                if not refund:
                    if "退款" in order.get("order_status", "") or "退货审核中" in order.get("order_status", ""):
                        raise ValueError("订单已有售后处理，请核实关联申请后继续")
                    refund = save_refund_request_to_db({
                        "order_id": order["order_id"], "user_id": order.get("user_id"),
                        "amount": order["amount"], "reason": "manual_review_approved",
                        "status": "pending_manual_review", "risk_level": review["risk_level"],
                        "user_request": review["user_request"],
                    })
                refund = update_refund_request_in_db(refund["refund_id"], {
                    "status": "queued", "review_approved": True,
                    "approval": {"review_id": review_id, "operator_id": operator.user_id, "note": note, "at": now},
                    "next_step": "审核已通过，等待退款处理。",
                })
                message = publish_message(REFUND_CREATED_TOPIC, {
                    "refund_id": refund["refund_id"], "order_id": refund["order_id"],
                    "user_id": refund.get("user_id"), "review_required": False,
                })
                refund = update_refund_request_in_db(refund["refund_id"], {"mq_message_id": message["message_id"]})
            elif refund:
                refund = update_refund_request_in_db(refund["refund_id"], {
                    "status": "rejected", "review_approved": False,
                    "rejection_reason": note, "next_step": "审核未通过，请查看审核说明。",
                })
            if refund:
                state.invalidate_keys.add(f"idempotency:refund_apply:{refund['order_id']}")
                review["related_id"] = refund["refund_id"]

        elif decision == "approve":
            from app.services.review_continuation import continue_approved_review
            continuation = continue_approved_review(review, operator.user_id, note, new_address=new_address)
            review["continuation"] = continuation

        review.update(status=status, updated_at=now,
                      resolution={"decision": decision, "note": note, "operator_id": operator.user_id, "at": now,
                                  "supplement_version": current_version},
                      next_step=("审核已通过，退款申请等待处理。" if refund and decision == "approve" else
                                 "审核已拒绝。" if decision == "reject" else "审核已通过，等待人工完成对应业务操作。"))
        if continuation:
            review["next_step"] = continuation["next_step"]
        execute("UPDATE manual_reviews SET status = ?, payload = ?, updated_at = ? WHERE review_id = ?",
                (status, json.dumps(review, ensure_ascii=False), now, review_id))
        return {"success": True, "idempotent_replay": False, "review": review, "refund": refund}
