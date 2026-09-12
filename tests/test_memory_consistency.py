"""Memory correctness against isolated databases and stale independent caches."""
from pathlib import Path
from unittest.mock import patch
import unittest

import test_refund_consistency as fixtures
from app.agent.entry import workflow
from app.agent.routing import memory as module
from app.agent.routing.memory import ConversationMemory, history_cache_key, pending_task_cache_key
from app.observability import tracing
from app.storage import database as db
from app.storage import cache
from app.storage.cache import set_json_cache
from app.storage.transactions import execute, transaction


class MemoryConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RefundConsistencyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        p = patch.object(tracing, 'TRACE_PATH', Path(self.fixture.tmp.name) / 'trace.jsonl')
        p.start()
        self.addCleanup(p.stop)
        self.memory = ConversationMemory()

    def test_failed_reply_write_rolls_back_entire_turn(self):
        write = module.append_message_to_db
        def fail_reply(conversation_id, role, content):
            if role == 'assistant':
                raise RuntimeError('injected second write failure')
            return write(conversation_id, role, content)
        state = workflow.build_initial_state('question', 'atomic-turn', False)
        state['reply'] = 'answer'
        with patch.object(module, 'append_message_to_db', side_effect=fail_reply):
            with self.assertRaises(RuntimeError):
                workflow.persist_result_node(state)
        self.assertEqual(db.load_messages_from_db('atomic-turn', 8), [])

    def test_stale_history_from_another_process_is_not_used(self):
        set_json_cache(history_cache_key('history'), [{'role': 'user', 'content': 'old order'}])
        db.append_message_to_db('history', 'user', 'new order')
        self.assertEqual(self.memory.load('history'), [{'role': 'user', 'content': 'new order'}])

    def test_cleared_pending_cannot_resurrect_from_stale_cache(self):
        task = {'user_request': 'refund', 'slots': {}, 'required_slots': ['order_id']}
        self.memory.set_pending_task('pending', task)
        db.clear_pending_task_in_db('pending')
        set_json_cache(pending_task_cache_key('pending'), task)
        self.assertIsNone(self.memory.get_pending_task('pending'))

    def test_cache_outage_does_not_break_committed_memory(self):
        with patch.object(cache, 'get_json_cache', side_effect=ConnectionError('cache unavailable')), \
             patch.object(cache, 'set_json_cache', side_effect=ConnectionError('cache unavailable')), \
             patch.object(cache, 'delete_cache', side_effect=ConnectionError('cache unavailable')):
            self.memory.append_turn('cache-down', 'question', 'answer')
            self.memory.set_pending_task('cache-down', {'user_request': 'question'})
            self.assertEqual(len(self.memory.load('cache-down')), 2)
            self.assertEqual(self.memory.get_pending_task('cache-down'), {'user_request': 'question'})
            self.memory.clear_pending_task('cache-down')
            self.assertIsNone(self.memory.get_pending_task('cache-down'))

    def test_context_budget_keeps_whole_recent_turns_and_full_history(self):
        memory = ConversationMemory(max_context_chars=20)
        memory.append_turn('budget', 'a' * 10, 'b' * 10)
        memory.append_turn('budget', 'new', 'reply')
        self.assertEqual(memory.load_context('budget'), [
            {'role': 'user', 'content': 'new'}, {'role': 'assistant', 'content': 'reply'}])
        self.assertEqual(len(memory.load('budget')), 4)

    def test_oversized_latest_turn_does_not_resurface_older_order(self):
        memory = ConversationMemory(max_context_chars=20)
        memory.append_turn('oversize', 'old order', 'ok')
        memory.append_turn('oversize', 'new order' * 10, 'reply')
        self.assertEqual(memory.load_context('oversize'), [])

    def test_expired_task_and_history_cannot_reactivate_old_request(self):
        self.memory.append_turn('expired', 'refund order 10009', 'please confirm')
        self.memory.set_pending_task('expired', {'user_request': 'refund', 'required_slots': ['order_id']})
        with transaction():
            execute('UPDATE pending_tasks SET updated_at = ? WHERE conversation_id = ?',
                    ('2000-01-01 00:00:00', 'expired'))
            execute('UPDATE conversation_messages SET created_at = ? WHERE conversation_id = ?',
                    ('2000-01-01 00:00:00', 'expired'))
        self.assertIsNone(self.memory.get_pending_task('expired'))
        self.assertEqual(self.memory.load_context('expired'), [])
        self.assertEqual(len(self.memory.load('expired')), 2)
        self.assertIsNotNone(db.get_pending_task_from_db('expired'))
        self.memory.set_pending_task('expired', {'user_request': 'new request'})
        self.assertEqual(self.memory.get_pending_task('expired'), {'user_request': 'new request'})

    def test_context_budget_does_not_discard_active_structured_task(self):
        memory = ConversationMemory(max_context_chars=20)
        task = {'user_request': 'refund', 'slots': {'order_id': '10009'}}
        memory.set_pending_task('structured', task)
        memory.append_turn('structured', 'x' * 100, 'reply')
        self.assertEqual(memory.load_context('structured'), [])
        self.assertEqual(memory.get_pending_task('structured'), task)


if __name__ == '__main__':
    unittest.main()
