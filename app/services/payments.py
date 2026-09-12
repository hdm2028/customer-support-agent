"""Payment boundary and reconciliation. No built-in simulated production gateway."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import importlib
import os
from typing import Literal, Protocol

from app.domain.refund_policy import require_confirmed_refund_payment
from app.services.review_service import decode_payload, require_operator
from app.storage.database import (get_refund_request_from_db, save_notification_to_db,
                                  update_order_in_db, update_refund_request_in_db)
from app.storage.transactions import lock_record, transaction


class PaymentNotConfigured(RuntimeError):
    pass


@dataclass(frozen=True)
class RefundPaymentRequest:
    refund_id: str
    order_id: str
    amount: Decimal
    currency: str
    idempotency_key: str


@dataclass(frozen=True)
class PaymentObservation:
    refund_id: str
    state: Literal["processing", "succeeded", "failed", "unknown"]
    amount: Decimal
    currency: str
    provider_reference: str | None = None
    reason: str | None = None


class PaymentGateway(Protocol):
    def submit_refund(self, request: RefundPaymentRequest) -> PaymentObservation: ...
    def query_refund(self, request: RefundPaymentRequest) -> PaymentObservation: ...


def get_payment_gateway() -> PaymentGateway:
    # The deployment supplies a reviewed adapter for its specific payment API.
    # Adapter credentials/signature verification belong to that implementation.
    spec = os.getenv("PAYMENT_ADAPTER", "")
    if not spec:
        raise PaymentNotConfigured("支付渠道尚未接入，未发起资金操作")
    module, separator, factory = spec.partition(":")
    if not separator or not module or not factory:
        raise PaymentNotConfigured("PAYMENT_ADAPTER 必须是 module:factory")
    gateway = getattr(importlib.import_module(module), factory)()
    if not callable(getattr(gateway, "submit_refund", None)) or not callable(getattr(gateway, "query_refund", None)):
        raise PaymentNotConfigured("支付适配器未实现提交和查询接口")
    return gateway


def request_for(refund: dict) -> RefundPaymentRequest:
    amount = Decimal(str(refund["amount"])).quantize(Decimal("0.01"))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("退款金额无效")
    return RefundPaymentRequest(refund["refund_id"], refund["order_id"], amount,
                                refund.get("payment_currency", os.getenv("PAYMENT_CURRENCY", "CNY")),
                                "refund:" + refund["refund_id"])


def visible_refund(refund_id: str) -> dict:
    from app.storage.store import get_order_by_id
    refund = get_refund_request_from_db(refund_id)
    if not refund:
        raise LookupError("退款申请不存在")
    get_order_by_id(refund["order_id"])
    return refund


def _observe(refund_id: str, observation: PaymentObservation) -> dict:
    with transaction() as state:
        row = lock_record("refund_requests", "refund_id", refund_id)
        if row is None:
            raise LookupError("退款申请不存在")
        refund = decode_payload(row)
        request = request_for(refund)
        if (observation.refund_id != refund_id or observation.amount != request.amount
                or observation.currency != request.currency
                or observation.state not in {"processing", "succeeded", "failed", "unknown"}):
            raise ValueError("支付查询结果与退款申请不一致，需人工核实")
        if observation.state == "succeeded" and not observation.provider_reference:
            raise ValueError("支付成功结果缺少渠道凭证")
        target = {"processing": "payment_submitting", "succeeded": "refund_succeeded",
                  "failed": "failed", "unknown": "refund_unknown"}[observation.state]
        if refund["status"] in {"refund_succeeded", "failed"}:
            if refund["status"] != target:
                raise ValueError("渠道结果与已记录终态冲突，需人工对账")
            return {"success": True, "idempotent_replay": True, "refund": refund}
        if refund["status"] not in {"payment_submitting", "refund_unknown"}:
            raise ValueError("退款尚未提交支付处理，不能写入渠道结果")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        receipt = {"state": observation.state, "provider_reference": observation.provider_reference,
                   "amount": str(observation.amount), "currency": observation.currency,
                   "reason": observation.reason, "observed_at": now}
        history = refund.get("payment_observations", []) + [receipt]
        refund = update_refund_request_in_db(refund_id, {"status": target, "payment_observations": history,
                                                       "last_payment_observation": receipt})
        if target == "refund_succeeded":
            lock_record("orders", "order_id", refund["order_id"])
            order = update_order_in_db(refund["order_id"], {"order_status": "已退款", "after_sales_status": "refund_succeeded", "last_refund_id": refund_id})
            if not order:
                raise ValueError("退款订单不存在")
            save_notification_to_db({"user_id": refund.get("user_id"), "channel": "system", "refund_id": refund_id,
                                     "content": f"退款申请 {refund_id} 已由支付渠道确认处理成功，请以原支付渠道到账记录为准。"})
        state.invalidate_keys.add(f"idempotency:refund_apply:{refund['order_id']}")
        return {"success": True, "idempotent_replay": False, "refund": refund}


def _query_or_submit(refund_id: str, *, submit: bool) -> dict:
    operator = require_operator()
    gateway = get_payment_gateway()  # Fail before changing business state if unconfigured.
    with transaction() as state:
        row = lock_record("refund_requests", "refund_id", refund_id)
        if row is None:
            raise LookupError("退款申请不存在")
        refund = decode_payload(row)
        request = request_for(refund)
        if submit:
            if refund["status"] in {"refund_succeeded", "failed"}:
                return {"success": True, "idempotent_replay": True, "refund": refund}
            if refund["status"] != "refund_processing":
                raise ValueError("退款未就绪或结果尚待核实，请查询状态，不能重复提交")
            order_row = lock_record("orders", "order_id", refund["order_id"])
            if order_row is None:
                raise ValueError("退款订单不存在，无法核实支付状态")
            require_confirmed_refund_payment(decode_payload(order_row))
            refund = update_refund_request_in_db(refund_id, {
                "status": "payment_submitting", "payment_currency": request.currency,
                "payment_idempotency_key": request.idempotency_key,
                "payment_submitted_by": operator.user_id,
            })
            state.invalidate_keys.add(f"idempotency:refund_apply:{refund['order_id']}")
        elif refund["status"] not in {"payment_submitting", "refund_unknown", "refund_succeeded", "failed"}:
            raise ValueError("退款尚未提交支付渠道")
    # The network call is outside the DB transaction. Once dispatch is recorded,
    # crashes/timeouts must be resolved by query using the same idempotency key.
    try:
        observation = gateway.submit_refund(request) if submit else gateway.query_refund(request)
        return _observe(refund_id, observation)
    except Exception as error:
        with transaction() as state:
            row = lock_record("refund_requests", "refund_id", refund_id)
            current = decode_payload(row)
            if current["status"] not in {"refund_succeeded", "failed"}:
                update_refund_request_in_db(refund_id, {"status": "refund_unknown", "payment_error_type": type(error).__name__})
                state.invalidate_keys.add(f"idempotency:refund_apply:{current['order_id']}")
        raise


def submit_refund_payment(refund_id: str) -> dict:
    return _query_or_submit(refund_id, submit=True)


def reconcile_refund_payment(refund_id: str) -> dict:
    return _query_or_submit(refund_id, submit=False)
