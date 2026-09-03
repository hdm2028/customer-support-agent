import re


def extract_order_id(user_message: str) -> str | None:
    match = re.search(r"(?<!\d)\d{4,}(?!\d)", user_message)

    if match:
        return match.group(0)

    return None