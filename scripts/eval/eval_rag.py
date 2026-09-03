from __future__ import annotations

import argparse
import hashlib
from collections import Counter, defaultdict
from pathlib import Path

from app.agent.policies.evidence_guardrail import validate_policy_evidence
from app.core.config import BASE_DIR, get_settings
from app.core.schemas import ToolResult
from app.rag.index_manager import RAGIndexManager
from app.rag.ingestion.chunker import (
    CHUNK_STRATEGIES as REGISTERED_CHUNK_STRATEGIES,
)
from app.rag.query_builder import build_retrieval_query
from app.rag.query_context import RAGQueryContext
from app.rag.ranking import (
    ABLATION_RUNNER_VERSION,
    BUSINESS_CONSTRAINT_VERSION,
    HYBRID_MODE,
    RANKING_MODES,
    RERANK_QUERY_MODE,
    RULE_RERANK_MODE,
    SEMANTIC_CONSTRAINT_MODE,
    SEMANTIC_FUSION_MODE,
    SEMANTIC_EVIDENCE_CATEGORIES,
    SEMANTIC_QUERY_MODES,
    SEMANTIC_RERANK_MODE,
    EvidenceConstraint,
    build_ablation_rankings,
    build_evidence_constraint,
    evaluate_evidence_constraint,
    resolve_semantic_rerank_query,
)
from app.rag.reranker import RULE_RERANKER_VERSION
from app.rag.retriever import HybridRetriever
from app.rag.semantic_reranker import (
    SemanticReranker,
    build_semantic_reranker,
)
from scripts.eval.common import (
    NA,
    average,
    build_skipped_report,
    load_jsonl,
    now_iso,
    print_json_report,
    rate,
    save_report,
)


DEFAULT_EVAL_PATH = BASE_DIR / "data" / "eval" / "rag_eval.jsonl"
DEFAULT_KNOWLEDGE_DIR = BASE_DIR / "data" / "knowledge"

DEFAULT_TOP_K = 5
CHUNK_STRATEGIES = tuple(REGISTERED_CHUNK_STRATEGIES)
CANDIDATE_K = 20
BASELINE_MODE = RULE_RERANK_MODE


COMPARISON_METRICS = (
    "hit_at_1",
    "hit_at_3",
    "hit_at_5",
    "mrr",
    "evidence_coverage_rate",
)


SIDE_EFFECTS = [
    "[READ ONLY BUSINESS DATA]",
    "[REFRESHES IN-MEMORY RAG INDEX]",
    "[WRITES CACHE]",
    "[CALLS EMBEDDING IF CONFIGURED]",
    "[LOADS LOCAL CROSS-ENCODER]",
]


VALID_SCENARIO_TYPES = {
    "single_policy",
    "state_conditioned",
    "cross_policy",
    "semantic_confusion",
    "exception_override",
    "authority_conflict",
    "insufficient_information",
}

VALID_DIFFICULTIES = {
    "easy",
    "medium",
    "hard",
}

VALID_CONTEXT_MODES = {
    "minimal",
    "explicit",
    "fact_enriched",
}

VALID_EVIDENCE_TYPES = {
    "refund_eligibility",
    "refund_application",
    "refund_state",
    "refund_timing",
    "refund_idempotency",
    "refund_risk_review",
    "order_cancel",
    "address_change",
    "shipping_status",
    "shipping_delay",
    "signed_not_received",
    "shipping_intercept",
    "product_quality",
    "warranty",
    "replacement",
    "custom_product",
    "human_review",
    "complaint_escalation",
    "membership_restriction",
    "payment_exception",
    "invoice",
}


# =============================================================================
# Path helpers
# =============================================================================


def resolve_path(value: str) -> Path:
    path = Path(value)

    if not path.is_absolute():
        path = BASE_DIR / path

    return path.resolve()


# =============================================================================
# Schema helpers
# =============================================================================


def is_v2_case(case: dict) -> bool:
    return case.get("schema_version") == "rag-eval-v2"


def validate_v2_case(case: dict) -> None:
    case_id = case.get("case_id", "<unknown>")

    if not str(case.get("query", "")).strip():
        raise ValueError(
            f"RAG v2 case {case_id} has empty query"
        )

    if case.get("split") not in {"dev", "holdout"}:
        raise ValueError(
            f"RAG v2 case {case_id} has invalid split: "
            f"{case.get('split')}"
        )

    if case.get("scenario_type") not in VALID_SCENARIO_TYPES:
        raise ValueError(
            f"RAG v2 case {case_id} has invalid scenario_type: "
            f"{case.get('scenario_type')}"
        )

    if case.get("difficulty") not in VALID_DIFFICULTIES:
        raise ValueError(
            f"RAG v2 case {case_id} has invalid difficulty: "
            f"{case.get('difficulty')}"
        )

    if case.get("context_mode") not in VALID_CONTEXT_MODES:
        raise ValueError(
            f"RAG v2 case {case_id} has invalid context_mode: "
            f"{case.get('context_mode')}"
        )

    input_context = case.get("input_context")

    if not isinstance(input_context, dict):
        raise ValueError(
            f"RAG v2 case {case_id} has no valid input_context"
        )

    expected = case.get("expected")

    if not isinstance(expected, dict):
        raise ValueError(
            f"RAG v2 case {case_id} has no valid expected object"
        )

    primary_targets = expected.get("primary_targets")

    if not isinstance(primary_targets, list) or not primary_targets:
        raise ValueError(
            f"RAG v2 case {case_id} must contain "
            f"at least one primary_target"
        )

    for target in primary_targets:
        if not isinstance(target, dict):
            raise ValueError(
                f"RAG v2 case {case_id} contains invalid primary_target"
            )

        if not str(target.get("source", "")).strip():
            raise ValueError(
                f"RAG v2 case {case_id} primary_target has no source"
            )

        sections = target.get("sections", [])

        if not isinstance(sections, list):
            raise ValueError(
                f"RAG v2 case {case_id} primary_target.sections "
                f"must be a list"
            )

    supporting_targets = expected.get("supporting_targets", [])

    if not isinstance(supporting_targets, list):
        raise ValueError(
            f"RAG v2 case {case_id} supporting_targets must be a list"
        )

    for target in supporting_targets:
        if not isinstance(target, dict):
            raise ValueError(
                f"RAG v2 case {case_id} contains invalid supporting_target"
            )

        if not str(target.get("source", "")).strip():
            raise ValueError(
                f"RAG v2 case {case_id} supporting_target has no source"
            )

    evidence_requirements = expected.get("evidence_requirements")

    if (
        not isinstance(evidence_requirements, list)
        or not evidence_requirements
    ):
        raise ValueError(
            f"RAG v2 case {case_id} must contain evidence_requirements"
        )

    for requirement in evidence_requirements:
        if not isinstance(requirement, dict):
            raise ValueError(
                f"RAG v2 case {case_id} contains "
                f"invalid evidence requirement"
            )

        evidence_type = str(
            requirement.get("evidence_type", "")
        ).strip()

        if not evidence_type:
            raise ValueError(
                f"RAG v2 case {case_id} evidence requirement "
                f"has no evidence_type"
            )

        if evidence_type not in VALID_EVIDENCE_TYPES:
            raise ValueError(
                f"RAG v2 case {case_id} has unknown evidence_type: "
                f"{evidence_type}"
            )


def validate_cases(cases: list[dict]) -> None:
    seen_case_ids: set[str] = set()

    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise ValueError(
                f"RAG dataset row {index} is not a JSON object"
            )

        case_id = str(case.get("case_id", "")).strip()

        if not case_id:
            raise ValueError(
                f"RAG dataset row {index} has no case_id"
            )

        if case_id in seen_case_ids:
            raise ValueError(
                f"Duplicate RAG case_id: {case_id}"
            )

        seen_case_ids.add(case_id)

        if not str(case.get("query", "")).strip():
            raise ValueError(
                f"RAG case {case_id} has empty query"
            )

        if is_v2_case(case):
            validate_v2_case(case)


# =============================================================================
# Static knowledge-base validation
# =============================================================================


def normalize_reference_text(value: object) -> str:
    return (
        str(value or "")
        .strip()
        .replace("\\", "/")
        .lower()
    )


def extract_markdown_sections(path: Path) -> set[str]:
    """
    Read Markdown headings.

    Example:
        ## 退款到账
        -> "退款到账"
    """

    sections: set[str] = set()

    text = path.read_text(
        encoding="utf-8",
        errors="ignore",
    )

    for line in text.splitlines():
        stripped = line.strip()

        if not stripped.startswith("#"):
            continue

        heading = stripped.lstrip("#").strip()

        if heading:
            sections.add(heading)

    return sections


