from __future__ import annotations

import json
import os
import re
from typing import Any

from app.agent.routing.semantic import SemanticRoute
from app.agent.routing.semantic_prompt import SYSTEM_PROMPT
from app.llm.llm_client import call_zhipu_chat


VALID_INTENTS = {
    "address_change",
    "cancel_order",
    "return_refund",
    "shipping_exception",
    "warranty_repair",
    "payment_invoice",
    "complaint",
    "membership",
    "order_lookup",
    "general_support",
}


VALID_ACTION_TYPES = {
    "query",
    "execute",
    "handoff",
    "unknown",
}


VALID_TOPICS = {
    # return_refund
    "refund_policy",
    "refund_timing",
    "refund_eligibility",
    "refund_apply",
    "return_apply",

    # cancel_order
    "cancel_policy",
    "cancel_apply",

    # address_change
    "address_change_policy",
    "address_change_apply",

    # shipping_exception
    "shipping_status",
    "shipping_delay",
    "shipping_exception",
    "lost_package",
    "shipping_policy",

    # warranty_repair
    "warranty_policy",
    "repair_apply",
    "replacement",
    "product_failure",

    # payment_invoice
    "payment_status",
    "payment_failed",
    "duplicate_charge",
    "invoice_policy",
    "invoice_apply",
    "invoice_change",

    # complaint
    "complaint",
    "escalation",

    # membership
    "membership_policy",
    "membership_benefit",

    # order_lookup
    "order_status",

    # general_support
    "general_question",

    # handoff
    "human_handoff",
}


