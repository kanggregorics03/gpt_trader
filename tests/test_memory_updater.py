import unittest

from batch.memory_updater import MemoryLesson, MemoryUpdater


class TestMemoryUpdater(unittest.TestCase):
    def setUp(self):
        self.updater = MemoryUpdater.__new__(MemoryUpdater)

    def test_build_stats_package_aggregates_pnl_and_reasons(self):
        class DummyStore:
            def get_position_ledger_stats(self, hours):
                return {"row_count": 2, "net_realized_pnl": 4.5, "gross_realized_pnl": 6.0, "fee_cost": 1.5}

            def get_symbol_performance_report(self, hours):
                return [{"symbol": "BTCUSDT", "net_realized_pnl": 4.5, "win_rate": 50.0}]

            def get_daily_ledger_report(self, days):
                return [{"day": "2026-03-04", "net_realized_pnl": 4.5, "row_count": 2}]

        self.updater.state_store = DummyStore()
        journals = [
            {
                "id": 1,
                "decision": "EXIT",
                "rationale": "Stopped out",
                "risk_reason": "Approved",
                "estimated_pnl_pct": -1.2,
                "loss_signal": 1,
            },
            {
                "id": 2,
                "decision": "REDUCE",
                "rationale": "Scaled out",
                "risk_reason": "Approved",
                "estimated_pnl_pct": 0.8,
                "loss_signal": 0,
            },
            {
                "id": 3,
                "decision": "BUY",
                "rationale": "Rejected setup",
                "risk_reason": "Cooldown active for another 15 minute(s).",
                "estimated_pnl_pct": None,
                "loss_signal": 0,
            },
        ]

        stats = self.updater._build_stats_package(journals, window_hours=24)

        self.assertEqual(stats["journal_count"], 3)
        self.assertEqual(stats["evaluated_trade_count"], 2)
        self.assertEqual(stats["win_count"], 1)
        self.assertEqual(stats["loss_count"], 1)
        self.assertEqual(stats["decision_breakdown"]["BUY"], 1)
        self.assertEqual(stats["risk_reason_counts"]["Cooldown active for another 15 minute(s)."], 1)
        self.assertAlmostEqual(stats["avg_estimated_pnl_pct"], -0.2)
        self.assertEqual(stats["ledger_stats"]["net_realized_pnl"], 4.5)
        self.assertEqual(stats["symbol_performance"][0]["symbol"], "BTCUSDT")

    def test_prepare_memory_entries_marks_hypothesis_under_low_support(self):
        stats = {
            "window_hours": 24,
            "journal_count": 6,
            "evaluated_trade_count": 4,
            "win_rate": 50.0,
            "avg_estimated_pnl_pct": 0.12,
        }
        lessons = [
            MemoryLesson(
                rule="Avoid chasing weak breakouts after a loss.",
                regime="high_volatility",
                category="anti_pattern",
                support_count=2,
                evidence_journal_ids=[7, 8],
                hypothesis=True,
            )
        ]

        entries = self.updater._prepare_memory_entries(stats, lessons)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["metrics"]["status"], "hypothesis")
        self.assertEqual(entries[0]["metrics"]["support"], 2)
        self.assertEqual(entries[0]["metrics"]["regime"], "high_volatility")


if __name__ == "__main__":
    unittest.main()