def build_knowledge_reference_index(
    knowledge_dir: Path,
) -> dict:
    if not knowledge_dir.exists():
        raise FileNotFoundError(
            f"Knowledge directory does not exist: {knowledge_dir}"
        )

    if not knowledge_dir.is_dir():
        raise NotADirectoryError(
            f"Knowledge path is not a directory: {knowledge_dir}"
        )

    markdown_files = sorted(
        path
        for path in knowledge_dir.rglob("*.md")
        if path.is_file()
    )

    if not markdown_files:
        raise ValueError(
            f"No Markdown knowledge files found under: {knowledge_dir}"
        )

    sources: dict[str, set[str]] = {}
    source_paths: dict[str, list[str]] = defaultdict(list)

    for path in markdown_files:
        relative_source = (
            path.relative_to(knowledge_dir)
            .as_posix()
        )

        basename = path.name

        sections = extract_markdown_sections(path)

        # Support both:
        #   退款政策.md
        #
        # and:
        #   policy/退款政策.md
        for source_name in {
            relative_source,
            basename,
        }:
            key = normalize_reference_text(
                source_name
            )

            if key not in sources:
                sources[key] = set()

            sources[key].update(sections)

            source_paths[key].append(
                str(path)
            )

    ambiguous_basenames = {
        source: paths
        for source, paths in source_paths.items()
        if "/" not in source
        and len(set(paths)) > 1
    }

    return {
        "sources": sources,
        "source_paths": source_paths,
        "ambiguous_basenames": ambiguous_basenames,
        "markdown_files": [
            str(path)
            for path in markdown_files
        ],
    }


def source_exists(
    source: str,
    kb_index: dict,
) -> bool:
    key = normalize_reference_text(source)

    return key in kb_index["sources"]


def available_sections_for_source(
    source: str,
    kb_index: dict,
) -> set[str]:
    key = normalize_reference_text(source)

    return kb_index["sources"].get(
        key,
        set(),
    )


def section_exists_for_source(
    source: str,
    section: str,
    kb_index: dict,
) -> bool:
    available = available_sections_for_source(
        source,
        kb_index,
    )

    normalized_expected = normalize_reference_text(
        section
    )

    return any(
        normalize_reference_text(actual)
        == normalized_expected
        for actual in available
    )


def validate_target_references(
    *,
    case_id: str,
    targets: list[dict],
    target_type: str,
    kb_index: dict,
    errors: list[dict],
) -> None:
    for target in targets:
        source = str(
            target.get("source", "")
        ).strip()

        if not source:
            errors.append(
                {
                    "case_id": case_id,
                    "type": f"{target_type}_missing_source",
                    "source": source,
                    "section": None,
                }
            )
            continue

        if not source_exists(
            source,
            kb_index,
        ):
            errors.append(
                {
                    "case_id": case_id,
                    "type": f"{target_type}_source_not_found",
                    "source": source,
                    "section": None,
                }
            )
            continue

        for section in target.get(
            "sections",
            [],
        ):
            if not section_exists_for_source(
                source,
                section,
                kb_index,
            ):
                errors.append(
                    {
                        "case_id": case_id,
                        "type": f"{target_type}_section_not_found",
                        "source": source,
                        "section": section,
                    }
                )


def validate_evidence_requirement_references(
    *,
    case_id: str,
    requirements: list[dict],
    kb_index: dict,
    errors: list[dict],
) -> None:
    for requirement in requirements:
        evidence_type = requirement[
            "evidence_type"
        ]

        acceptable_sources = list(
            requirement.get(
                "acceptable_sources",
                [],
            )
        )

        acceptable_sections = list(
            requirement.get(
                "acceptable_sections",
                [],
            )
        )

        valid_sources: list[str] = []

        for source in acceptable_sources:
            if not source_exists(
                source,
                kb_index,
            ):
                errors.append(
                    {
                        "case_id": case_id,
                        "type": "evidence_source_not_found",
                        "evidence_type": evidence_type,
                        "source": source,
                        "section": None,
                    }
                )
            else:
                valid_sources.append(
                    source
                )

        # acceptable_sections means:
        #
        # the section must exist in at least one
        # acceptable source.
        for section in acceptable_sections:
            found = any(
                section_exists_for_source(
                    source,
                    section,
                    kb_index,
                )
                for source in valid_sources
            )

            if not found:
                errors.append(
                    {
                        "case_id": case_id,
                        "type": "evidence_section_not_found",
                        "evidence_type": evidence_type,
                        "source": valid_sources,
                        "section": section,
                    }
                )


def validate_dataset_ground_truth(
    *,
    cases: list[dict],
    dataset_path: Path,
    knowledge_dir: Path,
) -> dict:
    kb_index = build_knowledge_reference_index(
        knowledge_dir
    )

    reference_errors: list[dict] = []

    scenario_counts: Counter = Counter()
    difficulty_counts: Counter = Counter()
    context_mode_counts: Counter = Counter()
    evidence_type_counts: Counter = Counter()

    for case in cases:
        if not is_v2_case(case):
            continue

        case_id = case["case_id"]

        scenario_counts[
            case["scenario_type"]
        ] += 1

        difficulty_counts[
            case["difficulty"]
        ] += 1

        context_mode_counts[
            case["context_mode"]
        ] += 1

        expected = case["expected"]

        primary_targets = list(
            expected.get(
                "primary_targets",
                [],
            )
        )

        supporting_targets = list(
            expected.get(
                "supporting_targets",
                [],
            )
        )

        requirements = list(
            expected.get(
                "evidence_requirements",
                [],
            )
        )

        for requirement in requirements:
            evidence_type_counts[
                requirement["evidence_type"]
            ] += 1

        validate_target_references(
            case_id=case_id,
            targets=primary_targets,
            target_type="primary",
            kb_index=kb_index,
            errors=reference_errors,
        )

        validate_target_references(
            case_id=case_id,
            targets=supporting_targets,
            target_type="supporting",
            kb_index=kb_index,
            errors=reference_errors,
        )

        validate_evidence_requirement_references(
            case_id=case_id,
            requirements=requirements,
            kb_index=kb_index,
            errors=reference_errors,
        )

    error_counts = Counter(
        item["type"]
        for item in reference_errors
    )

    digest = hashlib.sha256(
        dataset_path.read_bytes()
    ).hexdigest()

    passed = not reference_errors

    return {
        "report_type": "rag_dataset_validation",
        "passed": passed,

        "dataset": str(
            dataset_path
        ),

        "dataset_sha256": (
            f"sha256:{digest}"
        ),

        "knowledge_dir": str(
            knowledge_dir
        ),

        "dataset_cases": len(
            cases
        ),

        "v2_cases": sum(
            1
            for case in cases
            if is_v2_case(case)
        ),

        "knowledge_markdown_files": len(
            kb_index["markdown_files"]
        ),

        "schema_validation": {
            "passed": True,
            "duplicate_case_ids": 0,
        },

        "ground_truth_validation": {
            "passed": passed,

            "error_count": len(
                reference_errors
            ),

            "error_counts": dict(
                error_counts
            ),

            "errors": (
                reference_errors
            ),
        },

        "dataset_distribution": {
            "scenario_type": dict(
                sorted(
                    scenario_counts.items()
                )
            ),

            "difficulty": dict(
                sorted(
                    difficulty_counts.items()
                )
            ),

            "context_mode": dict(
                sorted(
                    context_mode_counts.items()
                )
            ),

            "evidence_type": dict(
                sorted(
                    evidence_type_counts.items()
                )
            ),
        },

        "knowledge_reference_index": {
            "ambiguous_basenames": (
                kb_index[
                    "ambiguous_basenames"
                ]
            ),
        },

        "created_at": now_iso(),
    }


# =============================================================================
# V1 helpers
# =============================================================================


def expected_sources(case: dict) -> list[str]:
    if case.get(
        "expected_document"
    ):
        return [
            case["expected_document"]
        ]

    return list(
        case.get(
            "expected_sources",
            [],
        )
    )


# =============================================================================
# Query contract
# =============================================================================


def case_query_context(
    case: dict,
) -> RAGQueryContext:
    if is_v2_case(case):
        data = case.get(
            "input_context"
        )

        context_field = (
            "input_context"
        )

    else:
        data = case.get(
            "rag_context"
        )

        context_field = (
            "rag_context"
        )

    if not isinstance(
        data,
        dict,
    ):
        raise ValueError(
            f"RAG case "
            f"{case.get('case_id', '<unknown>')} "
            f"has no valid {context_field}"
        )

    return RAGQueryContext(
        raw_query=case[
            "query"
        ].strip(),
        **data,
    )


# =============================================================================
# General scoring helpers
# =============================================================================


def first_expected_rank(
    results: list[dict],
    sources: list[str],
) -> int | None:
    for index, item in enumerate(
        results,
        start=1,
    ):
        if (
            item.get("source")
            in sources
        ):
            return index

    return None