def _extract_json_object(
    text: str,
) -> dict[str, Any]:
    """
    从 LLM 返回文本中提取 JSON 对象。

    支持：
    1. 纯 JSON
    2. ```json ... ```
    3. JSON 前后夹杂少量文本
    """

    if not text:
        raise ValueError(
            "LLM returned empty response"
        )

    cleaned = text.strip()

    # 去掉 Markdown code fence
    if cleaned.startswith("```"):
        cleaned = re.sub(
            r"^```(?:json)?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )

        cleaned = re.sub(
            r"\s*```$",
            "",
            cleaned,
        )

        cleaned = cleaned.strip()

    # 优先直接解析
    try:
        result = json.loads(cleaned)

        if not isinstance(
            result,
            dict,
        ):
            raise ValueError(
                "LLM JSON result is not an object"
            )

        return result

    except json.JSONDecodeError:
        pass

    # 查找最外层 JSON 对象
    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if (
        start == -1
        or end == -1
        or end <= start
    ):
        raise ValueError(
            "No JSON object found in LLM response"
        )

    candidate = cleaned[
        start:end + 1
    ]

    result = json.loads(
        candidate
    )

    if not isinstance(
        result,
        dict,
    ):
        raise ValueError(
            "Extracted JSON is not an object"
        )

    return result


def _normalize_confidence(
    value: Any,
) -> float:
    """
    confidence 统一限制到 0~1。
    """

    try:
        confidence = float(value)

    except (
        TypeError,
        ValueError,
    ):
        confidence = 0.5

    return max(
        0.0,
        min(
            confidence,
            1.0,
        ),
    )


def _validate_semantic_result(
    data: dict[str, Any],
) -> SemanticRoute:
    """
    校验并转换 LLM JSON。
    """

    intent = str(
        data.get(
            "intent",
            "",
        )
    ).strip()

    action_type = str(
        data.get(
            "action_type",
            "",
        )
    ).strip()

    topic = str(
        data.get(
            "topic",
            "",
        )
    ).strip()

    raw_related_topics = data.get(
        "related_topics",
        [],
    )

    if raw_related_topics is None:
        raw_related_topics = []

    if not isinstance(raw_related_topics, list):
        raise ValueError(
            "related_topics must be a list"
        )

    related_topics = []
    for value in raw_related_topics:
        related_topic = str(value).strip()

        if related_topic not in VALID_TOPICS:
            raise ValueError(
                "Invalid related topic: "
                f"{related_topic!r}"
            )

        if (
            related_topic != topic
            and related_topic not in related_topics
        ):
            related_topics.append(
                related_topic
            )

    reason = str(
        data.get(
            "reason",
            "",
        )
    ).strip()

    confidence = (
        _normalize_confidence(
            data.get(
                "confidence",
                0.5,
            )
        )
    )

    if intent not in VALID_INTENTS:
        raise ValueError(
            f"Invalid intent: {intent!r}"
        )

    if (
        action_type
        not in VALID_ACTION_TYPES
    ):
        raise ValueError(
            "Invalid action_type: "
            f"{action_type!r}"
        )

    if topic not in VALID_TOPICS:
        raise ValueError(
            f"Invalid topic: {topic!r}"
        )

    if not reason:
        reason = (
            "LLM semantic routing"
        )

    return SemanticRoute(
        intent=intent,
        action_type=action_type,
        topic=topic,
        related_topics=related_topics,
        confidence=confidence,
        reason=reason,
        source="llm",
    )


def _validate_with_auxiliary_policy(data: dict[str, Any]) -> SemanticRoute:
    """Recover malformed retrieval hints only for read-only semantic queries.

    Strict validation is still applied to the primary decision and the cleaned
    payload. No invalid hint is guessed, mapped to another topic or executed.
    """
    # An omitted auxiliary topic can change which business operation is planned.
    # Never salvage incomplete execute/handoff semantics into a write workflow.
    if (os.getenv('SEMANTIC_RELATED_TOPICS', '') != 'discard_invalid'
            or data.get('action_type') != 'query'):
        return _validate_semantic_result(data)
    values = data.get('related_topics', [])
    if values is None:
        values = []
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError('related_topics must be a list of strings')
    ignored = list(dict.fromkeys(v.strip() for v in values if v.strip() not in VALID_TOPICS))
    cleaned = {**data, 'related_topics': [v.strip() for v in values if v.strip() in VALID_TOPICS]}
    result = _validate_semantic_result(cleaned)
    return result.model_copy(update={'ignored_related_topics': ignored})


def infer_semantic_route(user_message: str) -> SemanticRoute:
    try:
        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": user_message,
            },
        ]

        raw = call_zhipu_chat(messages)
        data = None
        try:
            data = _extract_json_object(raw)
            return _validate_with_auxiliary_policy(data)
        except (ValueError, TypeError) as error:
            if os.getenv("SEMANTIC_ROUTE_REPAIR", "") != "once":
                raise
            # One contract repair only; no expected label or business decision
            # enters this request, and provider/network failures are not retried.
            correction = (
                "上一次输出未通过结构校验。请重新阅读原始用户请求，按系统中的语义定义输出一个合法 JSON。"
                "不要执行操作，不要把校验错误当成用户意图，不要添加任何新诉求。"
                "原输出中已使用合法枚举的 intent、action_type、topic 必须保持原值，只修复不合法部分。"
                f"校验错误：{str(error)[:500]}。"
                f"允许的 intent：{sorted(VALID_INTENTS)}；action_type：{sorted(VALID_ACTION_TYPES)}；"
                f"topic 及 related_topics 元素：{sorted(VALID_TOPICS)}。"
            )
            repaired = call_zhipu_chat([*messages, {"role": "assistant", "content": raw},
                                       {"role": "user", "content": correction}])
            result = _validate_semantic_result(_extract_json_object(repaired))
            for field, allowed in (("intent", VALID_INTENTS), ("action_type", VALID_ACTION_TYPES), ("topic", VALID_TOPICS)):
                original = str((data or {}).get(field, "")).strip()
                if original in allowed and getattr(result, field) != original:
                    raise ValueError(f"Contract repair changed valid primary field: {field}")
            return result

    except Exception as error:
        return SemanticRoute(
            intent="general_support",
            action_type="unknown",
            topic="general_question",
            confidence=0.0,
            reason=f"LLM semantic routing failed: {type(error).__name__}: {error}",
            source="fallback",
        )
