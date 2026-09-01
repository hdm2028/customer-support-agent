from dataclasses import dataclass

from app.rag.query_context import RAGQueryContext, RetrievalQuery
from app.rag.reranker import RULE_RERANKER_VERSION, rerank_documents
from app.rag.semantic_reranker import SemanticReranker, build_semantic_reranker


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

BUSINESS_CONSTRAINT_VERSION = "business-evidence-constraint-v1"
ABLATION_RUNNER_VERSION = "rag-ablation-v1"


@dataclass(frozen=True)
class EvidenceConstraint:
    """Evidence categories derived only from upstream routing semantics."""

    required_categories: tuple[str, ...] = ()
    category_reasons: tuple[tuple[str, str], ...] = ()

    def reason_for(self, category: str) -> str:
        return next(
            (
                reason
                for required_category, reason in self.category_reasons
                if required_category == category
            ),
            "required by upstream query semantics",
        )


SEMANTIC_EVIDENCE_CATEGORIES = {
    "address_change": "order_change",
    "address_change_apply": "order_change",
    "address_change_policy": "order_change",
    "cancel_apply": "order_change",
    "cancel_order": "order_change",
    "cancel_policy": "order_change",
    "complaint": "complaint",
    "duplicate_charge": "payment_invoice",
    "escalation": "complaint",
    "invoice_apply": "payment_invoice",
    "invoice_change": "payment_invoice",
    "invoice_policy": "payment_invoice",
    "lost_package": "shipping",
    "membership": "membership",
    "membership_benefit": "membership",
    "membership_policy": "membership",
    "payment_failed": "payment_invoice",
    "payment_invoice": "payment_invoice",
    "payment_status": "payment_invoice",
    "product_failure": "product_after_sales",
    "refund_apply": "refund",
    "refund_eligibility": "refund",
    "refund_policy": "refund",
    "refund_timing": "refund",
    "repair_apply": "product_after_sales",
    "replacement": "product_after_sales",
    "return_apply": "refund",
    "return_refund": "refund",
    "shipping_delay": "shipping",
    "shipping_exception": "shipping",
    "shipping_policy": "shipping",
    "shipping_status": "shipping",
    "warranty_policy": "product_after_sales",
    "warranty_repair": "product_after_sales",
}


def build_evidence_constraint(context: RAGQueryContext) -> EvidenceConstraint:
    semantic_fields = (
        ("primary_intent", context.primary_intent),
        ("topic", context.topic),
        *(("related_topics", topic) for topic in context.related_topics),
    )
    categories: list[str] = []
    reasons: list[tuple[str, str]] = []

    for field_name, semantic_value in semantic_fields:
        category = SEMANTIC_EVIDENCE_CATEGORIES.get(semantic_value or "")
        if not category or category in categories:
            continue
        categories.append(category)
        reasons.append((category, f"{field_name}={semantic_value}"))

    return EvidenceConstraint(
        required_categories=tuple(categories),
        category_reasons=tuple(reasons),
    )


def candidate_evidence_categories(candidate: dict) -> tuple[str, ...]:
    """Classify candidate evidence without inspecting the user query."""

    metadata = candidate.get("metadata") or {}
    knowledge_category = str(metadata.get("knowledge_category", ""))
    business_domain = str(metadata.get("business_domain", ""))
    source = str(candidate.get("source", ""))
    categories: list[str] = []

    def add(category: str) -> None:
        if category not in categories:
            categories.append(category)

    if knowledge_category == "refund_policy" or business_domain == "after_sales":
        add("refund")
    if knowledge_category == "logistics_policy" or business_domain == "fulfillment":
        add("shipping")
    if knowledge_category == "product_manual" or business_domain == "product_support":
        add("product_after_sales")
    if "商品售后规则" in source:
        add("product_after_sales")
    if any(value in source for value in ("退款", "退换货")):
        add("refund")
    if any(value in source for value in ("物流", "配送")):
        add("shipping")
    if any(value in source for value in ("商品说明", "保修")):
        add("product_after_sales")
    if "订单取消与修改" in source:
        add("order_change")
    if "支付与发票" in source:
        add("payment_invoice")
    if "会员权益" in source:
        add("membership")
    if any(value in source for value in ("客服SOP", "售后FAQ", "历史问题案例")):
        add("complaint")

    return tuple(categories)


def evaluate_evidence_constraint(
    candidates: list[dict],
    constraint: EvidenceConstraint | None,
) -> dict:
    required = list(constraint.required_categories) if constraint else []
    covered = {
        category
        for candidate in candidates
        for category in candidate_evidence_categories(candidate)
    }
    matched = [category for category in required if category in covered]
    missing = [category for category in required if category not in covered]
    coverage = len(matched) / len(required) if required else 1.0

    return {
        "required_categories": required,
        "matched_categories": matched,
        "missing_categories": missing,
        "evidence_coverage_rate": round(coverage, 4),
        "required_evidence_coverage": round(coverage, 4),
        "constraint_satisfied": not missing,
    }