def source_concentration(
    results: list[dict],
) -> float:
    sources = [
        item.get("source")
        for item in results
        if item.get("source")
    ]

    if not sources:
        return 0.0

    return round(
        max(
            Counter(
                sources
            ).values()
        )
        / len(sources),
        4,
    )


def _normalized(
    text: object,
) -> str:
    return (
        str(text or "")
        .lower()
        .replace(" ", "")
    )


def keyword_evidence_report(
    results: list[dict],
    terms: list[str],
) -> dict:
    combined = _normalized(
        "\n".join(
            "\n".join(
                (
                    str(
                        item.get(
                            "source",
                            "",
                        )
                    ),
                    str(
                        item.get(
                            "section",
                            "",
                        )
                    ),
                    str(
                        item.get(
                            "text",
                            "",
                        )
                    ),
                )
            )
            for item in results
        )
    )

    matched = [
        term
        for term in terms
        if _normalized(
            term
        )
        in combined
    ]

    missing = [
        term
        for term in terms
        if term not in matched
    ]

    coverage = (
        len(matched)
        / len(terms)
        if terms
        else 1.0
    )

    return {
        "matched_terms": matched,
        "missing_terms": missing,
        "coverage": round(
            coverage,
            4,
        ),
        "satisfied": not missing,
    }


def simplify_result(
    result: dict,
) -> dict:
    return {
        "chunk_id": result.get(
            "chunk_id"
        ),

        "source": result.get(
            "source"
        ),

        "section": result.get(
            "section"
        ),
        "covered_sections": _result_sections(
            result
        ),

        "citation": result.get(
            "citation"
        ),

        "retrieval_rank": result.get(
            "retrieval_rank"
        ),

        "retrieval_score": result.get(
            "retrieval_score"
        ),

        "hybrid_score": result.get(
            "hybrid_score"
        ),

        "vector_score": result.get(
            "vector_score"
        ),

        "bm25_score": result.get(
            "bm25_score"
        ),

        "keyword_score": result.get(
            "keyword_score"
        ),

        "rule_score": result.get(
            "rule_score"
        ),

        "rule_boost": result.get(
            "rule_boost"
        ),

        "rule_reason": result.get(
            "rule_reason",
            [],
        ),

        "semantic_rerank_score": result.get(
            "semantic_rerank_score"
        ),

        "semantic_rank": result.get(
            "semantic_rank"
        ),

        "constraint_adjusted": result.get(
            "constraint_adjusted",
            False,
        ),

        "constraint_reason": result.get(
            "constraint_reason"
        ),

        "final_rank": result.get(
            "final_rank"
        ),

        "text_preview": result.get(
            "text",
            "",
        )[:180],
    }


# =============================================================================
# V2 target matching
# =============================================================================


def _result_sections(
    result: dict,
) -> list[str]:
    """
    Return all Markdown sections covered by one retrieved chunk.

    Compatible with:
    - Markdown / TypeAware:
        result["section"]
    - Fixed:
        result["section"]
        + result["metadata"]["covered_sections"]

    Some retrieval implementations may flatten metadata,
    so result["covered_sections"] is also supported.
    """
    sections: list[str] = []

    section = result.get("section")

    if section:
        sections.append(
            str(section)
        )

    # ---------------------------------------------------------
    # Case 1:
    # covered_sections is flattened onto the retrieval result.
    # ---------------------------------------------------------
    direct_covered = result.get(
        "covered_sections"
    )

    if isinstance(
        direct_covered,
        (list, tuple, set),
    ):
        sections.extend(
            str(item)
            for item in direct_covered
            if item
        )

    # ---------------------------------------------------------
    # Case 2:
    # covered_sections remains inside metadata.
    # ---------------------------------------------------------
    metadata = result.get(
        "metadata"
    )

    if isinstance(
        metadata,
        dict,
    ):
        metadata_covered = metadata.get(
            "covered_sections",
            [],
        )

        if isinstance(
            metadata_covered,
            (list, tuple, set),
        ):
            sections.extend(
                str(item)
                for item in metadata_covered
                if item
            )

    # 去重，同时保留原顺序
    normalized_seen: set[str] = set()
    output: list[str] = []

    for section_name in sections:
        normalized = _normalized(
            section_name
        )

        if (
            normalized
            and normalized
            not in normalized_seen
        ):
            normalized_seen.add(
                normalized
            )

            output.append(
                section_name
            )

    return output


def _section_matches(
    result: dict,
    expected_sections: list[str],
) -> bool:
    """
    A target section matches when any section covered by the
    retrieved chunk matches any expected section.

    This makes the evaluation independent of chunk boundaries.
    """
    if not expected_sections:
        return True

    actual_sections = {
        _normalized(section)
        for section in _result_sections(
            result
        )
        if section
    }

    expected = {
        _normalized(section)
        for section in expected_sections
        if section
    }

    return bool(
        actual_sections
        & expected
    )

def target_matches(
    result: dict,
    target: dict,
) -> bool:
    expected_source = (
        target.get(
            "source"
        )
    )

    if (
        expected_source
        and result.get(
            "source"
        )
        != expected_source
    ):
        return False

    expected_sections = list(
        target.get(
            "sections"
        )
        or []
    )

    if not _section_matches(
        result,
        expected_sections,
    ):
        return False

    return True
def first_primary_rank(
    results: list[dict],
    targets: list[dict],
) -> int | None:
    for index, result in enumerate(
        results,
        start=1,
    ):
        if any(
            target_matches(
                result,
                target,
            )
            for target
            in targets
        ):
            return index

    return None


def supporting_target_report(
    results: list[dict],
    targets: list[dict],
) -> dict:
    matched = []

    for target in targets:
        matching_chunks = [
            result
            for result in results
            if target_matches(
                result,
                target,
            )
        ]

        if matching_chunks:
            matched.append(
                {
                    "source": target.get(
                        "source"
                    ),

                    "sections": target.get(
                        "sections",
                        [],
                    ),

                    "matched_chunk_ids": [
                        item.get(
                            "chunk_id"
                        )
                        for item
                        in matching_chunks
                    ],
                }
            )

    return {
        "supported": bool(
            targets
        ),

        "hit": bool(
            matched
        ),

        "matched_targets": (
            matched
        ),
    }


# =============================================================================
# V2 evidence requirement matching
# =============================================================================


def evidence_text(
    result: dict,
) -> str:
    return _normalized(
        "\n".join(
            (
                str(
                    result.get(
                        "source",
                        "",
                    )
                ),
                str(
                    result.get(
                        "section",
                        "",
                    )
                ),
                str(
                    result.get(
                        "text",
                        "",
                    )
                ),
            )
        )
    )


def evidence_requirement_matches(
    result: dict,
    requirement: dict,
) -> bool:
    acceptable_sources = list(
        requirement.get(
            "acceptable_sources"
        )
        or []
    )

    if (
        acceptable_sources
        and result.get(
            "source"
        )
        not in acceptable_sources
    ):
        return False

    acceptable_sections = list(
        requirement.get(
            "acceptable_sections"
        )
        or []
    )

    if not _section_matches(
        result,
        acceptable_sections,
    ):
        return False

    text = evidence_text(
        result
    )

    keywords_any = list(
        requirement.get(
            "keywords_any"
        )
        or []
    )

    if (
        keywords_any
        and not any(
            _normalized(
                term
            )
            in text
            for term
            in keywords_any
        )
    ):
        return False

    keywords_all = list(
        requirement.get(
            "keywords_all"
        )
        or []
    )

    if (
        keywords_all
        and not all(
            _normalized(
                term
            )
            in text
            for term
            in keywords_all
        )
    ):
        return False

    critical_terms = list(
        requirement.get(
            "critical_terms"
        )
        or []
    )

    if (
        critical_terms
        and not all(
            _normalized(
                term
            )
            in text
            for term
            in critical_terms
        )
    ):
        return False

    return True
def evidence_requirements_report(
    results: list[dict],
    requirements: list[dict],
) -> dict:
    details = []

    for requirement in requirements:
        matches = [
            result
            for result in results
            if evidence_requirement_matches(
                result,
                requirement,
            )
        ]

        details.append(
            {
                "evidence_type": (
                    requirement[
                        "evidence_type"
                    ]
                ),

                "satisfied": bool(
                    matches
                ),

                "matched_chunk_ids": [
                    item.get(
                        "chunk_id"
                    )
                    for item
                    in matches
                ],

                "matched_sources": list(
                    dict.fromkeys(
                        item.get(
                            "source"
                        )
                        for item
                        in matches
                        if item.get(
                            "source"
                        )
                    )
                ),

                "matched_sections": list(
                    dict.fromkeys(
                        item.get(
                            "section"
                        )
                        for item
                        in matches
                        if item.get(
                            "section"
                        )
                    )
                ),
            }
        )

    satisfied_count = sum(
        item["satisfied"]
        for item in details
    )

    total = len(
        details
    )

    coverage = (
        satisfied_count
        / total
        if total
        else 1.0
    )

    missing_evidence_types = [
        item[
            "evidence_type"
        ]
        for item
        in details
        if not item[
            "satisfied"
        ]
    ]

    return {
        "total_requirements": (
            total
        ),

        "satisfied_requirements": (
            satisfied_count
        ),

        "coverage": round(
            coverage,
            4,
        ),

        "satisfied": (
            satisfied_count
            == total
        ),

        "missing_evidence_types": (
            missing_evidence_types
        ),

        "details": details,
    }


