from collections.abc import AsyncGenerator

from app.agent.orchestrator import DEFAULT_ORCHESTRATOR


def get_conversation_history(conversation_id: str) -> list[dict]:
    return DEFAULT_ORCHESTRATOR.history(conversation_id)


def run_customer_support_agent(
    user_message: str,
    conversation_id: str | None = None,
    use_llm: bool = False,
) -> dict:
    return DEFAULT_ORCHESTRATOR.run(
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
    async for event in DEFAULT_ORCHESTRATOR.stream(
        user_message=user_message,
        conversation_id=conversation_id,
        use_llm=use_llm,
        stream_tokens=stream_tokens,
    ):
        yield event
