from collections.abc import AsyncGenerator

from app.agent.stream_runner import stream_workflow
from app.agent.workflow import get_conversation_history, run_workflow


def run_customer_support_agent(
    user_message: str,
    conversation_id: str | None = None,
    use_llm: bool = False,
) -> dict:
    """非流式 Agent 入口。"""

    return run_workflow(
        user_message=user_message,
        conversation_id=conversation_id,
        use_llm=use_llm,
    )


async def stream_customer_support_agent(
    user_message: str,
    conversation_id: str | None = None,
    use_llm: bool = False,
    stream_tokens: bool = True,
) -> AsyncGenerator[dict, None]:
    """流式 Agent 入口。"""

    async for event in stream_workflow(
        user_message=user_message,
        conversation_id=conversation_id,
        use_llm=use_llm,
        stream_tokens=stream_tokens,
    ):
        yield event