# =============================================================================
# Failure diagnosis
# =============================================================================


def diagnose_failure(
    *,
    pool_source_pass: bool,
    pool_keywords: dict,
    pool_constraint: dict,
    result_source_pass: bool,
    result_keywords: dict,
    result_constraint: dict,
    evidence_guardrail_pass: bool,
) -> tuple[str, str]:
    pool_problems = []

    if not pool_source_pass:
        pool_problems.append(
            "expected source absent from Top20"
        )

    if not pool_keywords[
        "satisfied"
    ]:
        pool_problems.append(
            "expected terms absent from Top20: "
            + ", ".join(
                pool_keywords[
                    "missing_terms"
                ]
            )
        )

    if not pool_constraint[
        "constraint_satisfied"
    ]:
        pool_problems.append(
            "required categories absent from Top20: "
            + ", ".join(
                pool_constraint[
                    "missing_categories"
                ]
            )
        )

    if pool_problems:
        return (
            "retrieval_failure",
            "; ".join(
                pool_problems
            ),
        )

    ranking_problems = []

    if not result_source_pass:
        ranking_problems.append(
            "expected source did not reach final TopK"
        )

    if not result_keywords[
        "satisfied"
    ]:
        ranking_problems.append(
            "expected terms did not reach final TopK: "
            + ", ".join(
                result_keywords[
                    "missing_terms"
                ]
            )
        )

    if ranking_problems:
        return (
            "ranking_failure",
            "; ".join(
                ranking_problems
            ),
        )

    if not result_constraint[
        "constraint_satisfied"
    ]:
        return (
            "evidence_coverage_failure",
            "required categories missing from final TopK: "
            + ", ".join(
                result_constraint[
                    "missing_categories"
                ]
            ),
        )

    if not evidence_guardrail_pass:
        return (
            "evidence_guardrail_failure",
            "existing evidence guardrail rejected TopK",
        )

    return (
        "passed",
        "all retrieval, ranking, and evidence checks passed",
    )


def diagnose_v2_failure(
    *,
    candidate_primary_rank: int | None,
    result_primary_rank: int | None,
    candidate_evidence: dict,
    result_evidence: dict,
    evidence_guardrail_pass: bool,
) -> tuple[str, str]:
    retrieval_problems = []

    if (
        candidate_primary_rank
        is None
    ):
        retrieval_problems.append(
            "primary target absent from Top20"
        )

    if not candidate_evidence[
        "satisfied"
    ]:
        retrieval_problems.append(
            "required evidence absent from Top20: "
            + ", ".join(
                candidate_evidence[
                    "missing_evidence_types"
                ]
            )
        )

    if retrieval_problems:
        return (
            "retrieval_failure",
            "; ".join(
                retrieval_problems
            ),
        )

    if (
        result_primary_rank
        is None
    ):
        return (
            "ranking_failure",
            "primary target did not reach final TopK",
        )

    if not result_evidence[
        "satisfied"
    ]:
        return (
            "evidence_coverage_failure",
            "required evidence missing from final TopK: "
            + ", ".join(
                result_evidence[
                    "missing_evidence_types"
                ]
            ),
        )

    if not evidence_guardrail_pass:
        return (
            "evidence_guardrail_failure",
            "existing evidence guardrail rejected TopK",
        )

    return (
        "passed",
        "all v2 RAG evaluation checks passed",
    )


# =============================================================================
# V1 scoring
# =============================================================================


def score_mode_v1(
    case: dict,
    candidates: list[dict],
    ranked: list[dict],
    constraint: EvidenceConstraint,
    *,
    mode: str,
    top_k: int,
) -> dict:
    results = ranked[
        :top_k
    ]

    sources = expected_sources(
        case
    )

    terms = list(
        case.get(
            "expected_keywords",
            [],
        )
    )

    pool_rank = (
        first_expected_rank(
            candidates,
            sources,
        )
        if sources
        else None
    )

    result_rank = (
        first_expected_rank(
            results,
            sources,
        )
        if sources
        else None
    )

    pool_source_pass = (
        not sources
        or pool_rank
        is not None
    )

    result_source_pass = (
        not sources
        or result_rank
        is not None
    )

    pool_keywords = (
        keyword_evidence_report(
            candidates,
            terms,
        )
    )

    result_keywords = (
        keyword_evidence_report(
            results,
            terms,
        )
    )

    pool_constraint = (
        evaluate_evidence_constraint(
            candidates,
            constraint,
        )
    )

    result_constraint = (
        evaluate_evidence_constraint(
            results,
            constraint,
        )
    )

    guardrail_pass, guardrail_report = (
        validate_policy_evidence(
            case["query"],
            ToolResult(
                tool_name=(
                    "policy_search"
                ),
                success=bool(
                    results
                ),
                result=(
                    results
                    if results
                    else (
                        "RAG 没有返回任何政策证据。"
                    )
                ),
            ),
        )
    )

    failure_type, reason = (
        diagnose_failure(
            pool_source_pass=(
                pool_source_pass
            ),
            pool_keywords=(
                pool_keywords
            ),
            pool_constraint=(
                pool_constraint
            ),
            result_source_pass=(
                result_source_pass
            ),
            result_keywords=(
                result_keywords
            ),
            result_constraint=(
                result_constraint
            ),
            evidence_guardrail_pass=(
                guardrail_pass
            ),
        )
    )

    return {
        "schema_version": (
            "rag-eval-v1"
        ),

        "case_id": case[
            "case_id"
        ],

        "query": case[
            "query"
        ],

        "mode": mode,

        "passed": (
            failure_type
            == "passed"
        ),

        "failure_type": (
            failure_type
        ),

        "failure_stage": (
            failure_type
        ),

        "reason": reason,

        "source_metric_supported": bool(
            sources
        ),

        "expected_sources": (
            sources
            if sources
            else NA
        ),

        "retrieved_documents": [
            item.get(
                "source"
            )
            for item
            in results
        ],

        "expected_rank": (
            result_rank
            if result_rank
            is not None
            else NA
        ),

        "candidate_expected_rank": (
            pool_rank
            if pool_rank
            is not None
            else NA
        ),

        "hit_at_1": bool(
            result_rank
            is not None
            and result_rank
            <= 1
        ),

        "hit_at_3": bool(
            result_rank
            is not None
            and result_rank
            <= 3
        ),

        "hit_at_5": bool(
            result_rank
            is not None
            and result_rank
            <= 5
        ),

        "reciprocal_rank": (
            round(
                1
                / result_rank,
                4,
            )
            if result_rank
            else 0.0
        ),

        "required_evidence_categories": list(
            constraint.required_categories
        ),

        "evidence_coverage_rate": (
            result_constraint[
                "evidence_coverage_rate"
            ]
        ),

        "required_evidence_coverage": (
            result_constraint[
                "required_evidence_coverage"
            ]
        ),

        "candidate_evidence_coverage_rate": (
            pool_constraint[
                "evidence_coverage_rate"
            ]
        ),

        "keywords_pass": (
            result_keywords[
                "satisfied"
            ]
        ),

        "missing_keywords": (
            result_keywords[
                "missing_terms"
            ]
        ),

        "keyword_report": (
            result_keywords
        ),

        "candidate_keyword_report": (
            pool_keywords
        ),

        "source_concentration": (
            source_concentration(
                results
            )
        ),

        "constraint_satisfied": (
            result_constraint[
                "constraint_satisfied"
            ]
        ),

        "constraint_report": (
            result_constraint
        ),

        "candidate_constraint_report": (
            pool_constraint
        ),

        "evidence_guardrail_pass": (
            guardrail_pass
        ),

        "evidence_guardrail_report": (
            guardrail_report
        ),

        "supporting_target_hit": (
            False
        ),

        "candidate_chunk_ids": [
            item.get(
                "chunk_id"
            )
            for item
            in candidates
        ],

        "ranking_trace": [
            simplify_result(
                item
            )
            for item
            in results
        ],

        "notes": case.get(
            "notes",
            "",
        ),
    }


# =============================================================================
# V2 scoring
# =============================================================================


