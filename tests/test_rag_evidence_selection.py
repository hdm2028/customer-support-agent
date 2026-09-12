from copy import deepcopy
import asyncio

import pytest

from app.rag.evidence_selection import (
    complete_primary_evidence, select_complementary_evidence, _facet_affinity, _coverage,
    _guardrail_masks,
)
from app.rag.query_context import RetrievalQuery
from app.rag.ranking import rank_candidates
from app.rag.retriever import HybridRetriever


def chunk(number, text, source='说明.md'):
    return {'chunk_id': str(number), 'source': source, 'document_id': source,
            'text': text, 'metadata': {'chunk_index': number}}


def test_continuation_uses_real_overlap_and_preserves_head_and_pool():
    head = chunk(1, '此前的说明已经结束。\n最终处理需要根据事实和审核结果决')
    continuation = chunk(2, '最终处理需要根据事实和审核结果决定。')
    pool = [head,chunk(9,'已有条目'),chunk(8,'已有条目'),continuation]
    snapshot = deepcopy(pool)
    selected = complete_primary_evidence(RetrievalQuery('处理','处理'),pool,top_k=3)
    assert selected[0] == head
    assert selected[2]['chunk_id'] == '2'
    assert {c['chunk_id'] for c in selected} == {c['chunk_id'] for c in pool}
    assert pool == snapshot


def test_same_section_or_adjacency_alone_does_not_justify_replacement():
    pool = [chunk(1,'完整句子。'),chunk(9,'其他证据'),chunk(2,'新规则。')]
    assert complete_primary_evidence(RetrievalQuery('规则','规则'),pool,top_k=2) == pool
    pool[0]['text'] = '尚未完成但与后文没有重叠的句子'
    assert complete_primary_evidence(RetrievalQuery('规则','规则'),pool,top_k=2) == pool


def test_completion_does_not_drop_existing_query_evidence_category():
    pool = [chunk(1,'当前结果需要根据真实物流状态判'),chunk(8,'物流规则','物流规则.md'),
            chunk(2,'当前结果需要根据真实物流状态判断。')]
    assert complete_primary_evidence(RetrievalQuery('物流','物流'),pool,top_k=2) == pool


def test_equivalent_heading_body_affinities_are_exact_ties():
    # 10 leading-heading units + 2 body units and 9 covered-heading
    # units + 3 body units must both give 12/30, not adjacent floats.
    a = {'section': '甲乙', 'text': '甲乙 丙丁'}
    b = {'section': '', 'metadata': {'covered_sections': ['甲乙']},
         'text': '甲乙 丙丁 戊己'}
    facet = {'甲乙','丙丁','戊己'}
    assert _facet_affinity(a, facet) == _facet_affinity(b, facet) == 0.4


def test_completion_keeps_guardrail_source_even_when_category_is_redundant():
    head = chunk(1, '投诉处理需要根据事实和人工审核结果决', '历史问题案例.md')
    continuation = chunk(2, '投诉处理需要根据事实和人工审核结果决定。', '历史问题案例.md')
    pool = [head, chunk(8, '投诉处理说明。', '售后FAQ.md'), continuation]
    assert complete_primary_evidence(RetrievalQuery('投诉','投诉'), pool, top_k=2) == pool


def test_guardrail_profile_uses_user_query_not_appended_facts():
    query = RetrievalQuery('我要投诉\n订单状态：已发货', '投诉 已发货',
                           '用户问题：我要投诉\n订单状态：已发货')
    sources,_ = _guardrail_masks(query,[chunk(1,'投诉','售后FAQ.md'),chunk(2,'物流','物流规则.md')])
    assert sources == [True,False]


def _complementary_fixture():
    query = RetrievalQuery('我要投诉', '退款 投诉',
                           '用户问题：我要投诉\n业务主题：退款条件\n关联主题：投诉处理')
    pool = [chunk(1, '应按照《乙政策》核实。处理需要人工审核。', '甲指南.md'),
            chunk(2, '投诉处理说明。', '售后FAQ.md')]
    pool += [chunk(i, '退款条件与投诉处理。', f'说明{i}.md') for i in range(3, 8)]
    pool += [chunk(8, '退款资格应根据人工审核结果确认。', '乙政策.md')]
    for i,c in enumerate(pool):
        c.update(score=1 - i / 100, section='处理规则')
    return query, pool


def test_complementary_selection_uses_references_and_preserves_invariants():
    query, pool = _complementary_fixture()
    before = deepcopy(pool)
    ranked = select_complementary_evidence(query, pool, top_k=5)
    assert ranked[0]['chunk_id'] == pool[0]['chunk_id']
    assert len({c['chunk_id'] for c in ranked}) == len(pool)
    assert any(c['source'] == '乙政策.md' for c in ranked[:5])
    assert any(c['source'] == '售后FAQ.md' for c in ranked[:3])
    assert _coverage(query, pool[:5], keyword_groups=True) <= _coverage(query, ranked[:5], keyword_groups=True)
    assert pool == before


def test_single_topic_does_not_expand_into_unrequested_topics():
    _, pool = _complementary_fixture()
    query = RetrievalQuery('退款条件是什么', '退款条件', '业务主题：退款条件')
    assert select_complementary_evidence(query, pool, top_k=5) == pool


def test_existing_trigger_group_allows_shipping_word_substitution():
    query = RetrievalQuery('快递物流', '快递物流')
    a = [chunk(1, '快递规则', '物流规则.md')]
    b = [chunk(2, '物流规则', '物流规则.md')]
    assert _coverage(query, a, keyword_groups=True) == _coverage(query, b, keyword_groups=True)
    assert _coverage(query, a) != _coverage(query, b)


@pytest.mark.parametrize('mode,selection', [('hybrid','complementary'), ('hybrid_rule','typo')])
def test_unsupported_selection_is_not_silently_ignored(mode, selection):
    query,pool = _complementary_fixture()
    with pytest.raises(ValueError, match='Unsupported evidence selection'):
        rank_candidates(query,pool,mode=mode,top_k=5,evidence_selection=selection)


def test_retriever_and_async_path_forward_opt_in_without_changing_default():
    query,pool = _complementary_fixture()
    retriever = HybridRetriever(index_manager=object())
    retriever.retrieve_candidates = lambda query, *, candidate_k: deepcopy(pool)
    default = retriever.retrieve(query,top_k=5,mode='hybrid_rule',candidate_k=20)
    assert default == rank_candidates(query,pool,mode='hybrid_rule',top_k=5)[:5]
    selected = retriever.retrieve(query,top_k=5,mode='hybrid_rule',candidate_k=20,
                                  evidence_selection='complementary')
    async_selected = asyncio.run(retriever.aretrieve(query,top_k=5,mode='hybrid_rule',candidate_k=20,
                                                   evidence_selection='complementary'))
    assert selected == async_selected
    assert all(c['evidence_selection'] == 'complementary' for c in selected)
    assert not any('evidence_selection' in c for c in default)
