import unittest
from scripts.eval.negation_aware_answer import forbidden_occurrences


class NegationScoringTests(unittest.TestCase):
    def test_clear_denials_are_distinguished_from_promises(self):
        for text in ('平台完成操作不代表资金一定立即到账。', '不能承诺立即到账。', '无法保证立即到账。'):
            with self.subTest(text=text):
                self.assertTrue(all(o['explicitly_denied'] for o in forbidden_occurrences(text, ['立即到账'])))

    def test_later_promise_is_not_exempted_by_an_earlier_denial(self):
        occurrences = forbidden_occurrences('通常不能立即到账，但本次保证立即到账。', ['立即到账'])
        self.assertEqual([o['explicitly_denied'] for o in occurrences], [True, False])

    def test_double_negation_quotes_and_unrelated_denials_remain_flagged(self):
        for text in ('不是不能立即到账。', '不能不立即到账。', '不能退款，但可以立即到账。',
                     '不要担心，我们保证立即到账。', '他说“不能立即到账”，我保证立即到账。'):
            with self.subTest(text=text):
                self.assertTrue(any(not o['explicitly_denied'] for o in forbidden_occurrences(text, ['立即到账'])))

    def test_negation_never_changes_other_forbidden_phrases(self):
        observations = forbidden_occurrences('不能立即到账，但已经退款。', ['立即到账', '已经退款'])
        self.assertEqual([o['explicitly_denied'] for o in observations], [True, False])

    def test_questions_and_conditional_denials_are_not_definite_statements(self):
        for text in ('不会立即到账吗？', '不能立即到账?', '如果不能立即到账，请退款。'):
            with self.subTest(text=text):
                self.assertFalse(forbidden_occurrences(text, ['立即到账'])[0]['explicitly_denied'])
