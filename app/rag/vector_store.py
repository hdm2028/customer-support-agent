def cosine_similarity(left: list[float], right: list[float]) -> float:
    """计算已归一化向量的点积相似度，供 Hybrid RAG 语义召回使用。"""

    return sum(left_value * right_value for left_value, right_value in zip(left, right))