def score_mode_v2(
    case: dict,
    candidates: list[dict],
    ranked: list[dict],
    constraint: EvidenceConstraint,
    *,
    mode: str,
    top_k: int,
) -> dict:
    results = ranked[
        :top_k
    ]

    expected = case[
        "expected"
    ]

    primary_targets = list(
        expected.get(
            "primary_targets",
            [],
        )
    )

    supporting_targets = list(
        expected.get(
            "supporting_targets",
            [],
        )
    )

    evidence_requirements = list(
        expected.get(
            "evidence_requirements",
            [],
        )
    )

    candidate_primary_rank = (
        first_primary_rank(
            candidates,
            primary_targets,
        )
    )

    result_primary_rank = (
        first_primary_rank(
            results,
            primary_targets,
        )
    )

    candidate_evidence = (
        evidence_requirements_report(
            candidates,
            evidence_requirements,
        )
    )

    result_evidence = (
        evidence_requirements_report(
            results,
            evidence_requirements,
        )
    )

    supporting_report = (
        supporting_target_report(
            results,
            supporting_targets,
        )
    )

    system_candidate_constraint = (
        evaluate_evidence_constraint(
            candidates,
            constraint,
        )
    )

    system_result_constraint = (
        evaluate_evidence_constraint(
            results,
            constraint,
        )
    )

    guardrail_pass, guardrail_report = (
        validate_policy_evidence(
            case["query"],
            ToolResult(
                tool_name=(
                    "policy_search"
                ),
                success=bool(
                    results
                ),
                result=(
                    results
                    if results
                    else (
                        "RAG 没有返回任何政策证据。"
                    )
                ),
            ),
        )
    )

    failure_type, reason = (
        diagnose_v2_failure(
            candidate_primary_rank=(
                candidate_primary_rank
            ),
            result_primary_rank=(
                result_primary_rank
            ),
            candidate_evidence=(
                candidate_evidence
            ),
            result_evidence=(
                result_evidence
            ),
            evidence_guardrail_pass=(
                guardrail_pass
            ),
        )
    )

    return {
        "schema_version": case.get(
            "schema_version"
        ),

        "case_id": case[
            "case_id"
        ],

        "query": case[
            "query"
        ],

        "mode": mode,

        "split": case.get(
            "split"
        ),

        "scenario_type": case.get(
            "scenario_type"
        ),

        "difficulty": case.get(
            "difficulty"
        ),

        "context_mode": case.get(
            "context_mode"
        ),

        "passed": (
            failure_type
            == "passed"
        ),

        "failure_type": (
            failure_type
        ),

        "failure_stage": (
            failure_type
        ),

        "reason": reason,

        "source_metric_supported": bool(
            primary_targets
        ),

        "primary_targets": (
            primary_targets
        ),

        "expected_rank": (
            result_primary_rank
            if result_primary_rank
            is not None
            else NA
        ),

        "candidate_expected_rank": (
            candidate_primary_rank
            if candidate_primary_rank
            is not None
            else NA
        ),

        "hit_at_1": bool(
            result_primary_rank
            is not None
            and result_primary_rank
            <= 1
        ),

        "hit_at_3": bool(
            result_primary_rank
            is not None
            and result_primary_rank
            <= 3
        ),

        "hit_at_5": bool(
            result_primary_rank
            is not None
            and result_primary_rank
            <= 5
        ),

        "reciprocal_rank": (
            round(
                1
                / result_primary_rank,
                4,
            )
            if result_primary_rank
            else 0.0
        ),

        "evidence_coverage_rate": (
            result_evidence[
                "coverage"
            ]
        ),

        "required_evidence_coverage": (
            result_evidence[
                "coverage"
            ]
        ),

        "candidate_evidence_coverage_rate": (
            candidate_evidence[
                "coverage"
            ]
        ),

        "evidence_requirements_satisfied": (
            result_evidence[
                "satisfied"
            ]
        ),

        "candidate_evidence_requirements_satisfied": (
            candidate_evidence[
                "satisfied"
            ]
        ),

        "evidence_report": (
            result_evidence
        ),

        "candidate_evidence_report": (
            candidate_evidence
        ),

        "missing_evidence_types": (
            result_evidence[
                "missing_evidence_types"
            ]
        ),

        "supporting_target_hit": (
            supporting_report[
                "hit"
            ]
        ),

        "supporting_target_report": (
            supporting_report
        ),

        "required_evidence_categories": list(
            constraint.required_categories
        ),

        "constraint_satisfied": (
            system_result_constraint[
                "constraint_satisfied"
            ]
        ),

        "constraint_report": (
            system_result_constraint
        ),

        "candidate_constraint_report": (
            system_candidate_constraint
        ),

        "keywords_pass": (
            result_evidence[
                "satisfied"
            ]
        ),

        "missing_keywords": [],

        "keyword_report": {},

        "candidate_keyword_report": {},

        "retrieved_documents": [
            item.get(
                "source"
            )
            for item
            in results
        ],

        "source_concentration": (
            source_concentration(
                results
            )
        ),

        "evidence_guardrail_pass": (
            guardrail_pass
        ),

        "evidence_guardrail_report": (
            guardrail_report
        ),

        "candidate_chunk_ids": [
            item.get(
                "chunk_id"
            )
            for item
            in candidates
        ],

        "ranking_trace": [
            simplify_result(
                item
            )
            for item
            in results
        ],

        "notes": case.get(
            "notes",
            "",
        ),
    }


def score_mode(
    case: dict,
    candidates: list[dict],
    ranked: list[dict],
    constraint: EvidenceConstraint,
    *,
    mode: str,
    top_k: int,
) -> dict:
    if is_v2_case(
        case
    ):
        return score_mode_v2(
            case,
            candidates,
            ranked,
            constraint,
            mode=mode,
            top_k=top_k,
        )

    return score_mode_v1(
        case,
        candidates,
        ranked,
        constraint,
        mode=mode,
        top_k=top_k,
    )


# =============================================================================
# Aggregation
# =============================================================================


def build_mode_report(
    results: list[dict],
    *,
    mode: str,
    top_k: int,
) -> dict:
    total = len(
        results
    )

    failures = Counter(
        item[
            "failure_type"
        ]
        for item in results
        if item[
            "failure_type"
        ]
        != "passed"
    )

    failed_cases = [
        item
        for item in results
        if not item[
            "passed"
        ]
    ]

    v2_results = [
        item
        for item in results
        if item.get(
            "schema_version"
        )
        == "rag-eval-v2"
    ]

    if v2_results:
        supporting_supported = [
            item
            for item in v2_results
            if item.get(
                "supporting_target_report",
                {},
            ).get(
                "supported"
            )
        ]

        supporting_hit_rate = (
            rate(
                sum(
                    item[
                        "supporting_target_hit"
                    ]
                    for item
                    in supporting_supported
                ),
                len(
                    supporting_supported
                ),
            )
            if supporting_supported
            else NA
        )

        candidate_evidence_recall = (
            average(
                [
                    item[
                        "candidate_evidence_coverage_rate"
                    ]
                    for item
                    in v2_results
                ]
            )
        )

    else:
        supporting_hit_rate = (
            NA
        )

        candidate_evidence_recall = (
            average(
                [
                    item.get(
                        "candidate_evidence_coverage_rate",
                        0.0,
                    )
                    for item
                    in results
                ]
            )
        )

    return {
        "mode": mode,

        "total_cases": (
            total
        ),

        "metric_supported_cases": (
            total
        ),

        "passed_count": (
            total
            - len(
                failed_cases
            )
        ),

        "failed_count": len(
            failed_cases
        ),

        "top_k": top_k,

        "candidate_k": (
            CANDIDATE_K
        ),

        "hit_at_1": rate(
            sum(
                item[
                    "hit_at_1"
                ]
                for item
                in results
            ),
            total,
        ),

        "hit_at_3": rate(
            sum(
                item[
                    "hit_at_3"
                ]
                for item
                in results
            ),
            total,
        ),

        "hit_at_5": rate(
            sum(
                item[
                    "hit_at_5"
                ]
                for item
                in results
            ),
            total,
        ),

        "mrr": average(
            [
                item[
                    "reciprocal_rank"
                ]
                for item
                in results
            ]
        ),

        "candidate_evidence_recall_at_20": (
            candidate_evidence_recall
        ),

        "evidence_coverage_rate": (
            average(
                [
                    item[
                        "evidence_coverage_rate"
                    ]
                    for item
                    in results
                ]
            )
        ),

        "required_evidence_coverage": (
            average(
                [
                    item[
                        "required_evidence_coverage"
                    ]
                    for item
                    in results
                ]
            )
        ),

        "supporting_target_hit_rate": (
            supporting_hit_rate
        ),

        "source_concentration": (
            average(
                [
                    item[
                        "source_concentration"
                    ]
                    for item
                    in results
                ]
            )
        ),

        "constraint_satisfaction": rate(
            sum(
                item[
                    "constraint_satisfied"
                ]
                for item
                in results
            ),
            total,
        ),

        "keyword_pass_rate": rate(
            sum(
                item[
                    "keywords_pass"
                ]
                for item
                in results
            ),
            total,
        ),

        "evidence_guardrail_pass_rate": rate(
            sum(
                item[
                    "evidence_guardrail_pass"
                ]
                for item
                in results
            ),
            total,
        ),

        "failure_counts": dict(
            failures
        ),

        "failed_cases": (
            failed_cases
        ),

        "results": results,
    }

