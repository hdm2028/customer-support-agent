import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


BASE_DIR = Path(__file__).resolve().parents[2]
ENV_PATH = BASE_DIR / ".env"
KNOWLEDGE_DIR = BASE_DIR / "data" / "knowledge"
KNOWLEDGE_MANIFEST_PATH = BASE_DIR / "data" / "cache" / "knowledge_manifest.json"
CHUNK_STRATEGY = os.getenv("CHUNK_STRATEGY", "fixed_256")


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


def build_mysql_dsn_from_env() -> str:
    database = os.getenv("MYSQL_DATABASE", "")
    user = os.getenv("MYSQL_USER", "")

    if not database or not user:
        return ""

    password = os.getenv("MYSQL_PASSWORD", "")
    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = os.getenv("MYSQL_PORT", "3306")
    charset = os.getenv("MYSQL_CHARSET", "utf8mb4")
    timeout = os.getenv("MYSQL_CONNECT_TIMEOUT", "5")
    auth = quote(user, safe="")

    if password:
        auth = f"{auth}:{quote(password, safe='')}"

    return (
        f"mysql+pymysql://{auth}@{host}:{port}/{database}"
        f"?charset={charset}&connect_timeout={timeout}"
    )


def build_redis_url_from_env() -> str:
    host = os.getenv("REDIS_HOST", "")

    if not host:
        return ""

    password = os.getenv("REDIS_PASSWORD", "")
    port = os.getenv("REDIS_PORT", "6379")
    database = os.getenv("REDIS_DB", "0")
    auth = f":{quote(password, safe='')}@" if password else ""

    return f"redis://{auth}{host}:{port}/{database}"


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_tool_timeout_overrides() -> dict[str, float]:
    prefix = "TOOL_"
    suffix = "_TIMEOUT_SECONDS"
    overrides = {}

    for key, value in os.environ.items():
        if not key.startswith(prefix) or not key.endswith(suffix):
            continue

        tool_name = key[len(prefix):-len(suffix)].lower()
        timeout_seconds = float(value)

        if tool_name and timeout_seconds > 0:
            overrides[tool_name] = timeout_seconds

    return overrides


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
    seed_demo_data: bool
    cache_ttl_seconds: int
    agent_state_ttl_seconds: int
    mq_backend: str
    rag_semantic_weight: float
    rag_bm25_weight: float
    rag_keyword_weight: float
    rag_candidate_multiplier: int
    rag_ranking_mode: str
    rag_semantic_reranker_provider: str
    rag_semantic_reranker_model: str
    rag_semantic_reranker_revision: str
    rag_semantic_reranker_device: str
    rag_semantic_reranker_batch_size: int
    rag_semantic_reranker_max_length: int
    embedding_cache_ttl_seconds: int
    refund_lock_ttl_seconds: int
    refund_lock_wait_seconds: float
    refund_idempotency_ttl_seconds: int
    tool_timeout_overrides: dict[str, float]
    tool_retry_backoff_seconds: float
    tool_timeout_worker_limit: int

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
        redis_url=os.getenv("REDIS_URL", "") or build_redis_url_from_env(),
        mysql_dsn=os.getenv("MYSQL_DSN", "") or build_mysql_dsn_from_env(),
        seed_demo_data=env_flag("SEED_DEMO_DATA", False),
        cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "300")),
        agent_state_ttl_seconds=int(os.getenv("AGENT_STATE_TTL_SECONDS", "1800")),
        mq_backend=os.getenv("MQ_BACKEND", "sqlite").lower(),
        rag_semantic_weight=float(os.getenv("RAG_SEMANTIC_WEIGHT", "0.62")),
        rag_bm25_weight=float(os.getenv("RAG_BM25_WEIGHT", "0.28")),
        rag_keyword_weight=float(os.getenv("RAG_KEYWORD_WEIGHT", "0.10")),
        rag_candidate_multiplier=int(os.getenv("RAG_CANDIDATE_MULTIPLIER", "4")),
        rag_ranking_mode=os.getenv("RAG_RANKING_MODE", "hybrid_rule").lower(),
        rag_semantic_reranker_provider=os.getenv(
            "RAG_SEMANTIC_RERANKER_PROVIDER",
            "cross_encoder",
        ).lower(),
        rag_semantic_reranker_model=os.getenv(
            "RAG_SEMANTIC_RERANKER_MODEL",
            "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
        ),
        rag_semantic_reranker_revision=os.getenv(
            "RAG_SEMANTIC_RERANKER_REVISION",
            "1427fd652930e4ba29e8149678df786c240d8825",
        ),
        rag_semantic_reranker_device=os.getenv(
            "RAG_SEMANTIC_RERANKER_DEVICE",
            "cpu",
        ).lower(),
        rag_semantic_reranker_batch_size=int(
            os.getenv("RAG_SEMANTIC_RERANKER_BATCH_SIZE", "8")
        ),
        rag_semantic_reranker_max_length=int(
            os.getenv("RAG_SEMANTIC_RERANKER_MAX_LENGTH", "512")
        ),
        embedding_cache_ttl_seconds=int(os.getenv("EMBEDDING_CACHE_TTL_SECONDS", "86400")),
        refund_lock_ttl_seconds=int(os.getenv("REFUND_LOCK_TTL_SECONDS", "15")),
        refund_lock_wait_seconds=float(os.getenv("REFUND_LOCK_WAIT_SECONDS", "3")),
        refund_idempotency_ttl_seconds=int(os.getenv("REFUND_IDEMPOTENCY_TTL_SECONDS", "86400")),
        tool_timeout_overrides=load_tool_timeout_overrides(),
        tool_retry_backoff_seconds=max(
            0.0,
            float(os.getenv("TOOL_RETRY_BACKOFF_SECONDS", "0.1")),
        ),
        tool_timeout_worker_limit=max(
            1,
            int(os.getenv("TOOL_TIMEOUT_WORKER_LIMIT", "16")),
        ),
    )
