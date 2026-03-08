import unittest

from tools.replay_audit import (
    build_replay_payload,
    should_run_retrieval_counterfactual,
    strip_retrieval_context,
)


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
            audit={
                "timestamp": "2026-03-07 10:00:00",
                "intent": {"decision": "BUY"},
                "ai_raw_response": "raw",
                "market": {
                    "market_snapshots": {
                        "BTCUSDT": {
                            "spread_percentile_1h": 40.0,
                            "depth_percentile_1h": 70.0,
                        }
                    }
                },
            },
            audit_id=101,
            replayed_intent={
                "decision": "BUY",
                "orders": [
                    {
                        "symbol": "BTCUSDT",
                        "action": "BUY",
                        "order_type": "MARKET",
                        "quantity": 0.1,
                        "execution_profile": "market_urgent",
                    }
                ],
                "confidence": 0.4,
            },
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
        self.assertGreater(payload["simulated_fill_ratio"], 0.0)
        self.assertGreater(payload["simulated_slippage_bps"], 0.0)

    def test_should_run_retrieval_counterfactual_only_for_material_cases(self):
        self.assertTrue(
            should_run_retrieval_counterfactual(
                audit={"intent": {"decision": "BUY"}, "risk_approved": True},
                request_payload={"similar_past_cases": ["case-1"]},
            )
        )
        self.assertFalse(
            should_run_retrieval_counterfactual(
                audit={"intent": {"decision": "HOLD"}, "risk_approved": True},
                request_payload={"similar_past_cases": ["case-1"]},
            )
        )
        self.assertFalse(
            should_run_retrieval_counterfactual(
                audit={"intent": {"decision": "BUY"}, "risk_approved": True},
                request_payload={"similar_past_cases": []},
            )
        )


if __name__ == "__main__":
    unittest.main()
