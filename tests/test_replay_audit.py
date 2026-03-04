import unittest

from tools.replay_audit import build_replay_payload, compare_intents


class TestReplayAudit(unittest.TestCase):
    def test_compare_intents_detects_decision_and_confidence_changes(self):
        original = {
            "decision": "HOLD",
            "orders": [],
            "confidence": 0.2,
            "rationale": ["Stay flat until volatility expands."],
            "rationale_items": [
                {
                    "evidence": [
                        {
                            "signal": "atr_pct",
                            "symbol": "BTCUSDT",
                            "timeframe": "15m",
                            "source": "market_snapshots.BTCUSDT.timeframes.15m.atr_pct",
                            "observed_value": 0.9,
                        },
                    ]
                }
            ],
        }
        replayed = {
            "decision": "BUY",
            "orders": [{"symbol": "BTCUSDT", "action": "BUY", "quantity": 0.5}],
            "confidence": 0.7,
            "rationale": ["Momentum breakout confirmed with stronger volume."],
            "rationale_items": [
                {
                    "evidence": [
                        {
                            "signal": "volume_ratio",
                            "symbol": "BTCUSDT",
                            "timeframe": "15m",
                            "source": "market_snapshots.BTCUSDT.timeframes.15m.volume_ratio",
                            "observed_value": 2.2,
                        },
                        {
                            "signal": "rsi_14",
                            "symbol": "BTCUSDT",
                            "timeframe": "15m",
                            "source": "market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                            "observed_value": 65.0,
                        },
                    ]
                }
            ],
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
        self.assertIn("BTCUSDT:volume_ratio@15m", comparison["evidence_signals_added"])
        self.assertIn("BTCUSDT:atr_pct@15m", comparison["evidence_signals_removed"])
        self.assertEqual(comparison["evidence_signal_count_delta"], 1)
        self.assertEqual(comparison["evidence_source_diffs"][0]["source"], "market_snapshots.BTCUSDT.timeframes.15m.atr_pct")

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
                "rationale_items": [
                    {
                        "evidence": [
                            {
                                "signal": "spread_bps",
                                "symbol": "BTCUSDT",
                                "timeframe": "microstructure",
                                "source": "market_snapshots.BTCUSDT.order_book.spread_bps",
                                "observed_value": 12.0,
                            },
                        ]
                    }
                ],
            },
            replayed_raw_response="{\"decision\":\"HOLD\"}",
        )

        self.assertEqual(payload["audit_id"], 7)
        self.assertEqual(payload["comparison"]["original_decision"], "SELL")
        self.assertEqual(payload["comparison"]["replayed_decision"], "HOLD")
        self.assertTrue(payload["comparison"]["decision_changed"])
        self.assertIn("weakened", payload["comparison"]["rationale_keywords_added"])
        self.assertIn("BTCUSDT:spread_bps@microstructure", payload["comparison"]["evidence_signals_added"])
        self.assertEqual(payload["comparison"]["evidence_source_diffs"][0]["source"], "market_snapshots.BTCUSDT.order_book.spread_bps")


if __name__ == "__main__":
    unittest.main()
