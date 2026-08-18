from app.core.schemas import ToolResult


POLICY_PROFILES = [
    {
        "name": "address_change",
        "triggers": ["改收货地址", "修改地址", "改地址", "修改为", "收货地址"],
        "expected_sources": ["订单取消与修改政策.md"],
        "required_keywords": ["修改收货地址", "地址修改工单", "出库前"],
    },
    {
        "name": "shipping_exception",
        "triggers": ["物流", "快递", "发货", "没更新", "没有更新", "不更新", "三天没动", "超过48", "停住", "延迟", "丢件", "未收到", "没收到"],
        "expected_sources": ["物流配送政策.md", "物流规则.md", "售后FAQ.md"],
        "required_keywords": ["物流", "48 小时", "工单"],
    },
    {
        "name": "warranty_repair",
        "triggers": ["保修", "维修", "检测", "坏了", "故障", "质量问题", "质量", "黑屏", "换新"],
        "expected_sources": ["保修政策.md", "商品售后规则.md", "商品说明文档.md", "售后FAQ.md"],
        "required_keywords": ["保修", "检测工单", "12 个月"],
    },
    {
        "name": "return_refund",
        "triggers": ["退货", "退款", "退钱", "七天无理由", "不想要", "不要了"],
        "expected_sources": ["退换货政策.md", "退款政策.md", "商品售后规则.md", "历史问题案例.md", "售后FAQ.md"],
        "required_keywords": ["退款", "退货", "七天无理由", "质检"],
    },
    {
        "name": "payment_invoice",
        "triggers": ["支付", "扣款", "未支付", "银行卡", "发票", "税号", "抬头"],
        "expected_sources": ["支付与发票政策.md"],
        "required_keywords": ["扣款", "支付异常工单", "电子发票", "发票抬头", "税号"],
    },
    {
        "name": "membership",
        "triggers": ["会员", "黑金", "权益", "跳过检测", "直接换新"],
        "expected_sources": ["会员权益政策.md"],
        "required_keywords": ["会员", "不能", "质量检测", "绕过"],
    },
    {
        "name": "complaint",
        "triggers": ["投诉", "商家没人处理", "没人处理", "起诉", "差评", "曝光", "12315"],
        "expected_sources": ["售后FAQ.md"],
        "required_keywords": ["投诉", "升级工单", "人工客服"],
    },
]

MIN_RAG_SCORE = 0.45


def normalize_text(text: str) -> str:
    """轻量归一化文本，避免空格影响证据关键词判断。"""

    return text.lower().replace(" ", "")


def contains_any(text: str, keywords: list[str]) -> bool:
    """判断文本中是否包含关键词列表中的任意一个。"""

    normalized = normalize_text(text)

    return any(normalize_text(keyword) in normalized for keyword in keywords)


def detect_policy_profile(query: str) -> dict | None:
    """根据用户问题识别本轮期望的政策类型。"""

    for profile in POLICY_PROFILES:
        if profile["name"] == "return_refund" and contains_any(query, profile["triggers"]):
            return profile

    for profile in POLICY_PROFILES:
        if contains_any(query, profile["triggers"]):
            return profile

    return None


def simplify_evidence(results: list[dict], limit: int = 3) -> list[dict]:
    """保留最关键的证据字段，写入失败原因和 trace 时更容易读。"""

    simplified = []

    for item in results[:limit]:
        simplified.append(
            {
                "source": item.get("source"),
                "section": item.get("section"),
                "citation": item.get("citation"),
                "score": item.get("score"),
                "rerank_score": item.get("rerank_score"),
                "rerank_reasons": item.get("rerank_reasons", []),
            }
        )

    return simplified


def validate_policy_evidence(query: str, policy_result: ToolResult) -> tuple[bool, dict]:
    """校验 RAG 证据是否足够支撑本轮售后回答。

    这里不只看有没有召回结果，还会看 top1 分数、来源文档和关键政策词。
    """

    if not policy_result.success:
        return False, {
            "reason": "policy_search_failed",
            "detail": policy_result.result,
        }

    results = policy_result.result or []
    if not results:
        return False, {
            "reason": "empty_policy_results",
            "detail": "RAG 没有返回任何政策证据。",
        }

    profile = detect_policy_profile(query)
    top_score = float(results[0].get("score", 0) or 0)
    combined_text = "\n".join(
        f"{item.get('source', '')}\n{item.get('section', '')}\n{item.get('text', '')}"
        for item in results
    )

    if top_score < MIN_RAG_SCORE:
        return False, {
            "reason": "low_rag_confidence",
            "top_score": round(top_score, 4),
            "min_score": MIN_RAG_SCORE,
            "evidence": simplify_evidence(results),
        }

    if not profile:
        return True, {
            "reason": "no_strict_profile",
            "top_score": round(top_score, 4),
            "evidence": simplify_evidence(results),
        }

    actual_sources = [
        item.get("source")
        for item in results[:3]
    ]
    source_matched = any(
        source in profile["expected_sources"]
        for source in actual_sources
    )

    if not source_matched:
        return False, {
            "reason": "evidence_source_mismatch",
            "profile": profile["name"],
            "expected_sources": profile["expected_sources"],
            "actual_sources": actual_sources,
            "evidence": simplify_evidence(results),
        }

    if not contains_any(combined_text, profile["required_keywords"]):
        return False, {
            "reason": "evidence_keyword_miss",
            "profile": profile["name"],
            "required_keywords": profile["required_keywords"],
            "evidence": simplify_evidence(results),
        }

    return True, {
        "reason": "evidence_valid",
        "profile": profile["name"],
        "top_score": round(top_score, 4),
        "evidence": simplify_evidence(results),
    }


def apply_policy_evidence_guardrail(query: str, policy_result: ToolResult) -> ToolResult:
    """把低置信或不匹配的 RAG 结果转换成失败 ToolResult，阻止下游自动行动。"""

    if not policy_result.success:
        return policy_result

    passed, report = validate_policy_evidence(query, policy_result)

    if passed:
        if isinstance(policy_result.result, list):
            policy_result.result = [
                {
                    **item,
                    "evidence_guardrail": report,
                }
                for item in policy_result.result
            ]
        return policy_result

    return ToolResult(
        tool_name=policy_result.tool_name,
        success=False,
        result={
            "error_type": "LowConfidenceEvidence",
            "error_message": "RAG 证据不足或与用户意图不匹配。",
            "fallback_action": "handoff_to_human",
            "guardrail_report": report,
        },
    )
