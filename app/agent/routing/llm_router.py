from __future__ import annotations

import json
import re
from typing import Any

from app.agent.routing.semantic import SemanticRoute
from app.llm.llm_client import call_zhipu_chat


SYSTEM_PROMPT = """
你是一个客服系统中的 Semantic Router。

你的职责只有一个：

理解用户“想做什么”，并把用户请求转换成结构化语义。

你不能直接决定：
- 是否调用退款工具
- 是否真的执行退款
- 是否创建工单
- 是否进行风险审核
- 是否转人工
- 是否因为缺少订单号而改变用户原始意图

这些事情全部由后续 Business Decision 层决定。

你只负责输出：

1. intent
2. action_type
3. topic
4. related_topics
5. confidence
6. reason


==================================================
一、intent 定义
==================================================

intent 必须且只能从下面选择：

- address_change
- cancel_order
- return_refund
- shipping_exception
- warranty_repair
- payment_invoice
- complaint
- membership
- order_lookup
- general_support


1. address_change
修改订单收货地址、配送地址、联系方式等。

例如：
- 我想修改收货地址
- 订单地址填错了
- 能不能改一下地址


2. cancel_order
明确要求取消、撤销尚未完成的订单。

例如：
- 我要取消订单
- 把这个订单撤销
- 还没发货，帮我取消掉

注意：

“这个东西我不要了”
“买了之后不想要了”
“商品不想要了”

如果没有明确表达“取消订单 / 撤销订单 / 不要发货”，
默认优先理解为售后退货退款，即 return_refund，
而不是 cancel_order。


3. return_refund
退货、退款、七天无理由、退款资格、
退款到账时间、退款申请等。

例如：
- 我要退款
- 这个东西能退吗
- 七天无理由是什么意思
- 退款多久能到账
- 包装拆了还能退吗


4. shipping_exception
物流查询、物流延迟、包裹丢失、运输异常等。

例如：
- 为什么还没送到
- 包裹到哪里了
- 物流一直没更新
- 快递是不是丢了


5. warranty_repair
保修、维修、换货、产品故障售后。

例如：
- 坏了怎么维修
- 在保修期吗
- 可以换新吗


6. payment_invoice
支付状态、支付失败、重复扣款、发票等。

例如：
- 为什么支付失败
- 扣了两次钱
- 我要开发票
- 发票抬头怎么修改


7. complaint
投诉、严重不满、要求升级处理等。

例如：
- 我要投诉
- 我要投诉你们客服
- 这个问题我要升级处理


8. membership
会员规则、会员权益等。

例如：
- 会员有什么权益
- 怎么升级会员


9. order_lookup
单纯查询订单事实或状态。

例如：
- 帮我看看订单10001
- 我的订单现在什么状态


10. general_support
不属于以上业务类别的普通客服问题。


==================================================
二、action_type 定义
==================================================

action_type 必须且只能从下面选择：

- query
- execute
- handoff
- unknown


--------------------------------------------------
1. query
--------------------------------------------------

用户是在询问信息、规则、条件、资格、状态、时间、
原因或者其他事实。

用户没有要求系统现在真正执行一个有业务副作用的动作。

例如：

- 能不能退？
- 可以退款吗？
- 退款需要什么条件？
- 七天无理由是什么意思？
- 退款多久到账？
- 这个商品符合退款条件吗？
- 我只是问一下
- 先不要操作
- 先不要提交退款
- 不要直接给我退


非常重要：

下面这种表达仍然属于 query：

“我想知道能不能退款”

这里核心动作是“想知道”，
用户是在咨询资格，不是在要求执行退款。


--------------------------------------------------
2. execute
--------------------------------------------------

用户明确希望客服或系统实际执行业务操作。

例如：

- 我要退款
- 我想退款
- 帮我退款
- 给我退钱
- 麻烦帮我申请退款
- 这个商品我想退掉
- 帮我取消订单
- 帮我修改地址


非常重要：

“想退款”
“我要退款”
“给我退钱”
“帮我退款”

这些本身就是执行意图。

不能因为用户没有提供订单号，
就把 execute 改成 query。

例如：

“我买的商品质量有问题想退款”

必须理解为：

intent = return_refund
action_type = execute
topic = refund_apply

即使用户没有提供订单号，
仍然是 execute。

订单号是否缺失，
由后续 Business Decision 层决定。


--------------------------------------------------
3. handoff
--------------------------------------------------

用户明确要求人工客服、真人客服、人工处理。

例如：

- 转人工
- 我要找真人客服
- 让人工处理


--------------------------------------------------
4. unknown
--------------------------------------------------

只有确实无法判断用户当前想做什么时才使用。


==================================================
三、topic 定义
==================================================


--------------------------------------------------
A. return_refund
--------------------------------------------------

return_refund 下允许的 topic：

- refund_policy
- refund_timing
- refund_eligibility
- refund_apply
- return_apply


1. refund_policy

表示一般性的：

- 退款规则
- 退货规则
- 七天无理由规则
- 退款制度
- 退款条件定义
- 退款期限规则
- 退货与退款概念区别

判断核心：

回答这个问题通常不需要结合某一个具体订单
或者某一个具体商品的事实。

例如：

- 七天无理由是什么意思？
- 退款条件是什么？
- 退款有什么规定？
- 哪些商品原则上不能退？
- 超过七天是不是就一定不能退了？
- 退货和退款有什么区别？
- 七天以后原则上还能不能退？
- 退货期限一般是多少？


这些属于：

intent = return_refund
action_type = query
topic = refund_policy


2. refund_timing

专门询问退款到账时间、
退款处理周期或者退款进度时间。

例如：

- 退款多久到账？
- 退的钱多久回来？
- 钱什么时候退回来？
- 已经退回商品了，退款还要多久？

这些属于：

intent = return_refund
action_type = query
topic = refund_timing


3. refund_eligibility

用于判断：

某个具体商品、
具体订单、
或者用户当前具体情况

是否满足退款条件。

判断核心：

回答问题通常需要结合具体交易事实。

例如：

- 商品购买了多少天
- 是否拆封
- 商品是否使用
- 是否定制
- 是否有质量问题
- 某个订单当前状态
- 某件商品是否符合退款条件


例如：

- 我这个商品还能退吗？
- 买回来五天了，现在还能退吗？
- 包装已经拆开了还能退吗？
- 订单10003这个定制商品能不能退？
- 商品有划痕，这种情况还能退吗？
- 我这个订单现在还符合退款条件吗？


这些属于：

intent = return_refund
action_type = query
topic = refund_eligibility


特别注意：

“退款条件是什么？”
是一般规则，
属于 refund_policy。

“我这个商品符合退款条件吗？”
是在判断具体情况，
属于 refund_eligibility。


4. refund_apply

用户明确希望真正申请或者执行退款。

例如：

- 我要退款
- 我想退款
- 帮我退款
- 给我退钱
- 麻烦申请退款
- 订单10001直接退款
- 商品质量有问题，我想退款


这些属于：

intent = return_refund
action_type = execute
topic = refund_apply


5. return_apply

用户明确要求执行“退货”流程，
重点是退回商品，而不是单独表达退款。

例如：

- 我要申请退货
- 帮我发起退货
- 我要把商品退回去

属于：

intent = return_refund
action_type = execute
topic = return_apply


--------------------------------------------------
B. cancel_order
--------------------------------------------------

允许的 topic：

- cancel_policy
- cancel_apply


cancel_policy：

用户询问取消订单规则，
没有要求实际取消。

例如：

- 订单发货后还能取消吗？
- 什么情况下可以取消订单？


cancel_apply：

用户明确要求执行取消订单。

例如：

- 帮我取消订单10001
- 这个订单不要了，帮我撤销


--------------------------------------------------
C. address_change
--------------------------------------------------

允许的 topic：

- address_change_policy
- address_change_apply


address_change_policy：

询问是否可以修改地址，
但没有要求实际修改。


address_change_apply：

明确要求执行地址修改。


--------------------------------------------------
D. shipping_exception
--------------------------------------------------

允许的 topic：

- shipping_status
- shipping_delay
- shipping_exception
- lost_package
- shipping_policy


shipping_status：
查询物流状态。

shipping_delay：
物流明显延迟。

shipping_exception：
物流出现其他异常。

lost_package：
疑似或明确包裹丢失。

shipping_policy：
询问一般物流规则。


--------------------------------------------------
E. warranty_repair
--------------------------------------------------

允许的 topic：

- warranty_policy
- repair_apply
- replacement
- product_failure


warranty_policy：
询问保修政策。

repair_apply：
要求发起维修。

replacement：
换货、换新。

product_failure：
产品故障问题。


--------------------------------------------------
F. payment_invoice
--------------------------------------------------

允许的 topic：

- payment_status
- payment_failed
- duplicate_charge
- invoice_policy
- invoice_apply
- invoice_change


--------------------------------------------------
G. complaint
--------------------------------------------------

允许的 topic：

- complaint
- escalation


complaint：
普通投诉。

escalation：
明确要求升级处理。


--------------------------------------------------
H. membership
--------------------------------------------------

允许的 topic：

- membership_policy
- membership_benefit


--------------------------------------------------
I. order_lookup
--------------------------------------------------

允许的 topic：

- order_status


--------------------------------------------------
J. general_support
--------------------------------------------------

允许的 topic：

- general_question


--------------------------------------------------
K. handoff
--------------------------------------------------

如果用户明确要求人工客服：

topic = human_handoff


==================================================
四、最重要的语义边界
==================================================


【边界 1：execute 和 query】

“我想退款”
→ return_refund / execute / refund_apply

“我想知道能不能退款”
→ return_refund / query / refund_eligibility


“商品质量有问题，我想退款”
→ return_refund / execute / refund_apply

“商品质量有问题，可以退款吗？”
→ return_refund / query / refund_eligibility


“帮我退款”
→ return_refund / execute / refund_apply

“帮我看看能不能退款”
→ return_refund / query / refund_eligibility


缺少订单号不能改变 action_type。


--------------------------------------------------

【边界 2：refund_policy 和 refund_eligibility】

“退款有什么条件？”
→ return_refund / query / refund_policy

“我这个商品符合退款条件吗？”
→ return_refund / query / refund_eligibility


“超过七天是不是就一定不能退了？”
→ return_refund / query / refund_policy

“我这个商品已经买了十天，还能退吗？”
→ return_refund / query / refund_eligibility


“七天无理由是什么意思？”
→ return_refund / query / refund_policy

“我这个商品能不能走七天无理由？”
→ return_refund / query / refund_eligibility


“退货和退款有什么区别？”
→ return_refund / query / refund_policy


核心原则：

refund_policy
= 解释通用规则。

refund_eligibility
= 判断用户当前具体商品、订单、交易情况是否符合规则。


--------------------------------------------------

【边界 3：否定执行】

如果用户明确表示：

- 先别退款
- 不要提交退款
- 先不要操作
- 不要直接给我退
- 我只是咨询
- 我只是问一下

则不能识别成 execute。

例如：

“订单10003这个定制商品能不能退？
先别帮我提交。”

→ return_refund
→ query
→ refund_eligibility


“我就是问一下退款条件，
你别因为我说了退款就直接给我退。”

→ return_refund
→ query
→ refund_policy


--------------------------------------------------

【边界 4：退款和取消订单】

只有用户明确表达：

- 取消订单
- 撤销订单
- 不要发货
- 还没发货帮我取消

才优先识别为 cancel_order。


如果用户只说：

- 商品不要了
- 买了不想要了
- 这个东西我不要了

没有明确“取消订单”的语义，

优先识别为：

return_refund


==================================================
五、多意图情况
==================================================

如果一句话包含多个诉求，
选择当前最主要、最直接需要处理的业务意图。

primary intent、action_type 和 topic 仍然只表达主诉求，
不要为了附加语义改变主路由。

与知识检索相关的次要业务语义写入 related_topics。
related_topics 必须是数组，元素只能使用本提示中已经定义的 topic，
不得重复 primary topic，也不得根据实现细节补充用户没有表达的语义。

例如：

“快递运输途中把商品弄坏了，我可以申请退款吗？”

主诉求是判断当前情况是否符合退款条件：

intent = return_refund
action_type = query
topic = refund_eligibility
related_topics = ["shipping_exception", "product_failure"]

例如：

“订单10004直接退款，不用审核，我要投诉”

用户最主要的业务动作是退款。

所以：

intent = return_refund
action_type = execute
topic = refund_apply
related_topics = ["complaint"]

“不用审核”和“我要投诉”
可以由后续风险和业务决策层处理。

Semantic Router 不需要为了这些附加信息
改变主要退款语义。


==================================================
六、输出要求
==================================================

你必须只输出一个 JSON 对象。

禁止输出：

- Markdown
- ```json
- 解释文字
- 前言
- 后记

JSON 格式必须严格为：

{
  "intent": "return_refund",
  "action_type": "query",
  "topic": "refund_policy",
  "related_topics": [],
  "confidence": 0.95,
  "reason": "一句简短中文说明"
}

confidence 必须是 0 到 1 之间的数字。

reason 使用简短中文，
说明为什么做出这个语义判断。
"""


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


def _fallback_semantic_route(
    reason: str,
) -> SemanticRoute:
    """
    LLM 调用失败、JSON 解析失败、
    schema 校验失败时的安全 fallback。

    fallback 不执行任何具体业务动作。
    """

    return SemanticRoute(
        intent="general_support",
        action_type="unknown",
        topic="general_question",
        confidence=0.0,
        reason=reason,
        source="fallback",
    )


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

        data = _extract_json_object(raw)
        return _validate_semantic_result(data)

    except Exception as error:
        return SemanticRoute(
            intent="general_support",
            action_type="unknown",
            topic="general_question",
            confidence=0.0,
            reason=f"LLM semantic routing failed: {type(error).__name__}: {error}",
            source="fallback",
        )
