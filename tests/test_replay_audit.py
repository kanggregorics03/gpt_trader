import unittest

from tools.replay_audit import build_replay_payload, strip_retrieval_context


class TestReplayAudit(unittest.TestCase):
    def test_strip_retrieval_context_clears_similar_cases_only(self):
        payload = {
            "policy_summary": "x",
            "similar_past_cases": ["BTCUSDT: case one", "ETHUSDT: case two"],
            "recent_journal_context": [{"journal_id": 1}],
        }

        stripped = strip_retrieval_context(payload)

        self.assertEqual(stripped["similar_past_cases"], [])
        self.assertEqual(stripped["recent_journal_context"], [{"journal_id": 1}])
        self.assertEqual(payload["similar_past_cases"], ["BTCUSDT: case one", "ETHUSDT: case two"])

    def test_build_replay_payload_includes_retrieval_counterfactual(self):
        payload = build_replay_payload(
            audit={"timestamp": "2026-03-07 10:00:00", "intent": {"decision": "BUY"}, "ai_raw_response": "raw"},
            audit_id=101,
            replayed_intent={"decision": "HOLD", "orders": [], "confidence": 0.4},
            replayed_raw_response="new-raw",
            retrieval_counterfactual={
                "with_vs_without_comparison": {
                    "decision_changed": True,
                    "confidence_delta": -0.12,
                }
            },
        )

        self.assertEqual(payload["audit_id"], 101)
        self.assertEqual(payload["retrieval_counterfactual"]["with_vs_without_comparison"]["decision_changed"], True)
        self.assertEqual(payload["replayed_raw_response"], "new-raw")


if __name__ == "__main__":
    unittest.main()
