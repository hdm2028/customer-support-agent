from app.storage.database import save_feedback_to_db


def save_feedback(conversation_id: str, score: int, comment: str | None) -> None:
    """把用户反馈保存到当前业务数据库，便于后续做评估和案例复盘。"""

    save_feedback_to_db(
        conversation_id=conversation_id,
        score=score,
        comment=comment,
    )
