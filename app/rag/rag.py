from app.rag.retriever import HybridRetriever


_RETRIEVER = HybridRetriever()


def knowledge_signature() -> tuple:
    return _RETRIEVER.knowledge_signature()


def build_knowledge_chunks():
    return _RETRIEVER.build_chunks()


def get_knowledge_index():
    return _RETRIEVER.get_index()


def search_documents(query: str, top_k: int = 3) -> list[dict]:
    return _RETRIEVER.retrieve(query=query, top_k=top_k)


def list_chunks() -> list[dict]:
    return _RETRIEVER.list_chunks()


def get_knowledge_catalog() -> dict:
    return _RETRIEVER.catalog()
