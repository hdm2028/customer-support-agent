import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.rag.embedding_client import get_embedding_provider


def main() -> None:
    settings = get_settings()
    provider = get_embedding_provider()

    text = "耳机坏了，还在保修期内吗？"
    vector = provider.embed_text(text)

    print(f"embedding provider: {settings.rag_embedding_provider}")
    print(f"embedding model: {settings.zhipu_embedding_model}")
    print(f"vector length: {len(vector)}")
    print(f"first 5 values: {vector[:5]}")


if __name__ == "__main__":
    main()
