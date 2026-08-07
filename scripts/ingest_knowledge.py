import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.rag import list_chunks, search_documents


def main() -> None:
    chunks = list_chunks()
    print(f"知识库切分完成，共 {len(chunks)} 个 chunk。")

    for chunk in chunks[:5]:
        print("-" * 60)
        print(f"chunk_id: {chunk['chunk_id']}")
        print(f"citation: {chunk['citation']}")
        print(f"text: {chunk['text'][:120]}")

    print("\n检索测试：")
    results = search_documents("我的耳机坏了，还在保修期内吗？", top_k=3)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