# =============================================================================
# Slice analysis
# =============================================================================


SLICE_FIELDS = (
    "scenario_type",
    "context_mode",
    "difficulty",
)


def compact_result_summary(
    results: list[dict],
) -> dict:
    """
    Compact metrics for one evaluation slice.

    This function intentionally does NOT include per-case results,
    so slice summaries remain small.
    """

    total = len(results)

    if total == 0:
        return {
            "total_cases": 0,
            "passed_count": 0,
            "pass_rate": 0.0,
            "hit_at_1": 0.0,
            "hit_at_3": 0.0,
            "hit_at_5": 0.0,
            "mrr": 0.0,
            "candidate_evidence_recall_at_20": 0.0,
            "evidence_coverage_rate": 0.0,
            "constraint_satisfaction": 0.0,
            "evidence_guardrail_pass_rate": 0.0,
            "failure_counts": {},
        }

    passed_count = sum(
        item["passed"]
        for item in results
    )

    failures = Counter(
        item["failure_type"]
        for item in results
        if item["failure_type"] != "passed"
    )

    return {
        "total_cases": total,

        "passed_count": passed_count,

        "pass_rate": rate(
            passed_count,
            total,
        ),

        "hit_at_1": rate(
            sum(
                item["hit_at_1"]
                for item in results
            ),
            total,
        ),

        "hit_at_3": rate(
            sum(
                item["hit_at_3"]
                for item in results
            ),
            total,
        ),

        "hit_at_5": rate(
            sum(
                item["hit_at_5"]
                for item in results
            ),
            total,
        ),

        "mrr": average(
            [
                item["reciprocal_rank"]
                for item in results
            ]
        ),

        "candidate_evidence_recall_at_20": average(
            [
                item.get(
                    "candidate_evidence_coverage_rate",
                    0.0,
                )
                for item in results
            ]
        ),

        "evidence_coverage_rate": average(
            [
                item.get(
                    "evidence_coverage_rate",
                    0.0,
                )
                for item in results
            ]
        ),

        "constraint_satisfaction": rate(
            sum(
                item.get(
                    "constraint_satisfied",
                    False,
                )
                for item in results
            ),
            total,
        ),

        "evidence_guardrail_pass_rate": rate(
            sum(
                item.get(
                    "evidence_guardrail_pass",
                    False,
                )
                for item in results
            ),
            total,
        ),

        "failure_counts": dict(
            failures
        ),
    }


def build_slice_summary(
    mode_reports: dict[str, dict],
) -> dict:
    """
    Build compact slice metrics for:

        scenario_type
        context_mode
        difficulty

    Structure:

        {
            "scenario_type": {
                "cross_policy": {
                    "hybrid": {...},
                    "hybrid_rule": {...},
                    ...
                }
            }
        }
    """

    summary: dict = {}

    baseline_results = mode_reports[
        HYBRID_MODE
    ]["results"]

    for field in SLICE_FIELDS:
        values = sorted(
            {
                str(
                    item.get(
                        field,
                        "unspecified",
                    )
                )
                for item in baseline_results
            }
        )

        field_summary = {}

        for value in values:
            mode_summary = {}

            for mode in RANKING_MODES:
                mode_results = mode_reports[
                    mode
                ]["results"]

                sliced_results = [
                    item
                    for item in mode_results
                    if str(
                        item.get(
                            field,
                            "unspecified",
                        )
                    )
                    == value
                ]

                mode_summary[
                    mode
                ] = compact_result_summary(
                    sliced_results
                )

            field_summary[
                value
            ] = mode_summary

        summary[
            field
        ] = field_summary

    return summary


# =============================================================================
# Rank movement
# =============================================================================


def numeric_rank(
    value: object,
) -> int | None:
    """
    expected_rank may be NA when primary target is outside final TopK.
    """

    if isinstance(
        value,
        bool,
    ):
        return None

    if isinstance(
        value,
        (int, float),
    ):
        return int(value)

    return None


def classify_rank_movement(
    before_rank: int | None,
    after_rank: int | None,
) -> str:
    """
    Compare Primary Target rank between two ranking modes.
    """

    if (
        before_rank is None
        and after_rank is None
    ):
        return "outside_top5_both"

    if (
        before_rank is None
        and after_rank is not None
    ):
        return "recovered_to_top5"

    if (
        before_rank is not None
        and after_rank is None
    ):
        return "dropped_from_top5"

    if after_rank < before_rank:
        return "promoted"

    if after_rank > before_rank:
        return "demoted"

    return "unchanged"


def compare_mode_rank_movement(
    *,
    before_mode: str,
    after_mode: str,
    mode_reports: dict[str, dict],
) -> dict:
    before_results = {
        item["case_id"]: item
        for item in mode_reports[
            before_mode
        ]["results"]
    }

    after_results = {
        item["case_id"]: item
        for item in mode_reports[
            after_mode
        ]["results"]
    }

    counts: Counter = Counter()
    cases_by_movement: dict[
        str,
        list[str],
    ] = defaultdict(list)

    details = []

    for case_id, before_item in before_results.items():
        after_item = after_results[
            case_id
        ]

        before_rank = numeric_rank(
            before_item.get(
                "expected_rank"
            )
        )

        after_rank = numeric_rank(
            after_item.get(
                "expected_rank"
            )
        )

        movement = classify_rank_movement(
            before_rank,
            after_rank,
        )

        counts[
            movement
        ] += 1

        cases_by_movement[
            movement
        ].append(
            case_id
        )

        details.append(
            {
                "case_id": case_id,

                "scenario_type": before_item.get(
                    "scenario_type"
                ),

                "context_mode": before_item.get(
                    "context_mode"
                ),

                "difficulty": before_item.get(
                    "difficulty"
                ),

                "before_rank": (
                    before_rank
                    if before_rank is not None
                    else NA
                ),

                "after_rank": (
                    after_rank
                    if after_rank is not None
                    else NA
                ),

                "movement": movement,
            }
        )

    promoted = counts[
        "promoted"
    ]

    demoted = counts[
        "demoted"
    ]

    recovered = counts[
        "recovered_to_top5"
    ]

    dropped = counts[
        "dropped_from_top5"
    ]

    return {
        "before_mode": before_mode,
        "after_mode": after_mode,

        "total_cases": len(
            details
        ),

        "counts": {
            "promoted": promoted,
            "unchanged": counts[
                "unchanged"
            ],
            "demoted": demoted,
            "recovered_to_top5": recovered,
            "dropped_from_top5": dropped,
            "outside_top5_both": counts[
                "outside_top5_both"
            ],
        },

        "net_top5_change": (
            recovered
            - dropped
        ),

        "positive_movement": (
            promoted
            + recovered
        ),

        "negative_movement": (
            demoted
            + dropped
        ),

        "cases_by_movement": dict(
            cases_by_movement
        ),

        "details": details,
    }


def build_rank_movement_report(
    mode_reports: dict[str, dict],
) -> dict:
    """
    Main comparisons needed for current A/B/C/D ablation.

        A -> B : Rule impact
        A -> C : Semantic reranker impact
        C -> D : Evidence constraint impact
        A -> D : End-to-end ranking impact
    """

    return {
        "A_to_B": compare_mode_rank_movement(
            before_mode=HYBRID_MODE,
            after_mode=RULE_RERANK_MODE,
            mode_reports=mode_reports,
        ),

        "A_to_C": compare_mode_rank_movement(
            before_mode=HYBRID_MODE,
            after_mode=SEMANTIC_RERANK_MODE,
            mode_reports=mode_reports,
        ),

        "C_to_D": compare_mode_rank_movement(
            before_mode=SEMANTIC_RERANK_MODE,
            after_mode=SEMANTIC_CONSTRAINT_MODE,
            mode_reports=mode_reports,
        ),

        "A_to_D": compare_mode_rank_movement(
            before_mode=HYBRID_MODE,
            after_mode=SEMANTIC_CONSTRAINT_MODE,
            mode_reports=mode_reports,
        ),

        "A_to_E": compare_mode_rank_movement(
            before_mode=HYBRID_MODE,
            after_mode=SEMANTIC_FUSION_MODE,
            mode_reports=mode_reports,
        ),

        "C_to_E": compare_mode_rank_movement(
            before_mode=SEMANTIC_RERANK_MODE,
            after_mode=SEMANTIC_FUSION_MODE,
            mode_reports=mode_reports,
        ),
    }
# =============================================================================
# A/B/C/D
# =============================================================================


