import unittest

from tools.replay_audit import build_replay_payload, compare_intents


class TestReplayAudit(unittest.TestCase):
    def test_compare_intents_detects_decision_and_confidence_changes(self):
        original = {
            "decision": "HOLD",
            "orders": [],
            "confidence": 0.2,
            "rationale": ["Stay flat until volatility expands."],
        }
        replayed = {
            "decision": "BUY",
            "orders": [{"symbol": "BTCUSDT", "action": "BUY", "quantity": 0.5}],
            "confidence": 0.7,
            "rationale": ["Momentum breakout confirmed with stronger volume."],
        }

        comparison = compare_intents(original, replayed)

        self.assertTrue(comparison["decision_changed"])
        self.assertTrue(comparison["order_count_changed"])
        self.assertTrue(comparison["side_changed"])
        self.assertEqual(comparison["original_decision"], "HOLD")
        self.assertEqual(comparison["replayed_decision"], "BUY")
        self.assertEqual(comparison["size_delta"], 0.5)
        self.assertAlmostEqual(comparison["confidence_delta"], 0.5)
        self.assertIn("momentum", comparison["rationale_keywords_added"])
        self.assertIn("flat", comparison["rationale_keywords_removed"])

    def test_build_replay_payload_embeds_comparison(self):
        audit = {
            "timestamp": "2026-03-04 01:00:00",
            "intent": {"decision": "SELL", "orders": []},
            "ai_raw_response": "{\"decision\":\"SELL\"}",
            "risk_reason": "Approved",
        }

        payload = build_replay_payload(
            audit=audit,
            audit_id=7,
            replayed_intent={
                "decision": "HOLD",
                "orders": [],
                "confidence": 0.4,
                "rationale": ["Risk rose while trend weakened."],
            },
            replayed_raw_response="{\"decision\":\"HOLD\"}",
        )

        self.assertEqual(payload["audit_id"], 7)
        self.assertEqual(payload["comparison"]["original_decision"], "SELL")
        self.assertEqual(payload["comparison"]["replayed_decision"], "HOLD")
        self.assertTrue(payload["comparison"]["decision_changed"])
        self.assertIn("weakened", payload["comparison"]["rationale_keywords_added"])


if __name__ == "__main__":
    unittest.main()
