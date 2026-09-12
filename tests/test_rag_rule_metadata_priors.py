from copy import deepcopy

from app.rag.query_context import RetrievalQuery
from app.rag.ranking import rank_candidates
from app.rag.reranker import business_rule_score


def test_metadata_prior_ablation_preserves_content_and_state_signals():
    candidate = {'source': '物流规则.md', 'section': '物流异常工单',
                 'text': '订单已发货，可以尝试物流拦截。'}
    original, reasons = business_rule_score('已发货快递', candidate)
    experimental, kept = business_rule_score('已发货快递', candidate, metadata_priors=False)
    assert abs(original - experimental - 0.30) < 1e-9
    assert kept == [r for r in reasons if not r.startswith(('source_match:', 'section_match:'))]
    assert any(r.startswith('order_state_match:') for r in kept)


def test_opt_in_preserves_pool_and_default_and_allows_same_section_chunks():
    query = RetrievalQuery('退款', '退款')
    pool = [
        {'chunk_id': 'a', 'source': '退款政策.md', 'section': '退款申请',
         'text': '退款申请条件', 'score': 0.5},
        {'chunk_id': 'b', 'source': '其他政策.md', 'section': '处理原则',
         'text': '退款申请条件', 'score': 0.7},
        {'chunk_id': 'c', 'source': '其他政策.md', 'section': '处理原则',
         'text': '退款申请条件之外的后续规则', 'score': 0.69},
    ]
    before = deepcopy(pool)
    default = rank_candidates(query, pool, mode='hybrid_rule', top_k=5)
    explicit = rank_candidates(query, pool, mode='hybrid_rule', top_k=5, rule_metadata_priors=True)
    experimental = rank_candidates(query, pool, mode='hybrid_rule', top_k=5, rule_metadata_priors=False)
    assert default == explicit
    assert [x['chunk_id'] for x in default] == ['a', 'b', 'c']
    assert [x['chunk_id'] for x in experimental] == ['b', 'c', 'a']
    assert pool == before
