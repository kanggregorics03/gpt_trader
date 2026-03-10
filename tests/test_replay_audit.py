import unittest
import tempfile
from pathlib import Path

from db.state_store import StateStore
from tools.replay_audit import (
    build_hold_baseline_intent,
    build_replay_payload,
    evaluate_hold_opportunity_counterfactual,
    resolve_incremental_replay_limit,
    should_run_retrieval_counterfactual,
    strip_retrieval_context,
)


class TestReplayAudit(unittest.TestCase):
    def setUp(self):
        Path("c:/gpt_trader/.tmp_tests").mkdir(parents=True, exist_ok=True)
        self.tmpdir = tempfile.TemporaryDirectory(dir="c:/gpt_trader/.tmp_tests")
        self.db_path = str(Path(self.tmpdir.name) / "replay.db")
        self.store = StateStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.tmpdir.cleanup()

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

    def test_resolve_incremental_replay_limit_scales_with_candidate_volume(self):
        self.assertEqual(
            resolve_incremental_replay_limit(
                candidate_count=20,
                base_limit=30,
                max_limit=60,
                coverage_target=0.5,
            ),
            20,
        )
        self.assertEqual(
            resolve_incremental_replay_limit(
                candidate_count=80,
                base_limit=30,
                max_limit=60,
                coverage_target=0.5,
            ),
            40,
        )
        self.assertEqual(
            resolve_incremental_replay_limit(
                candidate_count=180,
                base_limit=30,
                max_limit=60,
                coverage_target=0.5,
            ),
            60,
        )

    def test_hold_opportunity_counterfactual_detects_missed_move(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000.0, "available_balance": 1000.0, "positions": [], "open_orders": [], "open_orders_count": 0},
            market={
                "market_snapshots": {
                    "ETHUSDT": {
                        "symbol": "ETHUSDT",
                        "current_price": 100.0,
                        "mark_price": 100.0,
                        "atr_1h": 4.0,
                        "trend_1h": "UP",
                        "volume_ratio_15m": 1.8,
                        "spread_percentile_1h": 40.0,
                        "depth_percentile_1h": 70.0,
                        "distance_to_invalid_level": 1.0,
                        "fast_move_signal_active": True,
                        "volume_surge_signal_active": True,
                        "range_break_signal_active": True,
                        "timeframes": {
                            "15m": {"trend": "UP", "change_3bar_pct": 1.2, "rsi_14": 62.0},
                            "1h": {"trend": "UP", "change_3bar_pct": 2.1, "rsi_14": 58.0},
                        },
                        "order_book": {"mid_price": 100.0, "spread_bps": 3.0},
                    }
                }
            },
            intent={"decision": "HOLD", "orders": [], "rationale": ["No trade."], "confidence": 0.4},
            risk_ok=True,
            reason="Safe action.",
            receipts=[],
            decision_request={"policy_summary": "x", "account_state": {}, "market_snapshots": {}},
        )
        self.store.conn.execute(
            "UPDATE audit_logs SET timestamp = ? WHERE id = ?",
            ("2026-03-08 10:00:00", audit_id),
        )
        future_id = self.store.save_audit(
            account={"total_balance": 1000.0},
            market={
                "market_snapshots": {
                    "ETHUSDT": {
                        "symbol": "ETHUSDT",
                        "current_price": 104.5,
                        "mark_price": 104.4,
                        "order_book": {"mid_price": 104.45, "spread_bps": 4.0},
                    }
                }
            },
            intent={"decision": "HOLD", "orders": [], "confidence": 0.2},
            risk_ok=True,
            reason="Safe action.",
            receipts=[],
        )
        self.store.conn.execute(
            "UPDATE audit_logs SET timestamp = ? WHERE id = ?",
            ("2026-03-08 10:20:00", future_id),
        )
        self.store.conn.commit()

        audit = self.store.get_audit_log(audit_id)
        baseline = build_hold_baseline_intent(audit)
        summary = evaluate_hold_opportunity_counterfactual(self.store, audit)

        self.assertEqual(baseline["decision"], "BUY")
        self.assertEqual(summary["entry_side"], "BUY")
        self.assertTrue(summary["baseline_beats_hold"])
        self.assertTrue(summary["simple_rule_baseline_beats_hold"])
        self.assertGreaterEqual(summary["missed_breakout_count"], 1)
        self.assertGreater(summary["hold_opportunity_cost"], 0.0)
        self.assertTrue(summary["windows"]["30m"]["breakout_hit"])
        self.assertEqual(summary["hold_quality"], "missed_opportunity_hold")
        self.assertEqual(summary["trigger_context"]["signature"], "volume_surge+fast_move+range_break")
        self.assertIn("tight_spread", summary["execution_context"]["signature"])
        self.assertIn("UP", summary["trend_context"]["signature"])
        self.assertGreater(summary["windows"]["30m"]["realized_move_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
