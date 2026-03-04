import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from db.state_store import StateStore


class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "state.db")
        self.store = StateStore(self.db_path)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_monitoring_snapshot_counts_rejections_timeouts_and_memory(self):
        self.store.save_audit(
            account={"total_balance": 1000},
            market={"symbol": "BTCUSDT"},
            intent={"decision": "BUY"},
            risk_ok=False,
            reason="Cooldown active for another 10 minute(s).",
            receipts=[{"status": "timeout_unresolved"}],
            execution_summary={"loop_type": "Trigger"},
        )
        self.store.save_audit(
            account={},
            market={},
            intent={"decision": "HOLD"},
            risk_ok=False,
            reason="Error in Trigger cycle: boom",
            receipts=[],
            execution_summary={"error": "boom"},
        )
        self.store.save_long_term_memory_entries(
            [
                {
                    "rule": "Avoid re-entry during cooldown.",
                    "version": 1,
                    "metrics": {"status": "rule", "support": 4, "regime": "general"},
                }
            ]
        )

        snapshot = self.store.get_monitoring_snapshot()

        self.assertEqual(snapshot["total_audits_24h"], 2)
        self.assertEqual(snapshot["rejected_24h"], 2)
        self.assertEqual(snapshot["loop_errors_24h"], 1)
        self.assertEqual(snapshot["timeouts_unresolved_24h"], 1)
        self.assertEqual(len(snapshot["recent_memory"]), 1)
        self.assertEqual(snapshot["recent_memory"][0]["metrics"]["status"], "rule")

    def test_get_journal_entries_since_returns_new_fields(self):
        self.store.save_journal(
            decision="EXIT",
            rationale=["Closed position"],
            confidence=0.9,
            risk_reason="Approved",
            estimated_pnl_pct=-1.5,
            loss_signal=True,
        )

        rows = self.store.get_journal_entries_since(
            datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "EXIT")
        self.assertEqual(rows[0]["loss_signal"], 1)
        self.assertAlmostEqual(rows[0]["estimated_pnl_pct"], -1.5)

    def test_get_audit_log_and_replay_candidates_decode_json_payloads(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000},
            market={"symbol": "BTCUSDT"},
            intent={"decision": "HOLD", "orders": [], "confidence": 0.2},
            risk_ok=True,
            reason="Safe action.",
            receipts=[{"status": "closed"}],
            decision_request={"policy_summary": "x", "account_state": {}, "market_snapshot": {}},
            execution_summary={"receipt_summary": {"filled_orders": 1}},
        )
        self.store.save_execution_fills(
            audit_id,
            [
                {
                    "timestamp": 1111,
                    "symbol": "BTCUSDT",
                    "order_id": "order-1",
                    "client_order_id": "client-1",
                    "trade_id": "trade-1",
                    "side": "buy",
                    "price": 30000,
                    "amount": 0.1,
                    "cost": 3000,
                    "fee_cost": 2.3,
                    "fee_currency": "USDT",
                    "realized_pnl": 1.7,
                }
            ],
        )

        audit = self.store.get_audit_log(audit_id)
        candidates = self.store.get_replay_candidates(limit=5)
        fill_stats = self.store.get_fill_stats()

        self.assertIsNotNone(audit)
        self.assertEqual(audit["intent"]["decision"], "HOLD")
        self.assertEqual(audit["receipts"][0]["status"], "closed")
        self.assertEqual(audit["execution_summary"]["receipt_summary"]["filled_orders"], 1)
        self.assertEqual(audit["fills"][0]["trade_id"], "trade-1")
        self.assertEqual(candidates[0]["id"], audit_id)
        self.assertEqual(candidates[0]["intent"]["decision"], "HOLD")
        self.assertEqual(fill_stats["fill_count"], 1)
        self.assertEqual(fill_stats["fee_cost"], 2.3)
        self.assertEqual(fill_stats["realized_pnl"], 1.7)

    def test_position_ledger_and_replay_runs_are_queryable(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000},
            market={"symbol": "ETHUSDT"},
            intent={"decision": "EXIT", "orders": [], "confidence": 0.9},
            risk_ok=True,
            reason="Approved",
            receipts=[],
        )
        self.store.save_position_ledger_entries(
            audit_id,
            [
                {
                    "symbol": "ETHUSDT",
                    "decision": "EXIT",
                    "fill_count": 2,
                    "total_amount": 1.5,
                    "gross_realized_pnl": 12.4,
                    "fee_cost": 1.1,
                    "net_realized_pnl": 11.3,
                    "balance_delta": 10.8,
                    "balance_delta_pct": 1.08,
                }
            ],
        )
        self.store.save_replay_run(
            audit_id,
            {
                "comparison": {
                    "original_decision": "EXIT",
                    "replayed_decision": "REDUCE",
                    "decision_changed": True,
                    "order_count_changed": False,
                    "confidence_delta": -0.2,
                }
            },
        )

        ledger_stats = self.store.get_position_ledger_stats()
        recent_ledger = self.store.get_recent_position_ledger(limit=5)
        recent_replays = self.store.get_recent_replay_runs(limit=5)
        audit = self.store.get_audit_log(audit_id)

        self.assertEqual(ledger_stats["row_count"], 1)
        self.assertEqual(ledger_stats["net_realized_pnl"], 11.3)
        self.assertEqual(recent_ledger[0]["symbol"], "ETHUSDT")
        self.assertEqual(recent_replays[0]["audit_log_id"], audit_id)
        self.assertTrue(recent_replays[0]["decision_changed"])
        self.assertEqual(audit["replay_runs"][0]["replayed_decision"], "REDUCE")
        self.assertEqual(audit["ledger_entries"][0]["symbol"], "ETHUSDT")

    def test_symbol_and_daily_reports_aggregate_ledger_rows(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000},
            market={"symbol": "BTCUSDT"},
            intent={"decision": "REDUCE", "orders": [], "confidence": 0.6},
            risk_ok=True,
            reason="Approved",
            receipts=[],
        )
        self.store.save_position_ledger_entries(
            audit_id,
            [
                {
                    "symbol": "BTCUSDT",
                    "decision": "REDUCE",
                    "fill_count": 2,
                    "total_amount": 0.2,
                    "gross_realized_pnl": 15.0,
                    "fee_cost": 1.0,
                    "net_realized_pnl": 14.0,
                    "balance_delta": 13.5,
                    "balance_delta_pct": 1.35,
                },
                {
                    "symbol": "ETHUSDT",
                    "decision": "EXIT",
                    "fill_count": 1,
                    "total_amount": 1.0,
                    "gross_realized_pnl": -4.0,
                    "fee_cost": 0.5,
                    "net_realized_pnl": -4.5,
                    "balance_delta": -4.0,
                    "balance_delta_pct": -0.4,
                },
            ],
        )

        symbol_report = self.store.get_symbol_performance_report(hours=24)
        daily_report = self.store.get_daily_ledger_report(days=7)

        self.assertEqual(len(symbol_report), 2)
        self.assertEqual(symbol_report[0]["symbol"], "BTCUSDT")
        self.assertEqual(symbol_report[0]["net_realized_pnl"], 14.0)
        self.assertEqual(symbol_report[1]["symbol"], "ETHUSDT")
        self.assertEqual(daily_report[0]["row_count"], 2)
        self.assertEqual(daily_report[0]["net_realized_pnl"], 9.5)


if __name__ == "__main__":
    unittest.main()
