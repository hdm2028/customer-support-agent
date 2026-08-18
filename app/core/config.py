import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = BASE_DIR / ".env"


def load_env_file(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        os.environ.setdefault(key, value)


@dataclass
class Settings:
    app_name: str
    zhipu_api_key: str
    zhipu_base_url: str
    zhipu_model: str
    zhipu_embedding_url: str
    zhipu_embedding_model: str
    embedding_dimensions: int
    rag_embedding_provider: str
    llm_timeout_seconds: int
    database_backend: str
    redis_url: str
    mysql_dsn: str
    cache_ttl_seconds: int
    agent_state_ttl_seconds: int
    mq_backend: str
    rag_semantic_weight: float
    rag_bm25_weight: float
    rag_keyword_weight: float
    rag_candidate_multiplier: int
    embedding_cache_ttl_seconds: int
    refund_lock_ttl_seconds: int
    refund_lock_wait_seconds: float
    refund_idempotency_ttl_seconds: int

    @property
    def has_llm_key(self) -> bool:
        return bool(self.zhipu_api_key)


def get_settings() -> Settings:
    load_env_file()

    api_key = (
        os.getenv("ZHIPUAI_API_KEY")
        or os.getenv("ZHIPU_API_KEY")
        or os.getenv("BIGMODEL_API_KEY")
        or os.getenv("LLM_API_KEY")
        or ""
    )

    return Settings(
        app_name=os.getenv("APP_NAME", "中文电商智能售后客服 Agent"),
        zhipu_api_key=api_key,
        zhipu_base_url=os.getenv(
            "ZHIPU_BASE_URL",
            os.getenv(
                "LLM_ENDPOINT",
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            ),
        ),
        zhipu_model=os.getenv("ZHIPU_MODEL", "glm-4-flash"),
        zhipu_embedding_url=os.getenv(
            "ZHIPU_EMBEDDING_URL",
            "https://open.bigmodel.cn/api/paas/v4/embeddings",
        ),
        zhipu_embedding_model=os.getenv("ZHIPU_EMBEDDING_MODEL", "embedding-3"),
        embedding_dimensions=int(os.getenv("EMBEDDING_DIMENSIONS", "1024")),
        rag_embedding_provider=os.getenv("RAG_EMBEDDING_PROVIDER", "local").lower(),
        llm_timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "60")),
        database_backend=os.getenv("DATABASE_BACKEND", "auto").lower(),
        redis_url=os.getenv("REDIS_URL", ""),
        mysql_dsn=os.getenv("MYSQL_DSN", ""),
        cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "300")),
        agent_state_ttl_seconds=int(os.getenv("AGENT_STATE_TTL_SECONDS", "1800")),
        mq_backend=os.getenv("MQ_BACKEND", "sqlite").lower(),
        rag_semantic_weight=float(os.getenv("RAG_SEMANTIC_WEIGHT", "0.62")),
        rag_bm25_weight=float(os.getenv("RAG_BM25_WEIGHT", "0.28")),
        rag_keyword_weight=float(os.getenv("RAG_KEYWORD_WEIGHT", "0.10")),
        rag_candidate_multiplier=int(os.getenv("RAG_CANDIDATE_MULTIPLIER", "4")),
        embedding_cache_ttl_seconds=int(os.getenv("EMBEDDING_CACHE_TTL_SECONDS", "86400")),
        refund_lock_ttl_seconds=int(os.getenv("REFUND_LOCK_TTL_SECONDS", "15")),
        refund_lock_wait_seconds=float(os.getenv("REFUND_LOCK_WAIT_SECONDS", "3")),
        refund_idempotency_ttl_seconds=int(os.getenv("REFUND_IDEMPOTENCY_TTL_SECONDS", "86400")),
    )
