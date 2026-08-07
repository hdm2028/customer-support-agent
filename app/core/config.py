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
    )
