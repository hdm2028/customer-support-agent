"""Route withdrawal language to explicit selection of an existing refund."""
import re


WITHDRAWAL_TOPIC = "refund_withdrawal"
WITHDRAWAL_GUIDANCE = (
    "如需撤销已受理的退款，请在“退款申请查询与撤销”中选择对应申请，"
    "核对订单和状态后填写原因并提交撤销。当前聊天尚未撤销申请，也未创建新的退款申请。"
    "已提交支付或渠道结果待核实的申请，需要先由人工核实。"
)
_WITHDRAWAL = re.compile(
    r"(?:撤销|撤回|取消)\s*(?:(?:订单\s*\d+\s*的?|这(?:一)?笔|该|刚才的|之前的|我的)\s*)?"
    r"(?:退款|退货退款)(?:申请)?|(?:退款|退货退款)申请\s*(?:撤销|撤回|取消)"
)


def mentions_refund_withdrawal(message: str) -> bool:
    # This only presents a selection flow; matching text never authorizes a write.
    return bool(_WITHDRAWAL.search(message))
