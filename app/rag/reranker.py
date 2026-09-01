from app.rag.embedding_client import CUSTOMER_KEYWORDS


RULE_RERANKER_VERSION = "legacy-business-rules-v1"


BUSINESS_RERANK_RULES = [
    {
        "name": "address_change",
        "triggers": ["改收货地址", "修改收货地址", "修改地址", "改地址", "修改为", "收货地址"],
        "sources": ["订单取消与修改政策"],
        "sections": ["修改收货地址"],
        "phrases": ["修改收货地址", "出库前", "地址修改工单", "仓库"],
    },
    {
        "name": "shipping_exception",
        "triggers": ["物流", "没更新", "没有更新", "不更新", "三天没动", "超过48", "48小时", "48 小时", "快递", "未收到", "没收到", "停住"],
        "sources": ["物流配送政策", "物流规则"],
        "sections": ["物流查询", "物流异常", "长时间未更新", "已签收争议"],
        "phrases": ["48 小时", "物流异常", "工单", "快递", "未收到", "已签收"],
    },
    {
        "name": "warranty_repair",
        "triggers": ["保修", "维修", "检测", "坏了", "故障", "质量问题", "质量", "黑屏", "换新"],
        "sources": ["保修政策", "售后FAQ", "商品售后规则", "商品说明文档"],
        "sections": ["保修范围", "保修处理方式", "电子产品", "家电商品"],
        "phrases": ["12 个月", "保修", "检测工单", "质量检测", "黑屏"],
    },
    {
        "name": "return_refund",
        "triggers": ["退货", "退款", "退钱", "退款申请", "七天无理由", "不想要", "不要了"],
        "sources": ["退换货政策", "退款政策", "商品售后规则", "历史问题案例"],
        "sections": ["七天无理由", "重要限制", "退款申请", "退款人工审核"],
        "phrases": ["七天无理由", "定制", "质检", "退款", "退款申请", "人工审核", "MQ"],
    },
    {
        "name": "payment_invoice",
        "triggers": ["支付", "扣款", "未支付", "发票", "税号", "抬头"],
        "sources": ["支付与发票政策"],
        "sections": ["支付失败", "电子发票"],
        "phrases": ["扣款", "30 分钟", "支付异常工单", "电子发票", "发票抬头", "税号"],
    },
    {
        "name": "membership_limit",
        "triggers": ["会员", "黑金", "跳过检测", "跳过售后", "直接换新"],
        "sources": ["会员权益政策"],
        "sections": ["售后权益限制", "会员等级"],
        "phrases": ["不能", "绕过", "质量检测", "会员权益"],
    },
    {
        "name": "complaint",
        "triggers": ["投诉", "没人处理", "商家", "客服", "起诉", "差评", "曝光", "12315"],
        "sources": ["售后FAQ", "客服SOP", "历史问题案例"],
        "sections": ["我要投诉客服或商家怎么办？", "高风险操作"],
        "phrases": ["投诉升级工单", "人工客服", "记录用户诉求", "人工审核"],
    },
]


def contains_any(text: str, phrases: list[str]) -> bool:
    """判断一段文本是否包含任意业务短语。"""

    return any(phrase and phrase in text for phrase in phrases)


def matched_items(text: str, phrases: list[str]) -> list[str]:
    """返回命中的业务短语，用于解释 rerank 为什么加分。"""

    return [
        phrase for phrase in phrases
        if phrase and phrase in text
    ]


def keyword_coverage_score(query: str, candidate_text: str) -> tuple[float, list[str]]:
    """计算用户问题中的业务关键词在候选 chunk 中的覆盖程度。"""

    matched_keywords = []

    for keyword in CUSTOMER_KEYWORDS:
        if keyword in query and keyword in candidate_text:
            matched_keywords.append(keyword)

    bonus = min(len(matched_keywords) * 0.025, 0.2)

    return bonus, matched_keywords


def state_match_score(query: str, candidate_text: str) -> tuple[float, list[str]]:
    """根据订单状态类词汇做额外加分，提升条款级排序稳定性。"""

    state_rules = [
        {
            "query_terms": ["待发货", "没发货", "未发货"],
            "chunk_terms": ["待发货", "出库前", "仓库", "拣货"],
            "reason": "order_state_match: 待发货",
        },
        {
            "query_terms": ["已发货", "运输中", "快递"],
            "chunk_terms": ["已发货", "物流", "快递", "拦截"],
            "reason": "order_state_match: 已发货",
        },
        {
            "query_terms": ["已签收", "签收后", "退货仓库"],
            "chunk_terms": ["已签收", "签收后", "质检", "退款流程"],
            "reason": "order_state_match: 已签收",
        },
        {
            "query_terms": ["支付异常", "扣款", "未支付"],
            "chunk_terms": ["支付异常", "扣款", "支付系统", "30 分钟"],
            "reason": "order_state_match: 支付异常",
        },
    ]

    reasons = []
    bonus = 0.0

    for rule in state_rules:
        if not contains_any(query, rule["query_terms"]):
            continue

        if contains_any(candidate_text, rule["chunk_terms"]):
            bonus += 0.08
            reasons.append(rule["reason"])

    return bonus, reasons


def business_rule_score(query: str, candidate: dict) -> tuple[float, list[str]]:
    """根据售后业务意图、文档来源、章节标题和正文短语对候选结果重新加分。"""

    source = candidate.get("source", "")
    section = candidate.get("section", "")
    text = candidate.get("text", "")
    combined_candidate = f"{source}\n{section}\n{text}"
    bonus = 0.0
    reasons = []

    for rule in BUSINESS_RERANK_RULES:
        if not contains_any(query, rule["triggers"]):
            continue

        if contains_any(source, rule["sources"]):
            bonus += 0.12
            reasons.append(f"source_match: {rule['name']}")

        if contains_any(section, rule["sections"]):
            bonus += 0.18
            reasons.append(f"section_match: {section}")

        matched_phrases = matched_items(combined_candidate, rule["phrases"])
        if matched_phrases:
            phrase_bonus = min(len(matched_phrases) * 0.04, 0.16)
            bonus += phrase_bonus
            reasons.append(f"phrase_match: {', '.join(matched_phrases[:3])}")

    coverage_bonus, matched_keywords = keyword_coverage_score(query, combined_candidate)
    if coverage_bonus:
        bonus += coverage_bonus
        reasons.append(f"keyword_coverage: {', '.join(matched_keywords[:5])}")

    state_bonus, state_reasons = state_match_score(query, combined_candidate)
    if state_bonus:
        bonus += state_bonus
        reasons.extend(state_reasons)

    return bonus, reasons


def rerank_documents(query: str, candidates: list[dict]) -> list[dict]:
    """对初召回候选 chunk 做业务 rerank，并保留可解释的重排序原因。"""

    reranked = []

    for candidate in candidates:
        retrieval_score = float(candidate.get("score", 0))
        rerank_bonus, reasons = business_rule_score(query, candidate)
        rerank_score = retrieval_score + rerank_bonus
        item = {
            **candidate,
            "retrieval_score": round(retrieval_score, 4),
            "rerank_bonus": round(rerank_bonus, 4),
            "rerank_score": round(rerank_score, 4),
            "rerank_reasons": reasons[:6],
            "score": round(rerank_score, 4),
        }
        reranked.append(item)

    reranked.sort(
        key=lambda item: (
            item["rerank_score"],
            item.get("vector_score", 0),
            item.get("keyword_score", 0),
        ),
        reverse=True,
    )

    return reranked
