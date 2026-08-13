import json
from pathlib import Path
from typing import Any

from app.core.config import BASE_DIR


WORKBENCH_DIR = BASE_DIR / "data" / "workbench"
PRODUCTS_PATH = WORKBENCH_DIR / "products.json"
QUICK_REPLIES_PATH = WORKBENCH_DIR / "quick_replies.json"
CHANNELS_PATH = WORKBENCH_DIR / "channels.json"


def load_json_list(path: Path) -> list[dict[str, Any]]:
    """读取一个工作台 JSON 列表文件。"""

    if not path.exists():
        return []

    return json.loads(path.read_text(encoding="utf-8"))


def list_products() -> list[dict[str, Any]]:
    """读取店铺商品目录。"""

    return load_json_list(PRODUCTS_PATH)


def list_quick_replies() -> list[dict[str, Any]]:
    """读取客服快捷回复模板。"""

    return load_json_list(QUICK_REPLIES_PATH)


def list_channel_conversations() -> list[dict[str, Any]]:
    """读取多平台客服会话样例。"""

    return load_json_list(CHANNELS_PATH)


def normalize_text(value: str) -> str:
    """轻量归一化文本，便于规则匹配。"""

    return value.lower().replace(" ", "")


def product_matches(product: dict, query: str, platform: str | None = None) -> bool:
    """判断商品是否匹配用户需求或平台过滤条件。"""

    if platform and platform not in product.get("platforms", []):
        return False

    if not query:
        return True

    normalized_query = normalize_text(query)
    searchable_text = normalize_text(
        "\n".join(
            [
                product.get("title", ""),
                product.get("category", ""),
                " ".join(product.get("tags", [])),
                " ".join(product.get("selling_points", [])),
            ]
        )
    )

    query_tokens = [
        "降噪",
        "耳机",
        "通勤",
        "显示器",
        "发票",
        "扫地",
        "机器人",
        "缺货",
        "补货",
        "安全座椅",
        "母婴",
        "地址",
    ]
    matched_tokens = [
        token for token in query_tokens
        if token in query and normalize_text(token) in searchable_text
    ]

    if matched_tokens:
        return True

    return normalized_query and normalized_query in searchable_text


def search_products(
    query: str = "",
    platform: str | None = None,
    in_stock_only: bool = False,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """按关键词、平台和库存筛选商品。"""

    results = []

    for product in list_products():
        if in_stock_only and int(product.get("stock") or 0) <= 0:
            continue

        if product_matches(product, query=query, platform=platform):
            results.append(product)

    results.sort(
        key=lambda item: (
            int(item.get("stock") or 0) > 0,
            int(item.get("monthly_sales") or 0),
        ),
        reverse=True,
    )

    return results[:limit]


def get_product(product_id: str) -> dict[str, Any] | None:
    """按商品 ID 查找商品。"""

    for product in list_products():
        if product.get("product_id") == product_id:
            return product

    return None


def find_quick_reply(intent: str, platform: str | None = None) -> dict[str, Any] | None:
    """按意图和平台查找快捷回复模板。"""

    for reply in list_quick_replies():
        if reply.get("intent") != intent:
            continue

        if platform and platform not in reply.get("platforms", []):
            continue

        return reply

    return None


def build_workbench_overview() -> dict[str, Any]:
    """构造客服工作台概览数据。"""

    conversations = list_channel_conversations()
    products = list_products()

    return {
        "channels": sorted({item.get("platform") for item in conversations if item.get("platform")}),
        "conversation_count": len(conversations),
        "waiting_ai_count": sum(1 for item in conversations if item.get("status") == "waiting_ai"),
        "need_human_count": sum(1 for item in conversations if item.get("status") == "need_human"),
        "product_count": len(products),
        "out_of_stock_count": sum(1 for item in products if int(item.get("stock") or 0) <= 0),
        "top_products": sorted(
            products,
            key=lambda item: int(item.get("monthly_sales") or 0),
            reverse=True,
        )[:3],
    }
