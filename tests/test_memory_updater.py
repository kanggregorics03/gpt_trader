import unittest
from unittest.mock import patch

from batch.memory_updater import MemoryLesson, MemoryUpdater
from core.models import MemoryCondition


class TestMemoryUpdater(unittest.TestCase):
    def setUp(self):
        self.updater = MemoryUpdater.__new__(MemoryUpdater)

    def test_resolve_model_name_prefers_memory_model(self):
        with patch.dict(
            "os.environ",
            {
                "GEMINI_MEMORY_MODEL": "gemini-2.5-pro",
                "GEMINI_TRIGGER_MODEL": "gemini-2.5-flash",
                "GEMINI_DEFAULT_MODEL": "gemini-2.5-flash-lite",
                "GEMINI_MODEL": "gemini-1.5-pro",
            },
            clear=False,
        ):
            self.assertEqual(self.updater._resolve_model_name(), "gemini-2.5-pro")

    def test_resolve_model_name_falls_back_in_order(self):
        with patch.dict(
            "os.environ",
            {
                "GEMINI_MEMORY_MODEL": "",
                "GEMINI_TRIGGER_MODEL": "gemini-2.5-pro",
                "GEMINI_DEFAULT_MODEL": "gemini-2.5-flash",
                "GEMINI_MODEL": "gemini-1.5-pro",
            },
            clear=False,
        ):
            self.assertEqual(self.updater._resolve_model_name(), "gemini-2.5-pro")

        with patch.dict(
            "os.environ",
            {
                "GEMINI_MEMORY_MODEL": "",
                "GEMINI_TRIGGER_MODEL": "",
                "GEMINI_DEFAULT_MODEL": "gemini-2.5-flash",
                "GEMINI_MODEL": "gemini-1.5-pro",
            },
            clear=False,
        ):
            self.assertEqual(self.updater._resolve_model_name(), "gemini-2.5-flash")

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
                "audit_log_id": 101,
                "decision": "EXIT",
                "rationale": "Stopped out",
                "rationale_json": [
                    {
                        "evidence": [
                            {"signal": "rsi_14", "symbol": "BTCUSDT", "timeframe": "15m"},
                            {"signal": "volume_ratio", "symbol": "BTCUSDT", "timeframe": "15m"},
                        ]
                    }
                ],
                "risk_reason": "Approved",
                "estimated_pnl_pct": -1.2,
                "ledger_net_realized_pnl": -12.4,
                "ledger_fee_cost": 0.8,
                "risk_mode": "trigger",
                "evidence_quality": {
                    "factor": 0.7,
                    "issues": ["low_signal_diversity"],
                },
                "entry_size_cap": {
                    "base_cap_pct": 30.0,
                    "effective_cap_pct": 21.0,
                },
                "market_state": {
                    "market_snapshots": {
                        "BTCUSDT": {
                            "regime": {"trend_alignment": "aligned_up", "volatility_regime": "high"},
                            "opportunity_trigger_reasons": ["fast_price_move_15m"],
                            "risk_trigger_reasons": [],
                        }
                    }
                },
                "loss_signal": 1,
            },
            {
                "id": 2,
                "audit_log_id": 102,
                "decision": "REDUCE",
                "rationale": "Scaled out",
                "rationale_json": [
                    {
                        "evidence": [
                            {"signal": "rsi_14", "symbol": "BTCUSDT", "timeframe": "15m"}
                        ]
                    }
                ],
                "risk_reason": "Approved",
                "estimated_pnl_pct": 0.8,
                "ledger_net_realized_pnl": 8.6,
                "ledger_fee_cost": 0.4,
                "risk_mode": "normal",
                "evidence_quality": {
                    "factor": 1.0,
                    "issues": [],
                },
                "entry_size_cap": {
                    "base_cap_pct": 30.0,
                    "effective_cap_pct": 30.0,
                },
                "market_state": {
                    "market_snapshots": {
                        "BTCUSDT": {
                            "regime": {"trend_alignment": "aligned_up", "volatility_regime": "normal"},
                            "opportunity_trigger_reasons": ["range_break_1h"],
                            "risk_trigger_reasons": [],
                        }
                    }
                },
                "loss_signal": 0,
            },
            {
                "id": 3,
                "audit_log_id": 103,
                "decision": "BUY",
                "rationale": "Rejected setup",
                "rationale_json": [],
                "risk_reason": "Cooldown active for another 15 minute(s).",
                "estimated_pnl_pct": None,
                "ledger_net_realized_pnl": 0.0,
                "ledger_fee_cost": 0.0,
                "risk_mode": "cooldown",
                "market_state": {},
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
        self.assertEqual(stats["evidence_signal_counts"]["BTCUSDT:rsi_14@15m"], 2)
        self.assertEqual(stats["winning_evidence_signals"]["BTCUSDT:rsi_14@15m"], 1)
        self.assertEqual(stats["losing_evidence_signals"]["BTCUSDT:rsi_14@15m"], 1)
        self.assertEqual(stats["evidence_quality_patterns"]["factor_buckets"]["low"]["count"], 1)
        self.assertEqual(stats["evidence_quality_patterns"]["factor_buckets"]["full"]["count"], 1)
        self.assertEqual(stats["evidence_quality_patterns"]["adjusted_cap"]["count"], 1)
        self.assertEqual(stats["evidence_quality_patterns"]["issue_counts"]["low_signal_diversity"], 1)
        self.assertAlmostEqual(stats["avg_estimated_pnl_pct"], -0.2)
        self.assertEqual(stats["ledger_stats"]["net_realized_pnl"], 4.5)
        self.assertEqual(stats["symbol_performance"][0]["symbol"], "BTCUSDT")
        self.assertEqual(stats["top_losses"][0]["market_regime"]["BTCUSDT"]["volatility_regime"], "high")
        self.assertNotIn("rationale", stats["top_losses"][0])
        self.assertIn("BTCUSDT rsi_14@15m", stats["top_losses"][0]["evidence_summary"][0])
        self.assertEqual(stats["recent_examples"][0]["ledger_fee_cost"], 0.8)

    def test_build_stats_package_adds_quality_pattern_candidates(self):
        class DummyStore:
            def get_position_ledger_stats(self, hours):
                return {"row_count": 3, "net_realized_pnl": -5.0, "gross_realized_pnl": -3.0, "fee_cost": 2.0}

            def get_symbol_performance_report(self, hours):
                return []

            def get_daily_ledger_report(self, days):
                return []

        self.updater.state_store = DummyStore()
        journals = [
            {
                "id": 20,
                "decision": "BUY",
                "rationale_json": [],
                "risk_reason": "Approved",
                "estimated_pnl_pct": -1.1,
                "evidence_quality": {"factor": 0.7, "issues": ["low_signal_diversity"]},
                "entry_size_cap": {"base_cap_pct": 30.0, "effective_cap_pct": 21.0},
                "market_state": {
                    "market_snapshots": {
                        "ETHUSDT": {
                            "regime": {"trend_alignment": "aligned_down", "volatility_regime": "high"},
                        }
                    }
                },
                "loss_signal": 1,
            },
            {
                "id": 21,
                "decision": "SELL",
                "rationale_json": [],
                "risk_reason": "Approved",
                "estimated_pnl_pct": -0.8,
                "evidence_quality": {"factor": 0.6, "issues": ["missing_failure_mode"]},
                "entry_size_cap": {"base_cap_pct": 30.0, "effective_cap_pct": 18.0},
                "market_state": {
                    "market_snapshots": {
                        "ETHUSDT": {
                            "regime": {"trend_alignment": "aligned_down", "volatility_regime": "high"},
                        }
                    }
                },
                "loss_signal": 1,
            },
            {
                "id": 22,
                "decision": "REDUCE",
                "rationale_json": [],
                "risk_reason": "Approved",
                "estimated_pnl_pct": 0.3,
                "evidence_quality": {"factor": 1.0, "issues": []},
                "entry_size_cap": {"base_cap_pct": 30.0, "effective_cap_pct": 30.0},
                "market_state": {
                    "market_snapshots": {
                        "ETHUSDT": {
                            "regime": {"trend_alignment": "aligned_down", "volatility_regime": "high"},
                        }
                    }
                },
                "loss_signal": 0,
            },
        ]

        stats = self.updater._build_stats_package(journals, window_hours=24)

        self.assertEqual(stats["evidence_quality_patterns"]["factor_buckets"]["low"]["count"], 2)
        self.assertEqual(stats["evidence_quality_patterns"]["adjusted_cap"]["loss_count"], 2)
        quality_candidates = [
            item for item in stats["candidate_lessons"]
            if item.get("seed_origin") == "heuristic_quality_pattern"
        ]
        self.assertEqual(len(quality_candidates), 2)
        self.assertTrue(any(item["seed_signal"] == "evidence_quality:low" for item in quality_candidates))
        self.assertTrue(any(item["seed_signal"] == "entry_size_cap:adjusted" for item in quality_candidates))
        self.assertTrue(all(item["category"] == "risk_control" for item in quality_candidates))
        self.assertEqual(quality_candidates[0]["apply_when"]["trend"], ["aligned_down"])

    def test_build_candidate_lessons_promotes_repeated_evidence_signal(self):
        journals = [
            {
                "id": 10,
                "rationale_json": [{"evidence": [{"signal": "volume_ratio", "symbol": "BTCUSDT", "timeframe": "15m"}]}],
                "market_state": {
                    "market_snapshots": {
                        "BTCUSDT": {
                            "regime": {"trend_alignment": "aligned_up", "volatility_regime": "normal"},
                            "opportunity_trigger_reasons": ["volume_surge_15m"],
                            "risk_trigger_reasons": [],
                        }
                    }
                },
            },
            {
                "id": 11,
                "rationale_json": [{"evidence": [{"signal": "volume_ratio", "symbol": "BTCUSDT", "timeframe": "15m"}]}],
                "market_state": {
                    "market_snapshots": {
                        "BTCUSDT": {
                            "regime": {"trend_alignment": "aligned_up", "volatility_regime": "normal"},
                            "opportunity_trigger_reasons": ["volume_surge_15m"],
                            "risk_trigger_reasons": [],
                        }
                    }
                },
            },
        ]

        candidates = self.updater._build_candidate_lessons(
            journals=journals,
            winning_evidence_signals={"BTCUSDT:volume_ratio@15m": 2},
            losing_evidence_signals={},
            evidence_quality_patterns={},
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["category"], "opportunity")
        self.assertEqual(candidates[0]["support_count"], 2)
        self.assertEqual(candidates[0]["evidence_journal_ids"], [10, 11])
        self.assertEqual(candidates[0]["apply_when"]["trend"], ["aligned_up"])

    def test_build_stats_package_includes_symbol_journal_candidates(self):
        class DummyStore:
            def get_position_ledger_stats(self, hours):
                return {"row_count": 1, "net_realized_pnl": 2.0, "gross_realized_pnl": 2.5, "fee_cost": 0.5}

            def get_symbol_performance_report(self, hours):
                return [{"symbol": "ETHUSDT", "net_realized_pnl": 2.0, "win_rate": 100.0}]

            def get_daily_ledger_report(self, days):
                return [{"day": "2026-03-05", "net_realized_pnl": 2.0, "row_count": 1}]

        self.updater.state_store = DummyStore()
        journals = [
            {
                "id": 31,
                "decision": "HOLD",
                "rationale_json": [],
                "risk_reason": "Approved",
                "estimated_pnl_pct": None,
                "loss_signal": 0,
                "market_state": {},
            }
        ]
        symbol_journals = [
            {
                "id": 201,
                "audit_log_id": 901,
                "symbol": "ETHUSDT",
                "decision": "BUY",
                "estimated_pnl_pct": 0.7,
                "net_realized_pnl": 5.0,
                "evidence_signals": ["volume_ratio@15m", "spread_bps@microstructure"],
            },
            {
                "id": 202,
                "audit_log_id": 902,
                "symbol": "ETHUSDT",
                "decision": "BUY",
                "estimated_pnl_pct": 0.5,
                "net_realized_pnl": 3.0,
                "evidence_signals": ["volume_ratio@15m"],
            },
        ]

        stats = self.updater._build_stats_package(
            journals=journals,
            window_hours=24,
            symbol_journals=symbol_journals,
        )

        self.assertEqual(stats["symbol_journal_count"], 2)
        self.assertEqual(stats["symbol_evidence_signal_counts"]["ETHUSDT:volume_ratio@15m"], 2)
        symbol_candidates = [
            item for item in stats["candidate_lessons"]
            if item.get("seed_origin") == "symbol_journal_candidate"
        ]
        self.assertTrue(symbol_candidates)
        self.assertEqual(
            symbol_candidates[0]["evidence_journal_ids"],
            [201, 202],
        )

    def test_generate_lessons_falls_back_to_candidate_lessons_on_failure(self):
        class DummyClient:
            class models:
                @staticmethod
                def generate_content(**kwargs):
                    raise RuntimeError("boom")

        self.updater.client = DummyClient()
        self.updater.model_name = "gemini-2.5-flash"
        stats = {
            "journal_count": 2,
            "candidate_lessons": [
                {
                    "rule": "Favor setups confirmed by volume_ratio on 15m for BTCUSDT when the same evidence repeatedly outperforms.",
                    "regime": "aligned_up_normal",
                    "category": "opportunity",
                    "support_count": 2,
                    "evidence_journal_ids": [10, 11],
                    "hypothesis": True,
                    "apply_when": {"regime": ["aligned_up_normal"], "volatility": ["normal"], "trend": ["aligned_up"]},
                    "ignore_when": ["Ignore when volume_ratio on 15m is present without confirming regime alignment."],
                }
            ],
        }

        lessons = self.updater._generate_lessons(stats)

        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0].category, "opportunity")

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
                apply_when=MemoryCondition(
                    regime=["high_volatility"],
                    volatility=["high"],
                    trend=["up"],
                ),
                ignore_when=["Ignore when breakout retest holds with rising volume."],
            )
        ]

        entries = self.updater._prepare_memory_entries(stats, lessons)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["metrics"]["status"], "hypothesis")
        self.assertEqual(entries[0]["metrics"]["support"], 2)
        self.assertEqual(entries[0]["metrics"]["regime"], "high_volatility")
        self.assertEqual(entries[0]["metrics"]["apply_when"]["trend"], ["up"])
        self.assertEqual(
            entries[0]["metrics"]["ignore_when"],
            ["Ignore when breakout retest holds with rising volume."],
        )


if __name__ == "__main__":
    unittest.main()
