"""Cache cleanup must preserve committed results and original business errors."""
import unittest
from unittest.mock import Mock, patch

from app.concurrency import refund_guard


class RefundCacheFailureTests(unittest.TestCase):
    def test_cache_write_failure_is_observable_and_does_not_replace_committed_receipt(self):
        with patch.object(refund_guard, "set_json_cache", side_effect=ConnectionError("offline")), \
                self.assertLogs(refund_guard.logger, "WARNING") as logs:
            refund_guard.cache_refund_idempotency("example", {"refund_id": "committed"})
        self.assertIn("ConnectionError", logs.output[0])

    def test_release_failure_preserves_business_exception_and_clears_local_ownership(self):
        backend = Mock()
        backend.set_if_absent.return_value = True
        backend.compare_and_delete.side_effect = ConnectionError("offline")
        with patch.object(refund_guard, "get_cache_backend", return_value=backend):
            lock = refund_guard.RefundLock("example")
            with self.assertLogs(refund_guard.logger, "WARNING"), self.assertRaisesRegex(ValueError, "business failure"):
                with lock:
                    raise ValueError("business failure")
            self.assertFalse(lock.acquired)
