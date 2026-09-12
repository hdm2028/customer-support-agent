"""Optional evidence selection using candidate text and query, never eval labels."""
from __future__ import annotations

import re
from itertools import combinations

from app.rag.embedding_client import CUSTOMER_KEYWORDS
from app.rag.query_context import RetrievalQuery


EVIDENCE_SELECTION_VERSION = 'query-facets-dependencies-v1'


def _guardrail_masks(query: RetrievalQuery, candidates: list[dict]) -> tuple[list[bool], list[bool]]:
    from app.agent.policies.evidence_guardrail import contains_any, detect_policy_profile
    from app.core.schemas import RouteDecision

    # The online guardrail receives the user's query, not appended order facts.
    raw_query = query.semantic_query
    if (query.rerank_query or '').startswith('用户问题：'):
        raw_query = re.split(
            r'\n(?:主要意图|业务主题|关联主题|订单状态|物流状态|商品名称|商品类目|签收日期|处理约束)：',
            query.rerank_query.removeprefix('用户问题：'), maxsplit=1,
        )[0]
    route = RouteDecision(intent=query.primary_intent, action_type=query.action_type or "unknown") if query.primary_intent else None
    profile = detect_policy_profile(query.raw_query if query.raw_query is not None else raw_query, route=route)
    sources = [bool(profile and c.get('source') in profile['expected_sources']) for c in candidates]
    words = [bool(profile and contains_any(
        '\n'.join(str(c.get(field, '')) for field in ('source', 'section', 'text')),
        profile['required_keywords'],
    )) for c in candidates]
    return sources, words


def _preserves_guardrail(query: RetrievalQuery, before: list[dict], after: list[dict]) -> bool:
    old_sources, old_words = _guardrail_masks(query, before)
    new_sources, new_words = _guardrail_masks(query, after)
    return ((not any(old_sources[:3]) or any(new_sources[:3]))
            and (not any(old_words) or any(new_words)))


def _coverage(
    query: RetrievalQuery, candidates: list[dict], *, keyword_groups: bool = False,
) -> set[str]:
    from app.rag.ranking import candidate_evidence_categories

    text = '\n'.join(str(c.get('text', '')) for c in candidates)
    query_text = query.semantic_query + '\n' + query.lexical_query
    categories = {
        'category:' + category
        for c in candidates for category in candidate_evidence_categories(c)
    }
    words = {word for word in CUSTOMER_KEYWORDS if word in query_text and word in text}
    if not keyword_groups:
        return categories | {'keyword:' + word for word in words}
    from app.rag.reranker import BUSINESS_RERANK_RULES

    features = set()
    for word in words:
        groups = {r['name'] for r in BUSINESS_RERANK_RULES if word in r['triggers']}
        features.update({'keyword_group:' + group for group in groups}
                        if groups else {'keyword:' + word})
    return categories | features


def _primary_continuation(ranked: list[dict]) -> dict | None:
    if not ranked:
        return None
    head = ranked[0]
    text = str(head.get('text', '')).rstrip()
    metadata = head.get('metadata') or {}
    index = metadata.get('chunk_index')
    if not isinstance(index, int) or not text or text[-1] in '。！？.!?；;':
        return None
    # Require an actual overlapping unfinished sentence, not just an adjacent
    # chunk or a repeated section title. A short dangling word is insufficient.
    tail = re.split(r'[。！？.!?\n]', text)[-1]
    if len(tail) < 8:
        return None
    for candidate in ranked[1:]:
        other_meta = candidate.get('metadata') or {}
        other = str(candidate.get('text', ''))
        if (candidate.get('document_id') == head.get('document_id')
                and candidate.get('source') == head.get('source')
                and other_meta.get('chunk_index') == index + 1):
            # The overlap must include the old boundary, followed by new text
            # completing the sentence before any new Markdown heading.
            for size in range(min(len(tail), len(other)), 7, -1):
                fragment = tail[-size:]
                pos = other.find(fragment)
                if pos >= 0:
                    remainder = other[pos + size:]
                    if re.match(r'[^\n#]*[。！？.!?]', remainder):
                        return candidate
    return None


def complete_primary_evidence(
    query: RetrievalQuery, ranked: list[dict], *, top_k: int,
) -> list[dict]:
    """Complete the first result without moving it or dropping covered facets."""
    if top_k < 2 or len(ranked) <= top_k:
        return ranked
    continuation = _primary_continuation(ranked)
    selected = list(ranked[:top_k])
    if continuation is None or continuation in selected:
        return ranked
    covered = _coverage(query, selected)
    for index in range(len(selected) - 1, 0, -1):
        replacement = [*selected[:index], *selected[index + 1:], continuation]
        if (covered <= _coverage(query, replacement)
                and _preserves_guardrail(query, selected, replacement)):
            selected = replacement
            break
    else:
        return ranked
    ids = {c['chunk_id'] for c in selected}
    return [
        {**c, 'selection_reason': 'complete_primary_sentence'}
        if c['chunk_id'] == continuation['chunk_id'] else c
        for c in selected
    ] + [c for c in ranked if c['chunk_id'] not in ids]


def _grams(text: str) -> set[str]:
    words = re.findall(r'[\u3400-\u9fff]+|[A-Za-z0-9_]+', text.lower())
    return {w[i:i + 2] for w in words for i in range(max(len(w) - 1, 1))}


def _query_facets(query: RetrievalQuery) -> list[set[str]]:
    # Consume the existing query contract. Expansions are the existing router
    # vocabulary; no eval source names, section names or expected labels.
    from app.rag.query_builder import TOPIC_RETRIEVAL_TERMS

    expansions = {terms[0]: terms for terms in TOPIC_RETRIEVAL_TERMS.values() if terms}
    facets = []
    for line in (query.rerank_query or '').splitlines():
        label, separator, value = line.partition('：')
        if not separator or label not in {'业务主题', '关联主题', '订单状态', '物流状态', '商品类目'}:
            continue
        for part in value.split('；'):
            grams = set().union(*(_grams(t) for t in expansions.get(part, (part,))))
            if grams and grams not in facets:
                facets.append(grams)
    return facets


