import json
import unittest
from unittest.mock import patch

from app.agent.routing import llm_router


class AuxiliaryTopicTests(unittest.TestCase):
    def route(self, **changes):
        data = {'intent': 'return_refund', 'action_type': 'query', 'topic': 'refund_eligibility',
                'related_topics': ['return_refund', 'product_failure', 'product_failure'],
                'confidence': .9, 'reason': '询问资格', **changes}
        with patch.dict('os.environ', {'SEMANTIC_RELATED_TOPICS': 'discard_invalid', 'SEMANTIC_ROUTE_REPAIR': ''}), \
             patch.object(llm_router, 'call_zhipu_chat', return_value=json.dumps(data)) as call:
            result = llm_router.infer_semantic_route('只是询问规则')
        self.assertEqual(call.call_count, 1)
        return result

    def test_only_invalid_auxiliary_strings_are_discarded_and_recorded(self):
        result = self.route()
        self.assertEqual((result.intent, result.action_type, result.topic), ('return_refund','query','refund_eligibility'))
        self.assertEqual(result.related_topics, ['product_failure'])
        self.assertEqual(result.ignored_related_topics, ['return_refund'])
        self.assertEqual(result.source, 'llm')

    def test_invalid_primary_is_still_rejected(self):
        for changes in ({'intent': 'invalid'}, {'action_type': 'invalid'}, {'topic': 'invalid'}):
            with self.subTest(changes=changes):
                result = self.route(**changes)
                self.assertEqual(result.source, 'fallback')
                self.assertEqual(result.action_type, 'unknown')

    def test_nonstring_auxiliary_structure_is_not_silently_repaired(self):
        for values in ('refund_policy', [None], [{'topic': 'refund_policy'}]):
            with self.subTest(values=values):
                self.assertEqual(self.route(related_topics=values).source, 'fallback')

    def test_valid_risk_related_topic_is_preserved(self):
        result = self.route(related_topics=['complaint', 'refund_eligibility', 'invalid'])
        self.assertEqual(result.related_topics, ['complaint'])
        self.assertEqual(result.action_type, 'query')

    def test_incomplete_write_or_handoff_semantics_are_not_salvaged(self):
        for action in ('execute', 'handoff'):
            with self.subTest(action=action):
                result = self.route(intent='complaint', action_type=action, topic='complaint')
                self.assertEqual(result.source, 'fallback')
                self.assertEqual(result.action_type, 'unknown')

    def test_valid_write_semantics_remain_unchanged(self):
        result = self.route(action_type='execute', topic='refund_apply', related_topics=['complaint'])
        self.assertEqual(result.source, 'llm')
        self.assertEqual(result.action_type, 'execute')
        self.assertEqual(result.related_topics, ['complaint'])
