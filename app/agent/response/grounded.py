"""Render checked policy excerpts and observed facts, never model-written state."""
import json
import re

from app.agent.tools.tool_results import get_tool_result

_INTERNAL = re.compile(r"\bMQ\b|消息队列|幂等|重试策略|重复消费|数据库|SQL", re.IGNORECASE)
_BUSINESS_TOOLS = {"policy_search", "order_lookup", "create_ticket", "create_manual_review", "transfer_to_human"}


def needs_grounded_reply(tool_results):
    return any(item.tool_name in _BUSINESS_TOOLS for item in tool_results)


def policy_excerpts(tool_results):
    result = get_tool_result(tool_results, "policy_search")
    if not result or not result.success or not isinstance(result.result, list):
        return []
    excerpts = []
    for chunk_index, chunk in enumerate(result.result, 1):
        text = str(chunk.get("text") or "").strip()
        citation = chunk.get("citation") or chunk.get("source")
        if not citation:
            continue
        paragraphs = list(re.finditer(r"\S[\s\S]*?(?=\n[ \t]*\n|\Z)", text))
        cursor = 0
        scope = chunk.get("section") or ""
        while cursor < len(paragraphs):
            paragraph_index = cursor + 1
            match = paragraphs[cursor]
            paragraph = match[0].strip()
            cursor += 1
            if paragraph.startswith("#"):
                scope = re.sub(r"^#+\s*", "", paragraph)
                continue
            if paragraph.endswith(("：", ":")) and cursor < len(paragraphs):
                following = paragraphs[cursor]
                if not following[0].lstrip().startswith("#"):
                    paragraph = text[match.start():following.end()].strip()
                    cursor += 1
            if not paragraph or _INTERNAL.search(paragraph):
                continue
            # A chunk boundary can cut a condition or negation. Do not quote a
            # leading fragment or an unfinished final paragraph as a full rule.
            if paragraph_index == 1 and chunk.get("start_char", 0) > 0:
                continue
            if not re.search(r"[。！？；.!?;]$", paragraph):
                continue
            excerpts.append({"id": f"e{chunk_index}p{paragraph_index}", "text": paragraph,
                             "citation": citation, "chunk_id": chunk.get("chunk_id"),
                             "source": chunk.get("source"), "section": chunk.get("section"), "context_heading": scope})
    return excerpts


def selection_instruction(excerpts):
    return (
        "本轮只选择与用户问题相关的完整政策片段，最终文字由程序根据原文和订单事实生成。"
        "输出一个 JSON 对象，且只含 evidence_ids 字段（字符串数组），例如 {\"evidence_ids\":[\"e1p2\"]}。"
        "只选择已有片段且不要重复，保留规则的条件与例外；没有适用片段时返回空数组。"
        "不得生成客户回复、个人申请状态、改写原文或添加其他字段。可选片段：\n"
        + json.dumps(excerpts, ensure_ascii=False)
    )


def validate_selection(raw, excerpts, *, allow_known_subset=False):
    text = raw.strip()
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if not match:
            raise ValueError("invalid_json_envelope")
        text = match[1]
    try:
        selected = json.loads(text)
    except (ValueError, TypeError) as error:
        raise ValueError("invalid_json") from error
    if not isinstance(selected, dict) or set(selected) != {"evidence_ids"}:
        raise ValueError("unexpected_output_fields")
    ids = selected["evidence_ids"]
    if not isinstance(ids, list) or any(type(item) is not str for item in ids):
        raise ValueError("invalid_evidence_ids")
    allowed = {item["id"]: item for item in excerpts}
    if not allow_known_subset and (len(set(ids)) != len(ids) or any(item not in allowed for item in ids)):
        raise ValueError("unknown_or_duplicate_evidence_id")
    return [allowed[item] for item in dict.fromkeys(ids) if item in allowed]


def handoff_receipt(handoff):
    if not isinstance(handoff, dict) or not handoff.get("ticket_id"):
        return "本轮已整理转人工交接信息，等待人工接管。"
    status = {"pending_human_takeover": "已进入人工待办，等待客服认领",
              "in_progress": "已由人工认领，处理中", "resolved": "人工跟进已结案"}
    return f"转人工工单 {handoff['ticket_id']}：{status.get(handoff.get('status'), '请查看工单的当前处理状态')}。"


def render_grounded_reply(tool_results, selected, *, rejected=False):
    parts = []
    order_result = get_tool_result(tool_results, "order_lookup")
    if order_result and order_result.success and isinstance(order_result.result, dict):
        order = order_result.result
        parts.append(f"订单 {order.get('order_id')}，商品：{order.get('product_name')}，当前订单状态：{order.get('order_status')}。")
        if order.get("notes"):
            parts.append(f"订单备注记录：{order['notes']}")
    for name, field, label in (("create_ticket", "ticket_id", "工单"), ("create_manual_review", "review_id", "人工审核单")):
        receipt = get_tool_result(tool_results, name)
        if receipt and receipt.success and isinstance(receipt.result, dict):
            status = receipt.result.get("status")
            known = {"pending_human_review": "草稿，待人工审核", "pending_manual_review": "待人工审核",
                     "pending_review": "待人工审核", "approved": "审核已通过", "rejected": "审核未通过",
                     "cancelled": "已取消", "resolved": "已记录处理结果"}
            parts.append(f"{label} {receipt.result.get(field)}：{known.get(status, '请查看详情核实当前状态')}。")
    handoff = get_tool_result(tool_results, "transfer_to_human")
    if handoff and handoff.success:
        parts.append(handoff_receipt(handoff.result))
    if selected:
        parts.append("部分内容尚未核实，以下列出已核实的政策片段：" if rejected else "本轮可核实的政策内容：")
        grouped = {}
        for item in selected:
            key = (item["citation"], item.get("context_heading", ""))
            texts = grouped.setdefault(key, [])
            if item["text"] not in texts:
                texts.append(item["text"])
        for (citation, heading), texts in grouped.items():
            scope = f"（{heading}）" if heading and not citation.endswith(heading) else ""
            parts.append(f"《{citation}》{scope}：\n" + "\n\n".join(texts))
    elif rejected:
        parts.append("本轮生成内容未通过证据核对，暂时不能据此判断政策适用条件。请补充具体问题或由人工核实。")
    else:
        parts.append("本轮尚未取得足以回答此问题的政策片段，不能据此确认完整的政策条件。请补充具体问题或由人工核实。")
    return "\n\n".join(parts)


def checked_reply(raw, tool_results):
    excerpts = policy_excerpts(tool_results)
    audit = {"offered_count": len(excerpts), "proposal": raw[:10000], "proposal_truncated": len(raw) > 10000}
    try:
        selected = validate_selection(raw, excerpts)
    except ValueError as error:
        try:
            selected = validate_selection(raw, excerpts, allow_known_subset=True)
        except ValueError:
            selected = []
        return render_grounded_reply(tool_results, selected, rejected=True), {
            **audit, "passed": False, "partial": bool(selected), "reason": str(error), "selected": selected}
    return render_grounded_reply(tool_results, selected), {**audit, "passed": True, "selected": selected}