def run_ablation(
    cases: list[dict],
    retriever: HybridRetriever,
    *,
    top_k: int,
    semantic_reranker: SemanticReranker,
    semantic_query_mode: str = RERANK_QUERY_MODE,
) -> dict[str, dict]:
    if top_k <= 0 or top_k > CANDIDATE_K:
        raise ValueError(
            f"top_k must be between 1 and {CANDIDATE_K}"
        )

    mode_results: dict[str, list[dict]] = {
        mode: []
        for mode in RANKING_MODES
    }

    for case in cases:
        context = case_query_context(
            case
        )

        constraint = build_evidence_constraint(
            context
        )

        query = build_retrieval_query(
            context
        )

        candidates = retriever.retrieve_candidates(
            query,
            candidate_k=CANDIDATE_K,
        )


        rankings = build_ablation_rankings(
            query,
            candidates,
            semantic_reranker=semantic_reranker,
            top_k=top_k,
            evidence_constraint=constraint,
            semantic_query_mode=semantic_query_mode,
        )

        # A / B / C / D 都必须分别 score + append
        for mode in RANKING_MODES:
            scored = score_mode(
                case,
                candidates,
                rankings[mode],
                constraint,
                mode=mode,
                top_k=top_k,
            )

            scored["query_contract"] = {
                "raw_query": context.raw_query,
                "semantic_query": query.semantic_query,
                "lexical_query": query.lexical_query,
                "rerank_query": query.rerank_query,
                "semantic_query_mode": semantic_query_mode,
                "semantic_reranker_input": resolve_semantic_rerank_query(
                    query,
                    semantic_query_mode,
                ),

                "input_context": {
                    "primary_intent": context.primary_intent,
                    "action_type": context.action_type,
                    "topic": context.topic,
                    "related_topics": list(
                        context.related_topics
                    ),
                    "order_status": context.order_status,
                    "shipping_status": context.shipping_status,
                    "product_name": context.product_name,
                    "product_category": context.product_category,
                    "signed_date": context.signed_date,
                    "handoff_required": context.handoff_required,
                },
            }

            mode_results[
                mode
            ].append(
                scored
            )

    return {
        mode: build_mode_report(
            results,
            mode=mode,
            top_k=top_k,
        )
        for mode, results
        in mode_results.items()
    }


# =============================================================================
# Comparison
# =============================================================================


def _numeric_metric(
    value: object,
) -> float | None:
    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return float(value)

    return None


def _metric_delta(
    before: dict,
    after: dict,
) -> dict:
    deltas: dict[str, float | str] = {}

    for metric in COMPARISON_METRICS:
        before_value = _numeric_metric(
            before.get(metric)
        )

        after_value = _numeric_metric(
            after.get(metric)
        )

        if (
            before_value is None
            or after_value is None
        ):
            deltas[metric] = NA
            continue

        deltas[metric] = round(
            after_value - before_value,
            4,
        )

    return deltas


def build_comparison(
    mode_reports: dict[
        str,
        dict,
    ],
) -> tuple[list[dict], dict]:
    table = [
        {
            "mode": mode,

            **{
                metric: (
                    mode_reports[
                        mode
                    ][
                        metric
                    ]
                )
                for metric
                in COMPARISON_METRICS
            },
        }
        for mode
        in RANKING_MODES
    ]

    deltas = {
        "A_to_B": _metric_delta(
            mode_reports[
                HYBRID_MODE
            ],
            mode_reports[
                RULE_RERANK_MODE
            ],
        ),

        "A_to_C": _metric_delta(
            mode_reports[
                HYBRID_MODE
            ],
            mode_reports[
                SEMANTIC_RERANK_MODE
            ],
        ),

        "C_to_D": _metric_delta(
            mode_reports[
                SEMANTIC_RERANK_MODE
            ],
            mode_reports[
                SEMANTIC_CONSTRAINT_MODE
            ],
        ),
        "A_to_E": _metric_delta(
            mode_reports[HYBRID_MODE],
            mode_reports[SEMANTIC_FUSION_MODE],
        ),
        "C_to_E": _metric_delta(
            mode_reports[SEMANTIC_RERANK_MODE],
            mode_reports[SEMANTIC_FUSION_MODE],
        ),
    }

    return (
        table,
        deltas,
    )


# =============================================================================
# Reproducibility
# =============================================================================


def dataset_identity(
    dataset_path: Path,
) -> dict:
    digest = (
        hashlib.sha256(
            dataset_path.read_bytes()
        ).hexdigest()
    )

    return {
        "path": str(
            dataset_path
        ),

        "version": (
            f"sha256:{digest}"
        ),
    }


def reproducibility_metadata(
    *,
    manager: RAGIndexManager,
    semantic_reranker: SemanticReranker,
    top_k: int,
    dataset_path: Path,
    semantic_query_mode: str,
) -> dict:
    settings = (
        get_settings()
    )

    index = (
        manager.get_active_index()
    )

    return {
        "runner_version": (
            ABLATION_RUNNER_VERSION
        ),

        "run_timestamp": (
            now_iso()
        ),

        "dataset": (
            dataset_identity(
                dataset_path
            )
        ),

        "kb_version": (
            manager.active_kb_version
        ),

        "candidate_k": (
            CANDIDATE_K
        ),

        "top_k": (
            top_k
        ),

        "semantic_query_mode": (
            semantic_query_mode
        ),

        "hybrid_retrieval": {
            "semantic_weight": (
                settings.rag_semantic_weight
            ),

            "bm25_weight": (
                settings.rag_bm25_weight
            ),

            "keyword_weight": (
                settings.rag_keyword_weight
            ),

            "candidate_multiplier": (
                settings.rag_candidate_multiplier
            ),
        },

        "embedding_identity": (
            index.embedding_identity
            if index
            else None
        ),

        "semantic_reranker": (
            semantic_reranker
            .identity
            .to_dict()
        ),

        "rule_reranker": {
            "mode": (
                RULE_RERANK_MODE
            ),

            "version": (
                RULE_RERANKER_VERSION
            ),
        },

        "business_constraint": {
            "version": (
                BUSINESS_CONSTRAINT_VERSION
            ),

            "semantic_mapping": dict(
                sorted(
                    SEMANTIC_EVIDENCE_CATEGORIES.items()
                )
            ),
        },
    }


# =============================================================================
# CLI
# =============================================================================


def parse_args() -> argparse.Namespace:
    parser = (
        argparse.ArgumentParser(
            description=(
                "Evaluate RAG rerank ablations."
            )
        )
    )

    parser.add_argument(
        "--validate-only",
        action="store_true",
        help=(
            "Validate dataset schema and "
            "ground truth references "
            "without running retrieval."
        ),
    )

    parser.add_argument(
        "--top-k",
        type=int,
        default=(
            DEFAULT_TOP_K
        ),
    )

    parser.add_argument(
        "--semantic-query-mode",
        choices=SEMANTIC_QUERY_MODES,
        default=RERANK_QUERY_MODE,
        help=(
            "Cross-Encoder input: semantic_query or rerank_query."
        ),
    )

    parser.add_argument(
        "--dataset",
        type=str,
        default=str(
            DEFAULT_EVAL_PATH
        ),
        help=(
            "Path to the RAG evaluation "
            "JSONL dataset."
        ),
    )

    parser.add_argument(
        "--knowledge-dir",
        type=str,
        default=str(
            DEFAULT_KNOWLEDGE_DIR
        ),
        help=(
            "Knowledge-base root directory "
            "used for static source/section validation."
        ),
    )
    parser.add_argument(
        "--chunk-strategy",
        choices=CHUNK_STRATEGIES,
        default="fixed_256",
        help="Chunk strategy for this isolated experiment.",
    )
    parser.add_argument(
        "--experiment-name",
        default=None,
        help="Optional report name prefix to keep experiment outputs separate.",
    )

    return (
        parser.parse_args()
    )


# =============================================================================
# Main
# =============================================================================