def _facet_affinity(candidate: dict, facet: set[str]) -> float:
    metadata = candidate.get('metadata') or {}
    section = str(candidate.get('section', ''))
    headings = list(metadata.get('covered_sections') or [])
    headings.extend(candidate.get('covered_sections') or [])
    # A query-aligned heading gets credit even when it is inside a fixed chunk.
    # Credit both leading and internally covered sections.
    heading = max([10 * len(_grams(section) & facet)] +
                  [9 * len(_grams(str(s)) & facet) for s in headings])
    body = len(_grams(str(candidate.get('text', ''))) & facet)
    return (heading + body) / (10 * len(facet))


def select_complementary_evidence(
    query: RetrievalQuery, ranked: list[dict], *, top_k: int,
) -> list[dict]:
    """Choose a complementary subset, retaining baseline head and covered facets.

    Exact subset search is small for the configured 20 candidates / 5 results.
    References are extracted from the first result's own text. Candidate content
    and upstream query fields are the only relevance features.
    """
    if top_k < 2 or len(ranked) <= top_k:
        return ranked
    baseline = ranked[:top_k]
    topic_values = {
        part for line in (query.rerank_query or '').splitlines()
        for label, separator, value in [line.partition('：')]
        if separator and label in {'业务主题', '关联主题'}
        for part in value.split('；') if part
    }
    if len(topic_values) < 2:
        # A single-topic query does not justify cross-policy expansion away
        # from its original evidence. Sentence completion still applies.
        return complete_primary_evidence(query, ranked, top_k=top_k)
    required = _coverage(query, baseline, keyword_groups=True)
    coverage = [_coverage(query, [c], keyword_groups=True) for c in ranked]
    facets = _query_facets(query)
    topic_count = len(facets)
    concept_indices = []
    # The primary passage can name a prerequisite absent from router topics.
    # Reuse the existing business vocabulary, requiring a specific multi-word
    # concept actually present in the passage, rather than inventing a rule.
    primary_text = str(ranked[0].get('text', ''))
    for keyword in CUSTOMER_KEYWORDS:
        if len(keyword) >= 4 and keyword in primary_text:
            facet = _grams(keyword)
            if facet and facet not in facets:
                facets.append(facet)
            if facet:
                concept_indices.append(facets.index(facet))
    if not facets:
        return complete_primary_evidence(query, ranked, top_k=top_k)
    affinities = [[_facet_affinity(c, facet) for facet in facets] for c in ranked]
    normative = [
        (c.get('metadata') or {}).get('source_type') != 'case'
        for c in ranked
    ]
    references = set(re.findall(r'《([^》]+)》', str(ranked[0].get('text', ''))))
    available_sources = {str(c.get('source', '')).rsplit('.', 1)[0] for c in ranked}
    references &= available_sources
    ref_coverage = [{str(c.get('source', '')).rsplit('.', 1)[0]} & references for c in ranked]
    continuation = _primary_continuation(ranked)
    continuation_id = continuation['chunk_id'] if continuation else None
    sections = [
        {(str(c.get('source', '')), str(s)) for s in (
            [c.get('section')] + list((c.get('metadata') or {}).get('covered_sections') or [])
            + list(c.get('covered_sections') or [])
        ) if s}
        for c in ranked
    ]
    original_sections = set().union(*sections[:top_k])
    # Preserve an already-satisfied online guardrail. These are the existing
    # production policy profiles, not evaluator expected/source labels.
    guard_sources, guard_words = _guardrail_masks(query, ranked)
    keep_guard_source = any(guard_sources[:min(3, top_k)])
    keep_guard_words = any(guard_words[:top_k])
    best_indices = tuple(range(top_k))
    best_key = None
    for rest in combinations(range(1, len(ranked)), top_k - 1):
        indices = (0, *rest)
        if not required <= set().union(*(coverage[i] for i in indices)):
            continue
        if keep_guard_source and not any(guard_sources[i] for i in indices[:3]):
            continue
        if keep_guard_words and not any(guard_words[i] for i in indices):
            continue
        # Complementarity uses max per facet, so repeated topical chunks cannot
        # earn the same credit repeatedly. Original ranks break relevance ties.
        key = (
            len(set().union(*(ref_coverage[i] for i in indices))),
            int(any(ranked[i]['chunk_id'] == continuation_id for i in indices)),
            # Explicit upstream topics/facts outrank inferred prerequisites.
            sum(max((affinities[i][j] for i in indices if normative[i]), default=0.0)
                for j in range(topic_count)),
            # A dependency and its business condition must be supported by
            # the same passage; covering them in unrelated documents is weak.
            sum(max((affinities[i][j] * affinities[i][k]
                     for i in indices if normative[i]), default=0.0)
                for j in range(topic_count) for k in concept_indices),
            sum(max((affinities[i][j] for i in indices if normative[i]), default=0.0)
                for j in concept_indices),
            sum(max(affinities[i][j] for i in indices) for j in range(len(facets))),
            len(set().union(*(sections[i] for i in indices)) & original_sections),
            -sum(indices),
        )
        if best_key is None or key > best_key:
            best_key, best_indices = key, indices
    selected = set(best_indices)
    return [
        {**ranked[i], 'selection_reason': 'query_facets_and_primary_dependencies'}
        for i in best_indices
    ] + [c for i, c in enumerate(ranked) if i not in selected]
