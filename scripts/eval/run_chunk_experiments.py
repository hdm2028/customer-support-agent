"""Build and export the registered chunking baselines with identical KB input."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import BASE_DIR, KNOWLEDGE_DIR
from app.rag.ingestion.service import KnowledgeIngestionService
from app.rag.ingestion.chunker import CHUNK_STRATEGIES, token_count


EXPERIMENTS = {
    "chunk_a_fixed_256": "fixed_256",
    "chunk_b_fixed_512": "fixed_512",
    "chunk_c_markdown": "markdown",
    "chunk_d_type_aware": "type_aware",
    "chunk_e_fixed_128": "fixed_128",
}


def run_one(name: str, strategy: str, knowledge_dir: Path, output_dir: Path) -> dict:
    service = KnowledgeIngestionService(knowledge_dir, chunk_strategy=strategy)
    result = service.build(compare_with_stored=False)
    chunks = [chunk.to_dict() for chunk in result.chunks]
    target = output_dir / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "chunks.jsonl").write_text(
        "".join(json.dumps({
            "chunk_id": item["chunk_id"], "source": item["source"],
            "section": item["section"], "section_title": item["metadata"].get("section_title"),
            "chunk_index": item["metadata"].get("chunk_index"),
            "token_count": item["metadata"].get("token_count", token_count(item["text"])),
            "text": item["text"],
        }, ensure_ascii=False) + "\n" for item in chunks), encoding="utf-8")
    counts = [item["metadata"].get("token_count", token_count(item["text"])) for item in chunks]
    summary = {
        "experiment": name, "chunk_strategy": strategy, "knowledge_dir": str(knowledge_dir),
        "chunk_count": len(chunks),
        "average_token_count": round(sum(counts) / len(counts), 4) if counts else 0,
        "min_token_count": min(counts) if counts else 0,
        "max_token_count": max(counts) if counts else 0,
        "chunks_path": str(target / "chunks.jsonl"),
        "chunker_version": result.manifest.chunker_version,
    }
    (target / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--knowledge-dir", default=str(KNOWLEDGE_DIR))
    parser.add_argument("--output-dir", default=str(BASE_DIR / "reports" / "chunk_experiments"))
    args = parser.parse_args()
    summaries = [run_one(name, strategy, Path(args.knowledge_dir), Path(args.output_dir))
                 for name, strategy in EXPERIMENTS.items()]
    output = Path(args.output_dir) / "summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