def main() -> None:
    args = (
        parse_args()
    )

    dataset_path = (
        resolve_path(
            args.dataset
        )
    )

    knowledge_dir = (
        resolve_path(
            args.knowledge_dir
        )
    )

    # =========================================================================
    # IMPORTANT:
    #
    # Dataset loading + static validation happen BEFORE:
    #
    #   RAGIndexManager()
    #   manager.refresh()
    #   HybridRetriever()
    #   build_semantic_reranker()
    #
    # Therefore --validate-only never loads embedding or Cross-Encoder models.
    # =========================================================================

    try:
        cases = (
            load_jsonl(
                dataset_path
            )
        )

        validate_cases(
            cases
        )

        # ---------------------------------------------------------------------
        # STATIC VALIDATION ONLY
        # ---------------------------------------------------------------------

        if args.validate_only:
            validation_report = (
                validate_dataset_ground_truth(
                    cases=cases,
                    dataset_path=(
                        dataset_path
                    ),
                    knowledge_dir=(
                        knowledge_dir
                    ),
                )
            )

            report_path = (
                save_report(
                    "eval_rag_validation",
                    validation_report,
                )
            )

            print_json_report(
                "RAG Dataset Validation",
                validation_report,
                report_path,
            )

            if not validation_report[
                "passed"
            ]:
                raise SystemExit(
                    1
                )

            return

        # ---------------------------------------------------------------------
        # REAL RAG EVALUATION STARTS HERE
        # ---------------------------------------------------------------------

        manager = RAGIndexManager(chunk_strategy=args.chunk_strategy)

        refresh = (
            manager.refresh()
        )

        retriever = (
            HybridRetriever(
                manager
            )
        )

        semantic_reranker = (
            build_semantic_reranker()
        )

        mode_reports = (
            run_ablation(
                cases,
                retriever,
                top_k=(
                    args.top_k
                ),
                semantic_reranker=(
                    semantic_reranker
                ),
                semantic_query_mode=(
                    args.semantic_query_mode
                ),
            )
        )

        comparison, deltas = (
            build_comparison(
                mode_reports
            )
        )
        slice_summary = build_slice_summary(
            mode_reports
        )

        rank_movement = build_rank_movement_report(
            mode_reports
        )

        baseline = (
            mode_reports[
                BASELINE_MODE
            ]
        )

        splits = sorted(
            {
                str(
                    case.get(
                        "split"
                    )
                )
                for case
                in cases
                if case.get(
                    "split"
                )
            }
        )

        schema_versions = sorted(
            {
                str(
                    case.get(
                        "schema_version",
                        "rag-eval-v1",
                    )
                )
                for case
                in cases
            }
        )

        report = {
            **baseline,

            "side_effects": (
                SIDE_EFFECTS
            ),

            "dataset": str(
                dataset_path
            ),

            "dataset_cases": len(
                cases
            ),

            "dataset_splits": (
                splits
                if splits
                else [
                    "legacy_unspecified"
                ]
            ),

            "schema_versions": (
                schema_versions
            ),

            "dev_set": (
                not splits
                or splits
                == ["dev"]
            ),

            "holdout_set": (
                splits
                == ["holdout"]
            ),

            "baseline_mode": (
                BASELINE_MODE
            ),

            "candidate_k": (
                CANDIDATE_K
            ),

            "kb_version": (
                refresh.kb_version
            ),

            "constraint_input": (
                "rag_context_upstream_semantics"
            ),

            "semantic_query_mode": (
                args.semantic_query_mode
            ),

            "comparison": (
                comparison
            ),

            "deltas": (
                deltas
            ),

            "reproducibility": (
                reproducibility_metadata(
                    manager=(
                        manager
                    ),
                    semantic_reranker=(
                        semantic_reranker
                    ),
                    top_k=(
                        args.top_k
                    ),
                    dataset_path=(
                        dataset_path
                    ),
                    semantic_query_mode=(
                        args.semantic_query_mode
                    ),
                )
            ),

            "ablation": (
                mode_reports
            ),
            "slice_summary": slice_summary,
            "rank_movement": rank_movement,
        }

    except Exception as error:
        report = (
            build_skipped_report(
                reason=(
                    f"{type(error).__name__}: "
                    f"{error}"
                ),
                side_effects=(
                    SIDE_EFFECTS
                ),
                dataset=str(
                    dataset_path
                ),
            )
        )

        report[
            "failed_count"
        ] = 1

        report[
            "execution_error"
        ] = report[
            "skip_reason"
        ]

    report_name = args.experiment_name or f"eval_rag_{args.chunk_strategy}_{args.semantic_query_mode}"
    report["chunk_strategy"] = args.chunk_strategy
    report_path = save_report(report_name, report)

    print_rag_summary(
        report,
        report_path,
    )
    print_slice_summary(
        report
    )

    print_rank_movement_summary(
        report
    )

    if (
        report.get(
            "skipped"
        )
        or report.get(
            "failed_count"
        )
    ):
        raise SystemExit(
            1
        )
def format_metric(
    value: object,
    *,
    digits: int = 4,
) -> str:
    if isinstance(value, bool):
        return str(value)

    if isinstance(value, (int, float)):
        return f"{float(value):.{digits}f}"

    if value is None:
        return NA

    return str(value)
def print_rag_summary(report: dict, report_path: Path) -> None:
    if report.get("holdout_set"):
        title = "RAG Holdout Evaluation"
    elif report.get("dev_set"):
        title = "RAG Dev Evaluation"
    else:
        title = "RAG Evaluation"

    print(f"\n{title}")
    print("=" * 90)

    print(f"Dataset: {report.get('dataset')}")
    print(f"Cases:   {report.get('dataset_cases')}")
    print(f"KB:      {report.get('kb_version')}")
    print()

    print(
        f"{'Mode':<28}"
        f"{'Pass':<10}"
        f"{'Hit@1':<9}"
        f"{'Hit@3':<9}"
        f"{'Hit@5':<9}"
        f"{'MRR':<10}"
        f"{'Recall@20':<12}"
        f"{'Coverage@5':<12}"
    )

    print("-" * 108)

    for mode, mode_report in report.get("ablation", {}).items():
        total = mode_report["total_cases"]
        passed = mode_report["passed_count"]

        print(
            f"{mode:<28}"
            f"{f'{passed}/{total}':<10}"
            f"{format_metric(mode_report.get('hit_at_1')):<9}"
            f"{format_metric(mode_report.get('hit_at_3')):<9}"
            f"{format_metric(mode_report.get('hit_at_5')):<9}"
            f"{format_metric(mode_report.get('mrr')):<10}"
            f"{format_metric(mode_report.get('candidate_evidence_recall_at_20')):<12}"
            f"{format_metric(mode_report.get('evidence_coverage_rate')):<12}"
        )

    print("\nFailure Counts")
    print("-" * 90)

    for mode, mode_report in report.get("ablation", {}).items():
        print(
            f"{mode}: "
            f"{mode_report.get('failure_counts', {})}"
        )

    print("\nDeltas")
    print("-" * 90)

    for name, delta in report.get("deltas", {}).items():
        print(f"{name}: {delta}")

    print(f"\nreport_path: {report_path}")
def format_pass(
    summary: dict,
) -> str:
    return (
        f"{summary['passed_count']}/"
        f"{summary['total_cases']}"
    )


def print_slice_summary(
    report: dict,
) -> None:
    slices = report.get(
        "slice_summary",
        {},
    )

    for field in SLICE_FIELDS:
        field_summary = slices.get(
            field,
            {},
        )

        if not field_summary:
            continue

        print(
            f"\nSlice: {field}"
        )

        print(
            f"{'Value':<28}"
            f"{'A Pass':<10}"
            f"{'B Pass':<10}"
            f"{'C Pass':<10}"
            f"{'D Pass':<10}"
            f"{'A H@5':<10}"
            f"{'C H@5':<10}"
            f"{'D Cov':<10}"
        )

        print(
            "-" * 98
        )

        for value, modes in field_summary.items():
            a = modes[
                HYBRID_MODE
            ]

            b = modes[
                RULE_RERANK_MODE
            ]

            c = modes[
                SEMANTIC_RERANK_MODE
            ]

            d = modes[
                SEMANTIC_CONSTRAINT_MODE
            ]

            print(
                f"{value:<28}"
                f"{format_pass(a):<10}"
                f"{format_pass(b):<10}"
                f"{format_pass(c):<10}"
                f"{format_pass(d):<10}"
                f"{format_metric(a.get('hit_at_5')):<10}"
                f"{format_metric(c.get('hit_at_5')):<10}"
                f"{format_metric(d.get('evidence_coverage_rate')):<10}"
            )
def print_rank_movement_summary(
    report: dict,
) -> None:
    movement_report = report.get(
        "rank_movement",
        {},
    )

    if not movement_report:
        return

    print(
        "\nRank Movement"
    )

    print(
        f"{'Comparison':<14}"
        f"{'Promoted':<11}"
        f"{'Same':<9}"
        f"{'Demoted':<11}"
        f"{'Recovered':<12}"
        f"{'Dropped':<10}"
        f"{'Outside':<10}"
        f"{'NetTop5':<10}"
    )

    print(
        "-" * 87
    )

    for name in (
        "A_to_B",
        "A_to_C",
        "C_to_D",
        "A_to_D",
        "A_to_E",
        "C_to_E",
    ):
        item = movement_report[
            name
        ]

        counts = item[
            "counts"
        ]

        print(
            f"{name:<14}"
            f"{counts['promoted']:<11}"
            f"{counts['unchanged']:<9}"
            f"{counts['demoted']:<11}"
            f"{counts['recovered_to_top5']:<12}"
            f"{counts['dropped_from_top5']:<10}"
            f"{counts['outside_top5_both']:<10}"
            f"{item['net_top5_change']:<10}"
        )
if __name__ == "__main__":
    main()
