from dataclasses import dataclass

from app.rag.query_context import RetrievalQuery
from app.rag.reranker import rerank_documents


HYBRID_MODE = "hybrid"
RULE_RERANK_MODE = "hybrid_rule"
SEMANTIC_RERANK_MODE = "hybrid_semantic"
SEMANTIC_CONSTRAINT_MODE = "hybrid_semantic_constraint"
RANKING_MODES = (
    HYBRID_MODE,
    RULE_RERANK_MODE,
    SEMANTIC_RERANK_MODE,
    SEMANTIC_CONSTRAINT_MODE,
)


@dataclass(frozen=True)
class EvidenceConstraint:
    """Explicit evidence requirements supplied by an upstream contract."""

    required_sources: tuple[str, ...] = ()
    required_terms: tuple[str, ...] = ()


def _normalized(text: object) -> str:
    return str(text or "").lower().replace(" ", "")


def _candidate_text(candidate: dict) -> str:
    return "\n".join(
        [
            str(candidate.get("source", "")),
            str(candidate.get("section", "")),
            str(candidate.get("text", "")),
        ]
    )


def semantic_rerank_documents(candidates: list[dict]) -> list[dict]:
    """Rerank by semantic relevance only, without business-rule signals."""

    reranked = []
    for candidate in candidates:
        semantic_score = float(
            candidate.get("semantic_score", candidate.get("vector_score", 0))
            or 0
        )
        reranked.append(
            {
                **candidate,
                "semantic_rerank_score": round(semantic_score, 4),
                "score": round(semantic_score, 4),
            }
        )

    reranked.sort(
        key=lambda item: (
            item["semantic_rerank_score"],
            float(item.get("hybrid_score", 0) or 0),
        ),
        reverse=True,
    )
    return reranked


def evaluate_evidence_constraint(
    candidates: list[dict],
    constraint: EvidenceConstraint | None,
) -> dict:
    if constraint is None:
        return {
            "source_satisfied": True,
            "matched_terms": [],
            "missing_terms": [],
            "required_evidence_coverage": 1.0,
            "constraint_satisfied": True,
        }

    sources = {str(candidate.get("source", "")) for candidate in candidates}
    source_satisfied = bool(
        not constraint.required_sources
        or sources.intersection(constraint.required_sources)
    )
    combined = _normalized("\n".join(_candidate_text(item) for item in candidates))
    matched_terms = [
        term
        for term in constraint.required_terms
        if _normalized(term) in combined
    ]
    missing_terms = [
        term
        for term in constraint.required_terms
        if term not in matched_terms
    ]
    term_coverage = (
        len(matched_terms) / len(constraint.required_terms)
        if constraint.required_terms
        else 1.0
    )
    required_parts = len(constraint.required_terms) + bool(constraint.required_sources)
    covered_parts = len(matched_terms) + bool(
        constraint.required_sources and source_satisfied
    )
    evidence_coverage = (
        covered_parts / required_parts
        if required_parts
        else 1.0
    )

    return {
        "source_satisfied": source_satisfied,
        "matched_terms": matched_terms,
        "missing_terms": missing_terms,
        "term_coverage": round(term_coverage, 4),
        "required_evidence_coverage": round(evidence_coverage, 4),
        "constraint_satisfied": bool(source_satisfied and not missing_terms),
    }


def apply_business_evidence_constraint(
    candidates: list[dict],
    constraint: EvidenceConstraint | None,
    *,
    top_k: int,
) -> list[dict]:
    """Prioritize explicit evidence coverage while preserving candidate membership."""

    if constraint is None or top_k <= 0:
        return [dict(candidate) for candidate in candidates]

    remaining = list(enumerate(candidates))
    selected: list[tuple[int, dict]] = []
    uncovered_terms = {
        _normalized(term): term
        for term in constraint.required_terms
    }
    source_needed = bool(constraint.required_sources)

    while remaining and len(selected) < top_k and (uncovered_terms or source_needed):
        best_position = None
        best_gain = (0, 0, 0)

        for position, (original_rank, candidate) in enumerate(remaining):
            normalized_text = _normalized(_candidate_text(candidate))
            covered_terms = sum(
                1 for term in uncovered_terms if term in normalized_text
            )
            source_gain = int(
                source_needed
                and candidate.get("source") in constraint.required_sources
            )
            gain = (covered_terms + source_gain, source_gain, -original_rank)

            if gain > best_gain:
                best_gain = gain
                best_position = position

        if best_position is None or best_gain[0] <= 0:
            break

        chosen = remaining.pop(best_position)
        selected.append(chosen)
        chosen_text = _normalized(_candidate_text(chosen[1]))
        uncovered_terms = {
            normalized: original
            for normalized, original in uncovered_terms.items()
            if normalized not in chosen_text
        }
        if chosen[1].get("source") in constraint.required_sources:
            source_needed = False

    ordered = selected + remaining
    return [
        {
            **candidate,
            "constraint_applied": True,
            "constraint_original_rank": original_rank + 1,
        }
        for original_rank, candidate in ordered
    ]


def rank_candidates(
    query: RetrievalQuery,
    candidates: list[dict],
    *,
    mode: str,
    top_k: int,
    evidence_constraint: EvidenceConstraint | None = None,
) -> list[dict]:
    if mode == HYBRID_MODE:
        return [dict(candidate) for candidate in candidates]

    if mode == RULE_RERANK_MODE:
        return rerank_documents(query.semantic_query, candidates)

    semantic = semantic_rerank_documents(candidates)

    if mode == SEMANTIC_RERANK_MODE:
        return semantic

    if mode == SEMANTIC_CONSTRAINT_MODE:
        return apply_business_evidence_constraint(
            semantic,
            evidence_constraint,
            top_k=top_k,
        )

    raise ValueError(f"Unsupported RAG ranking mode: {mode}")
