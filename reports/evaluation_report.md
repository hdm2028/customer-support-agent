# Customer Support Agent Evaluation

Generated at: 2026-08-28T21:20:40

## Dataset

- routing_cases: 8
- rag_cases: 8
- tool_cases: 5
- answer_cases: 4
- e2e_cases: 12

## Routing

- accuracy: 0.875
- macro_f1: 1.0

## RAG

- hit_at_1: 0.875
- hit_at_3: 0.875
- hit_at_5: 0.875
- mrr: 0.875

## Tool Calling

- selection_accuracy: N/A
- argument_accuracy: N/A
- execution_success_rate: N/A
- dangerous_tool_misuse: N/A

## Answer

- correctness: N/A
- faithfulness: N/A
- hallucination_rate: N/A

## End-to-End

- task_success_rate: N/A
- workflow_success_rate: N/A
- routing_success_rate: N/A
- rag_success_rate: N/A
- tool_success_rate: N/A
- answer_success_rate: N/A

## Risk

- risk_recall: N/A
- false_negative_count: N/A
- false_positive_count: N/A

## Reliability

### refund_idempotency
- status: SKIPPED
- requests: N/A
- refund_records: N/A
- duplicate_refunds: N/A

### refund_concurrency
- status: SKIPPED
- concurrent_requests: N/A
- refund_records: N/A
- duplicate_refunds: N/A

### mq
- status: SKIPPED
- received: N/A
- processed: N/A
- duplicates_ignored: N/A

### high_risk_review
- status: SKIPPED

### tool_failure
- status: SKIPPED

### service_degradation
- status: SKIPPED

## Failed Cases

- routing / routing_refund_003 / ROUTING: route.need_clarification expected=False, actual=True
- rag / rag_refund_006 / RAG: expected_source_miss expected=['退换货政策.md', '物流配送政策.md']; missing_keywords=['物流拦截', '等待商品送达', '拒收']

## Source Reports

- routing: D:\python-project\客服agent\reports\eval_routing.json
- rag: D:\python-project\客服agent\reports\eval_rag.json
- tool_calling: D:\python-project\客服agent\reports\eval_tools.json
- answer: D:\python-project\客服agent\reports\eval_answer.json
- e2e: D:\python-project\客服agent\reports\eval_e2e.json
- refund_idempotency: D:\python-project\客服agent\reports\reliability_refund_idempotency.json
- refund_concurrency: D:\python-project\客服agent\reports\reliability_refund_concurrency.json
- mq_duplicate_delivery: D:\python-project\客服agent\reports\reliability_mq_duplicate_delivery.json
- high_risk_review: D:\python-project\客服agent\reports\reliability_high_risk_review.json
- tool_failure: D:\python-project\客服agent\reports\reliability_tool_failure.json
- service_degradation: D:\python-project\客服agent\reports\reliability_service_degradation.json
