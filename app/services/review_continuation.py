"""Concrete continuations for approved non-refund reviews."""
import json

from app.mq.queue import REFUND_CREATED_TOPIC, publish_message
from app.storage.database import (save_refund_request_to_db, save_ticket_to_db,
                                  update_order_in_db, update_refund_request_in_db)
from app.storage.transactions import execute, lock_record


def requested_action(review):
    if review.get("review_type") in {"cancel_order", "address_change"}:
        return review["review_type"]
    route = (review.get("context") or {}).get("route") or {}
    if route.get("action_type") == "execute" and route.get("intent") in {"cancel_order", "address_change"}:
        return route["intent"]
    return "followup_ticket"


def require_unshipped_order(order):
    if order.get("order_status") not in {"待支付", "待付款", "待发货", "待出库", "已支付"}:
        raise ValueError("当前订单不能直接取消或修改地址，需要核实物流处理路径")
    shipping = str(order.get("shipping_status") or "")
    blocked = ("正在拣货", "拣货中", "已拣货", "出库中", "已出库", "已发货", "已揽收", "已签收", "配送中", "派送中")
    if order.get("signed_date") or any(marker in shipping for marker in blocked):
        raise ValueError("订单已进入拣货、出库或配送流程，不能直接完成此操作")
    if order.get("after_sales_status") not in {None, "", "none"}:
        raise ValueError("订单已有售后处理，需要先核实当前业务状态")


def continue_approved_review(review, operator_id, note, *, new_address=None):
    action = requested_action(review)
    if new_address is not None and action != "address_change":
        raise ValueError("当前审核不是地址修改，不能提交新地址")
    if action == "followup_ticket":
        ticket = save_ticket_to_db({"order_id": review.get("order_id"), "user_id": review.get("user_id"),
            "issue_type": "人工复核跟进", "priority": "high", "status": "pending_manual_review",
            "user_request": review["user_request"], "review_id": review["review_id"],
            "approval_note": note, "approved_by": operator_id, "context": review.get("context")})
        return {"action": action, "ticket_id": ticket["ticket_id"],
                "next_step": f"审核已通过，已创建跟进工单 {ticket['ticket_id']}。"}

    row = lock_record("orders", "order_id", review.get("order_id"))
    if row is None:
        raise ValueError("订单不存在，不能继续处理")
    order = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    require_unshipped_order(order)
    active = execute("SELECT refund_id FROM refund_requests WHERE order_id = ? AND status NOT IN ('failed', 'rejected', 'cancelled', 'canceled')",
                     (order["order_id"],), fetch=True)
    if active:
        raise ValueError("订单已有有效退款申请，请先处理关联退款")
    audit = {"review_id": review["review_id"], "operator_id": operator_id, "note": note}
    if action == "address_change":
        if not new_address or not 6 <= len(new_address.strip()) <= 500:
            raise ValueError("请填写已向用户核实的新收货地址（6–500 字）")
        update_order_in_db(order["order_id"], {"shipping_address": new_address.strip(), "last_address_change": audit})
        return {"action": action, "order_id": order["order_id"], "new_address": new_address.strip(),
                "next_step": "收货地址已更新，请向用户确认新的收货信息。"}
    if order.get("payment_status") not in {"paid", "unpaid"}:
        raise ValueError("支付状态尚未确认，不能直接取消订单")
    refund = None
    if order["payment_status"] == "paid":
        previous = execute("SELECT refund_id FROM refund_requests WHERE idempotency_key = ?",
                           ("refund_apply:" + order["order_id"],), fetch=True)
        if previous:
            raise ValueError("订单存在既往退款业务，请核实关联申请后继续")
        refund = save_refund_request_to_db({"order_id": order["order_id"], "user_id": order.get("user_id"),
            "amount": order["amount"], "reason": "approved_order_cancellation", "status": "queued",
            "risk_level": review["risk_level"], "review_approved": True, "approval": audit,
            "user_request": review["user_request"], "idempotency_key": "refund_apply:" + order["order_id"],
            "next_step": "订单取消已确认，退款申请等待处理，尚未完成退款。"})
        message = publish_message(REFUND_CREATED_TOPIC, {"refund_id": refund["refund_id"], "order_id": order["order_id"],
                                                       "user_id": order.get("user_id"), "review_required": False})
        update_refund_request_in_db(refund["refund_id"], {"mq_message_id": message["message_id"]})
    update_order_in_db(order["order_id"], {"order_status": "已取消", "fulfillment_cancelled": True, "cancellation": audit})
    return {"action": action, "order_id": order["order_id"], "refund_id": refund["refund_id"] if refund else None,
            "next_step": "订单已取消，退款申请已创建，等待处理。" if refund else "未支付订单已取消，不会进入发货流程。"}
