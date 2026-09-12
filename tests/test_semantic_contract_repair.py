import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from app.agent.routing import llm_router


def response(**changes):
    return json.dumps({"intent": "return_refund", "action_type": "query", "topic": "refund_policy",
                       "related_topics": [], "confidence": .9, "reason": "询问一般规则", **changes})


class SemanticContractRepairTests(unittest.TestCase):
    def test_repair_is_opt_in_and_valid_responses_are_unchanged(self):
        for mode in ("", "once"):
            with patch.dict("os.environ", {"SEMANTIC_ROUTE_REPAIR": mode}), \
                    patch.object(llm_router, "call_zhipu_chat", return_value=response()) as call:
                route = llm_router.infer_semantic_route("了解退款规则")
            self.assertEqual(route.topic, "refund_policy")
            self.assertEqual(route.source, "llm")
            self.assertEqual(call.call_count, 1)
        with patch.dict("os.environ", {"SEMANTIC_ROUTE_REPAIR": ""}), \
                patch.object(llm_router, "call_zhipu_chat", return_value=response(related_topics=["invalid"])) as call:
            self.assertEqual(llm_router.infer_semantic_route("了解退款规则").source, "fallback")
            self.assertEqual(call.call_count, 1)

    def test_only_one_contract_repair_is_requested_with_original_query(self):
        invalid = response(related_topics=["undefined_topic"])
        with patch.dict("os.environ", {"SEMANTIC_ROUTE_REPAIR": "once"}), \
                patch.object(llm_router, "call_zhipu_chat", side_effect=[invalid, response()]) as call:
            route = llm_router.infer_semantic_route("了解退款规则")
        self.assertEqual(route.source, "llm")
        self.assertEqual(call.call_count, 2)
        messages = call.call_args.args[0]
        self.assertEqual(messages[1]["content"], "了解退款规则")
        self.assertEqual(messages[2]["content"], invalid)
        self.assertIn("Invalid related topic", messages[3]["content"])
        self.assertFalse(any("expected" in m["content"] for m in messages))

    def test_invalid_repair_falls_back_without_a_third_call(self):
        with patch.dict("os.environ", {"SEMANTIC_ROUTE_REPAIR": "once"}), \
                patch.object(llm_router, "call_zhipu_chat", side_effect=["not json", response(intent="invalid")]) as call:
            route = llm_router.infer_semantic_route("了解退款规则")
        self.assertEqual(route.source, "fallback")
        self.assertEqual(route.action_type, "unknown")
        self.assertEqual(call.call_count, 2)

    def test_network_error_is_not_retried_as_contract_repair(self):
        with patch.dict("os.environ", {"SEMANTIC_ROUTE_REPAIR": "once"}), \
                patch.object(llm_router, "call_zhipu_chat", side_effect=URLError("offline")) as call:
            route = llm_router.infer_semantic_route("了解退款规则")
        self.assertEqual(route.source, "fallback")
        self.assertEqual(call.call_count, 1)

    def test_repair_cannot_escalate_a_valid_query_or_change_valid_primary_semantics(self):
        for changed in [response(action_type="execute"), response(topic="refund_eligibility"), response(intent="cancel_order")]:
            with patch.dict("os.environ", {"SEMANTIC_ROUTE_REPAIR": "once"}), \
                    patch.object(llm_router, "call_zhipu_chat", side_effect=[response(related_topics=["invalid"]), changed]) as call:
                result = llm_router.infer_semantic_route("只是咨询退款规则")
            self.assertEqual(result.source, "fallback")
            self.assertIn("changed valid primary field", result.reason)
            self.assertEqual(call.call_count, 2)

    def test_valid_semantic_misclassification_is_not_rewritten_by_repair(self):
        valid = response(intent="cancel_order", action_type="execute", topic="cancel_apply")
        with patch.dict("os.environ", {"SEMANTIC_ROUTE_REPAIR": "once"}), \
                patch.object(llm_router, "call_zhipu_chat", return_value=valid) as call:
            result = llm_router.infer_semantic_route("了解退款规则")
        self.assertEqual(result.intent, "cancel_order")
        self.assertEqual(call.call_count, 1)

    def test_replay_input_mismatch_remains_fatal_even_if_router_catches_it(self):
        from scripts.eval.semantic_response_replay import install
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path / "observed.jsonl").write_text(json.dumps({"messages": [{"role": "system", "content": llm_router.SYSTEM_PROMPT},
                {"role": "user", "content": "original input"}], "response": response(), "error": None}) + "\n", encoding="utf-8")
            with patch.object(llm_router, "call_zhipu_chat"), patch.dict("os.environ", {"SEMANTIC_ROUTE_REPAIR": "once"}):
                verify = install(path / "responses.jsonl", path / "observed.jsonl")
                self.assertEqual(llm_router.infer_semantic_route("different input").source, "fallback")
                with self.assertRaises(RuntimeError):
                    verify()