def _retrieval_ranked(candidates: list[dict]) -> list[dict]:
    ranked = []
    for rank, candidate in enumerate(candidates, start=1):
        retrieval_score = float(
            candidate.get("retrieval_score", candidate.get("score", 0)) or 0
        )
        ranked.append(
            {
                **candidate,
                "retrieval_rank": int(candidate.get("retrieval_rank", rank)),
                "retrieval_score": round(retrieval_score, 6),
            }
        )
    return ranked


def _final_ranked(candidates: list[dict]) -> list[dict]:
    return [
        {**candidate, "final_rank": rank}
        for rank, candidate in enumerate(candidates, start=1)
    ]


def _rule_ranked(query: RetrievalQuery, candidates: list[dict]) -> list[dict]:
    reranked = rerank_documents(query.semantic_query, candidates)
    return [
        {
            **candidate,
            "rule_score": candidate.get("rerank_score"),
            "rule_boost": candidate.get("rerank_bonus"),
            "rule_reason": list(candidate.get("rerank_reasons", [])),
            "rule_reranker_version": RULE_RERANKER_VERSION,
        }
        for candidate in reranked
    ]


def _replaceable_top_index(
    top: list[dict],
    required_categories: tuple[str, ...],
) -> int:
    required = set(required_categories)
    for index in range(len(top) - 1, -1, -1):
        remaining = top[:index] + top[index + 1 :]
        covered = {
            category
            for candidate in remaining
            for category in candidate_evidence_categories(candidate)
        }
        uniquely_required = (
            set(candidate_evidence_categories(top[index])) & required
        ) - covered
        if not uniquely_required:
            return index
    return len(top) - 1


def apply_business_evidence_constraint(
    semantic_candidates: list[dict],
    constraint: EvidenceConstraint | None,
    *,
    top_k: int,
) -> list[dict]:
    """Minimally insert missing evidence from the existing semantic Top20."""

    if (
        constraint is None
        or not constraint.required_categories
        or top_k <= 0
        or not semantic_candidates
    ):
        return [dict(candidate) for candidate in semantic_candidates]

    limit = min(top_k, len(semantic_candidates))
    top = [dict(candidate) for candidate in semantic_candidates[:limit]]
    remainder = [dict(candidate) for candidate in semantic_candidates[limit:]]
    missing_categories = evaluate_evidence_constraint(top, constraint)[
        "missing_categories"
    ]

    for category in missing_categories:
        if evaluate_evidence_constraint(top, constraint)["constraint_satisfied"]:
            break
        if category not in evaluate_evidence_constraint(top, constraint)[
            "missing_categories"
        ]:
            continue
        candidate_index = next(
            (
                index
                for index, candidate in enumerate(remainder)
                if category in candidate_evidence_categories(candidate)
            ),
            None,
        )
        if candidate_index is None:
            continue

        replace_index = _replaceable_top_index(
            top,
            constraint.required_categories,
        )
        inserted = remainder.pop(candidate_index)
        displaced = top[replace_index]
        inserted["constraint_adjusted"] = True
        inserted["constraint_reason"] = (
            f"inserted {category}: {constraint.reason_for(category)}"
        )
        top[replace_index] = inserted
        remainder.append(displaced)

    remainder.sort(key=lambda item: int(item.get("semantic_rank", 10**9)))
    return top + remainder


def build_ablation_rankings(
    query: RetrievalQuery,
    candidates: list[dict],
    *,
    semantic_reranker: SemanticReranker,
    top_k: int,
    evidence_constraint: EvidenceConstraint | None = None,
) -> dict[str, list[dict]]:
    """Build A/B/C/D from one fixed candidate pool and one semantic pass."""

    retrieval = _retrieval_ranked(candidates)
    hybrid = _final_ranked([dict(candidate) for candidate in retrieval])
    rule = _final_ranked(_rule_ranked(query, retrieval))
    semantic = _final_ranked(
        semantic_reranker.rerank(query.semantic_query, retrieval)
    )
    constrained = _final_ranked(
        apply_business_evidence_constraint(
            semantic,
            evidence_constraint,
            top_k=top_k,
        )
    )
    return {
        HYBRID_MODE: hybrid,
        RULE_RERANK_MODE: rule,
        SEMANTIC_RERANK_MODE: semantic,
        SEMANTIC_CONSTRAINT_MODE: constrained,
    }


def rank_candidates(
    query: RetrievalQuery,
    candidates: list[dict],
    *,
    mode: str,
    top_k: int,
    evidence_constraint: EvidenceConstraint | None = None,
    semantic_reranker: SemanticReranker | None = None,
) -> list[dict]:
    retrieval = _retrieval_ranked(candidates)

    if mode == HYBRID_MODE:
        return _final_ranked(retrieval)

    if mode == RULE_RERANK_MODE:
        return _final_ranked(_rule_ranked(query, retrieval))

    if mode not in {SEMANTIC_RERANK_MODE, SEMANTIC_CONSTRAINT_MODE}:
        raise ValueError(f"Unsupported RAG ranking mode: {mode}")

    reranker = semantic_reranker or build_semantic_reranker()
    semantic = _final_ranked(
        reranker.rerank(query.semantic_query, retrieval)
    )
    if mode == SEMANTIC_RERANK_MODE:
        return semantic

    return _final_ranked(
        apply_business_evidence_constraint(
            semantic,
            evidence_constraint,
            top_k=top_k,
        )
    )
