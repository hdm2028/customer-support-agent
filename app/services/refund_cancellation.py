"""Withdraw an accepted refund before payment dispatch, under database locks."""
import json
from datetime import datetime

from app.core.security import authorize_order, current_principal
from app.services.review_service import decode_payload
from app.storage.database import (get_refund_request_from_db, save_notification_to_db,
                                  update_order_in_db, update_refund_request_in_db)
from app.storage.transactions import execute, lock_record, transaction


def cancel_refund(refund_id: str, note: str):
    actor = current_principal.get()
    if actor is None:
        raise PermissionError("请先登录后撤销退款申请")
    if not note.strip():
        raise ValueError("请填写撤销原因")
    initial = get_refund_request_from_db(refund_id)
    if initial is None:
        raise LookupError("退款申请不存在")
    now = datetime.now().isoformat(timespec="seconds")
    with transaction() as state:
        # Same order as review resolution: reviews -> refund -> order.
        # A secondary-index FOR UPDATE scan can hold the status index while
        # waiting for a primary row locked by approval, whose status UPDATE
        # then needs that same index. Enumerate without locks, acquire primary
        # rows in order, and recheck their current state after waiting.
        review_ids = execute("SELECT review_id FROM manual_reviews WHERE order_id = ? AND status = 'pending_review' "
                             "ORDER BY review_id", (initial["order_id"],), fetch=True)
        reviews = []
        for candidate in review_ids:
            current = lock_record("manual_reviews", "review_id", candidate["review_id"])
            if current and current["order_id"] == initial["order_id"] and current["status"] == "pending_review":
                reviews.append(current)
        row = lock_record("refund_requests", "refund_id", refund_id)
        if row is None:
            raise LookupError("退款申请不存在")
        refund = decode_payload(row)
        order_row = lock_record("orders", "order_id", refund["order_id"])
        if order_row is None:
            raise ValueError("订单不存在，需要人工核实")
        order = decode_payload(order_row)
        authorize_order(order)
        if refund["status"] == "cancelled":
            return {"success": True, "idempotent_replay": True, "refund": refund}
        if refund.get("reason") == "approved_order_cancellation":
            raise ValueError("订单已取消，关联退款需继续处理；如需恢复订单请联系人工核实")
        if refund["status"] not in {"queued", "pending_manual_review", "refund_processing"}:
            raise ValueError("当前退款不能撤销；已提交支付或结果待核实时请先查询渠道状态")
        if refund["status"] == "refund_processing":
            before = refund.get("order_before_refund")
            if (not before or order.get("last_refund_id") != refund_id
                    or order.get("order_status") != "退款处理中"
                    or order.get("after_sales_status") != "refund_processing"
                    or any(order.get(key) != before.get(key) for key in ("shipping_status", "signed_date"))):
                raise ValueError("订单状态已变化或缺少处理前记录，需要人工核实后撤销")
            update_order_in_db(refund["order_id"], {key: before.get(key) for key in
                                                   ("order_status", "after_sales_status", "last_refund_id")})
        refund = update_refund_request_in_db(refund_id, {
            "status": "cancelled", "next_step": "退款申请已撤销，未继续提交支付渠道。",
            "cancellation": {"actor_id": actor.user_id, "note": note.strip(), "at": now},
        })
        for row in reviews:
            review = decode_payload(row)
            if review.get("review_type") != "refund" or review.get("related_id") not in {None, refund_id}:
                continue
            review.update(status="cancelled", updated_at=now, next_step="关联退款已撤销，无需继续审核。",
                          resolution={"decision": "cancel", "operator_id": actor.user_id, "note": note.strip(), "at": now})
            execute("UPDATE manual_reviews SET status = ?, payload = ?, updated_at = ? WHERE review_id = ?",
                    ("cancelled", json.dumps(review, ensure_ascii=False), now, review["review_id"]))
        save_notification_to_db({"user_id": refund.get("user_id"), "channel": "system", "refund_id": refund_id,
                                 "content": f"退款申请 {refund_id} 已撤销，未继续提交支付渠道。"})
        state.invalidate_keys.add(f"idempotency:refund_apply:{refund['order_id']}")
        return {"success": True, "idempotent_replay": False, "refund": refund}
