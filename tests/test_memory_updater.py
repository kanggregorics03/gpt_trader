import unittest
from unittest.mock import patch
from datetime import UTC, datetime, timedelta

from ai.llm_provider import resolve_api_key
from batch.memory_updater import MemoryLesson, MemoryUpdater
from core.models import MemoryCondition


class TestMemoryUpdater(unittest.TestCase):
    def setUp(self):
        self.updater = MemoryUpdater.__new__(MemoryUpdater)
        self.updater.provider_name = "gemini"

    def test_resolve_model_name_prefers_memory_model(self):
        with patch.dict(
            "os.environ",
            {
                "MEMORY_MODEL": "",
                "AI_DEFAULT_MODEL": "",
                "AI_GUARDED_ATTENTION_MODEL": "",
                "GEMINI_MEMORY_MODEL": "gemini-2.5-pro",
                "GEMINI_GUARDED_ATTENTION_MODEL": "gemini-2.5-flash",
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
                "MEMORY_MODEL": "",
                "AI_DEFAULT_MODEL": "",
                "AI_GUARDED_ATTENTION_MODEL": "",
                "GEMINI_MEMORY_MODEL": "",
                "GEMINI_GUARDED_ATTENTION_MODEL": "gemini-2.5-pro",
                "GEMINI_DEFAULT_MODEL": "gemini-2.5-flash",
                "GEMINI_MODEL": "gemini-1.5-pro",
            },
            clear=False,
        ):
            self.assertEqual(self.updater._resolve_model_name(), "gemini-2.5-pro")

        with patch.dict(
            "os.environ",
            {
                "MEMORY_MODEL": "",
                "AI_DEFAULT_MODEL": "",
                "AI_GUARDED_ATTENTION_MODEL": "",
                "GEMINI_MEMORY_MODEL": "",
                "GEMINI_GUARDED_ATTENTION_MODEL": "",
                "GEMINI_DEFAULT_MODEL": "gemini-2.5-flash",
                "GEMINI_MODEL": "gemini-1.5-pro",
            },
            clear=False,
        ):
            self.assertEqual(self.updater._resolve_model_name(), "gemini-2.5-flash")

    def test_memory_provider_prefers_provider_specific_key_before_shared_ai_key(self):
        with patch.dict(
            "os.environ",
            {
                "MEMORY_PROVIDER": "anthropic",
                "MEMORY_API_KEY": "",
                "AI_API_KEY": "shared-key",
                "ANTHROPIC_API_KEY": "anthropic-key",
            },
            clear=False,
        ):
            updater = MemoryUpdater.__new__(MemoryUpdater)
            updater.provider_name = "anthropic"
            updater.shared_ai_api_key = "shared-key"
            self.assertEqual(
                resolve_api_key(
                    updater.provider_name,
                    explicit_api_key="",
                    shared_api_key=updater.shared_ai_api_key,
                ),
                "anthropic-key",
            )

    def test_filter_learning_eligible_journals_excludes_system_error_audits(self):
        journals = [
            {
                "id": 1,
                "audit_log_id": 101,
                "decision": "HOLD",
                "risk_reason": "Approved",
                "rationale": ["Normal hold."],
                "execution_summary": {"ai_usage": {"total_token_count": 123}},
            },
            {
                "id": 2,
                "audit_log_id": 102,
                "decision": "HOLD",
                "risk_reason": "Safe action.",
                "rationale": ["Fallback due to json validation error from AI."],
                "execution_summary": {"ai_usage": {"validation_error": True}},
            },
            {
                "id": 3,
                "audit_log_id": 104,
                "decision": "BUY",
                "risk_reason": "Approved",
                "rationale": ["Breakout long."],
                "execution_summary": {
                    "decision_quality": {
                        "decision_valid": True,
                        "explanation_valid": False,
                        "learning_eligible": False,
                    }
                },
            },
            {
                "id": 4,
                "audit_log_id": 103,
                "decision": "HOLD",
                "risk_reason": "Error in Adaptive (1h) cycle: boom",
                "rationale": ["Loop error during Adaptive (1h)."],
                "execution_summary": {"error": "boom"},
            },
            {
                "id": 5,
                "audit_log_id": 105,
                "decision": "BUY",
                "risk_reason": "Blocked",
                "rationale": ["Probe long."],
                "execution_summary": {
                    "decision_quality": {
                        "decision_valid": True,
                        "explanation_valid": True,
                        "execution_admissible": False,
                        "learning_eligible": True,
                    }
                },
            },
        ]

        eligible, excluded = self.updater._filter_learning_eligible_journals(journals)

        self.assertEqual([row["audit_log_id"] for row in eligible], [101])
        self.assertEqual([row["audit_log_id"] for row in excluded], [102, 104, 103, 105])

    def test_build_recent_journal_reranker_profile_learns_bucket_biases(self):
        profile = self.updater._build_recent_journal_reranker_profile(
            [
                {
                    "decision": "HOLD",
                    "estimated_pnl_pct": 1.2,
                    "rationale_json": [{"hold_quality": "good_hold"}],
                    "loss_signal": False,
                },
                {
                    "decision": "BUY",
                    "estimated_pnl_pct": -0.8,
                    "rationale_json": [],
                    "loss_signal": True,
                },
            ]
        )

        self.assertEqual(profile["model_kind"], "bucket_bias_profile_v1")
        self.assertIn("good_hold", profile["bucket_weights"])
        self.assertIn("BUY", profile["decision_weights"])

    def test_build_similar_case_reranker_profile_uses_counterfactual_and_outcomes(self):
        profile = self.updater._build_similar_case_reranker_profile(
            retrieval_evaluations=[
                {
                    "audit_log_id": 1,
                    "similarity": 0.9,
                    "final_score": 1.2,
                    "matched_band_count": 4,
                    "retrieval_source": "band_index",
                    "outcome_realized_pnl": 1.1,
                    "payload": {"thesis_failure_reason": "wide_spread"},
                },
                {
                    "audit_log_id": 2,
                    "similarity": 0.4,
                    "final_score": 0.3,
                    "matched_band_count": 1,
                    "retrieval_source": "full_scan",
                    "outcome_realized_pnl": -0.9,
                    "payload": {},
                },
            ],
            replay_runs=[
                {
                    "audit_log_id": 1,
                    "payload": {"retrieval_counterfactual": {"with_vs_without_comparison": {"with_retrieval_better": True}}},
                }
            ],
        )

        self.assertEqual(profile["model_kind"], "linear_profile_v1")
        self.assertIn("band_index", profile["source_weights"])
        self.assertNotEqual(profile["similarity_weight"], 0.0)

    def test_learning_promotions_allowed_respects_liveness_gate(self):
        class DummyStore:
            def get_learning_artifact(self, artifact_key):
                return {"payload": {"promotion_blocked": artifact_key == "learning_liveness"}}

        self.updater.state_store = DummyStore()

        self.assertFalse(self.updater._learning_promotions_allowed())

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
                "risk_mode": "guarded",
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
                            "fast_move_signal_active": True,
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
                            "range_break_signal_active": True,
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
                            "volume_surge_signal_active": True,
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
                            "volume_surge_signal_active": True,
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

    def test_build_stats_package_includes_trade_case_patterns(self):
        class DummyStore:
            def get_position_ledger_stats(self, hours):
                return {"row_count": 2, "net_realized_pnl": -1.0, "gross_realized_pnl": 0.0, "fee_cost": 1.0}

            def get_symbol_performance_report(self, hours):
                return []

            def get_daily_ledger_report(self, days):
                return []

        self.updater.state_store = DummyStore()
        stats = self.updater._build_stats_package(
            journals=[],
            window_hours=24,
            symbol_journals=[],
            trade_cases=[
                {
                    "trade_case_id": "case-1",
                    "symbol": "BTCUSDT",
                    "status": "closed",
                    "estimated_pnl_pct": -1.1,
                    "observed_mfe_pct": 0.5,
                    "observed_mae_pct": -1.4,
                    "slippage_bps": 8.0,
                    "thesis_failure_reason": "execution_quality_degraded:wide_spread",
                    "evidence_signals": ["spread_bps@microstructure"],
                    "payload": {"regime_tags": ["trend:aligned_up", "volatility:high"]},
                },
                {
                    "trade_case_id": "case-2",
                    "symbol": "BTCUSDT",
                    "status": "closed",
                    "estimated_pnl_pct": -0.7,
                    "observed_mfe_pct": 0.2,
                    "observed_mae_pct": -0.9,
                    "slippage_bps": 6.0,
                    "thesis_failure_reason": "execution_quality_degraded:wide_spread",
                    "evidence_signals": ["spread_bps@microstructure"],
                    "payload": {"regime_tags": ["trend:aligned_up", "volatility:high"]},
                },
            ],
        )

        self.assertEqual(stats["trade_case_count"], 2)
        self.assertEqual(stats["closed_trade_case_count"], 2)
        self.assertEqual(
            stats["trade_case_patterns"]["failure_reason_counts"]["execution_quality_degraded:wide_spread"]["count"],
            2,
        )
        case_candidates = [
            item for item in stats["candidate_lessons"]
            if item.get("seed_origin") == "trade_case_candidate"
        ]
        cluster_candidates = [
            item for item in stats["candidate_lessons"]
            if item.get("seed_origin") == "trade_case_cluster_candidate"
        ]
        self.assertTrue(case_candidates)
        self.assertTrue(cluster_candidates)
        self.assertEqual(case_candidates[0]["category"], "risk_control")
        self.assertEqual(case_candidates[0]["trade_case_ids"], ["case-1", "case-2"])
        self.assertEqual(stats["trade_case_clusters"][0]["count"], 2)
        self.assertEqual(stats["trade_case_clusters"][0]["loss_count"], 2)
        self.assertEqual(stats["trade_case_clusters"][0]["trend_alignment"], "aligned_up")
        self.assertEqual(stats["trade_case_clusters"][0]["volatility_regime"], "high")
        self.assertIn("quality_score", stats["trade_case_clusters"][0])
        self.assertIn("quality_label", stats["trade_case_clusters"][0])
        self.assertIn("evaluation", stats["trade_case_clusters"][0])
        self.assertIn("silhouette_score", stats["trade_case_clusters"][0]["evaluation"])
        self.assertIn("consistency_score", stats["trade_case_clusters"][0]["evaluation"])
        self.assertEqual(stats["trade_case_cluster_evaluation"]["cluster_count"], 1)
        self.assertGreater(stats["trade_case_cluster_evaluation"]["avg_quality_score"], 0.0)
        self.assertFalse(stats["trade_case_clusters"][0]["retired"])

    def test_build_stats_package_includes_retrieval_evaluation_summary(self):
        class DummyStore:
            def get_position_ledger_stats(self, hours):
                return {"row_count": 1, "net_realized_pnl": 1.0, "gross_realized_pnl": 1.2, "fee_cost": 0.2}

            def get_symbol_performance_report(self, hours):
                return []

            def get_daily_ledger_report(self, days):
                return []

        self.updater.state_store = DummyStore()
        stats = self.updater._build_stats_package(
            journals=[],
            window_hours=24,
            symbol_journals=[],
            trade_cases=[],
            retrieval_evaluations=[
                {
                    "audit_log_id": 11,
                    "context_type": "similar_past_cases",
                    "trade_case_id": "BTCUSDT-case-1",
                    "decision_symbol_aligned": True,
                    "estimated_pnl_pct": 0.8,
                    "outcome_trade_case_id": "BTCUSDT-live-case-1",
                    "outcome_status": "closed",
                    "outcome_realized_pnl": 12.0,
                    "similarity": 0.92,
                    "final_score": 1.35,
                    "retrieval_source": "band_index",
                    "matched_band_count": 4,
                },
                {
                    "audit_log_id": 12,
                    "context_type": "similar_past_cases",
                    "trade_case_id": "BTCUSDT-case-2",
                    "decision_symbol_aligned": False,
                    "estimated_pnl_pct": -0.4,
                    "similarity": 0.71,
                    "final_score": 0.94,
                    "retrieval_source": "full_scan",
                    "matched_band_count": 0,
                },
            ],
        )

        self.assertEqual(stats["retrieval_evaluation_count"], 2)
        self.assertEqual(stats["retrieval_evaluation_summary"]["aligned_count"], 1)
        self.assertEqual(stats["retrieval_evaluation_summary"]["settled_count"], 1)
        self.assertEqual(stats["retrieval_evaluation_summary"]["source_counts"]["band_index"], 1)
        self.assertGreater(stats["retrieval_evaluation_summary"]["avg_similarity"], 0.0)
        self.assertEqual(len(stats["recent_retrieval_evaluations"]), 2)

    def test_build_stats_package_includes_retrieval_counterfactual_summary(self):
        class DummyStore:
            def get_position_ledger_stats(self, hours):
                return {"row_count": 0, "net_realized_pnl": 0.0, "gross_realized_pnl": 0.0, "fee_cost": 0.0}

            def get_symbol_performance_report(self, hours):
                return []

            def get_daily_ledger_report(self, days):
                return []

        self.updater.state_store = DummyStore()
        stats = self.updater._build_stats_package(
            journals=[],
            window_hours=24,
            symbol_journals=[],
            trade_cases=[],
            retrieval_evaluations=[
                {
                    "audit_log_id": 9001,
                    "outcome_trade_case_id": "BTCUSDT-live-case-1",
                    "outcome_status": "closed",
                    "outcome_realized_pnl": -1.2,
                    "outcome_observed_mae_pct": -1.7,
                    "outcome_slippage_bps": 6.5,
                    "outcome_holding_minutes": 135,
                }
            ],
            replay_runs=[
                {
                    "audit_log_id": 9001,
                    "payload": {
                        "retrieval_counterfactual": {
                            "with_retrieval_intent": {"decision": "EXIT"},
                            "without_retrieval_intent": {"decision": "BUY"},
                            "with_vs_without_comparison": {
                                "decision_changed": True,
                                "side_changed": True,
                                "confidence_delta": -0.12,
                                "size_delta": -0.01,
                            }
                        }
                    }
                },
                {
                    "audit_log_id": 9002,
                    "payload": {
                        "retrieval_counterfactual": {
                            "with_retrieval_intent": {"decision": "BUY"},
                            "without_retrieval_intent": {"decision": "BUY"},
                            "with_vs_without_comparison": {
                                "decision_changed": False,
                                "side_changed": False,
                                "confidence_delta": 0.05,
                                "size_delta": 0.0,
                            }
                        }
                    }
                },
            ],
        )

        self.assertEqual(stats["replay_run_count"], 2)
        self.assertEqual(stats["retrieval_counterfactual_summary"]["evaluation_count"], 2)
        self.assertEqual(stats["retrieval_counterfactual_summary"]["decision_changed_count"], 1)
        self.assertEqual(stats["retrieval_counterfactual_summary"]["side_changed_count"], 1)
        self.assertEqual(stats["retrieval_counterfactual_summary"]["outcome_linked_count"], 1)
        self.assertEqual(stats["retrieval_counterfactual_summary"]["with_retrieval_better_count"], 1)
        self.assertEqual(stats["retrieval_counterfactual_summary"]["without_retrieval_better_count"], 0)
        self.assertGreater(stats["retrieval_counterfactual_summary"]["avg_outcome_utility_delta"], 0.0)
        self.assertEqual(len(stats["recent_retrieval_counterfactuals"]), 2)

    def test_build_stats_package_includes_hold_opportunity_summary_and_candidate(self):
        class DummyStore:
            def get_position_ledger_stats(self, hours):
                return {"row_count": 0, "net_realized_pnl": 0.0, "gross_realized_pnl": 0.0, "fee_cost": 0.0}

            def get_symbol_performance_report(self, hours):
                return []

            def get_daily_ledger_report(self, days):
                return []

        self.updater.state_store = DummyStore()
        journals = [
            {
                "id": 31,
                "audit_log_id": 7001,
                "decision": "HOLD",
                "rationale_json": [],
                "risk_reason": "Safe action.",
                "estimated_pnl_pct": None,
                "loss_signal": 0,
                "market_state": {
                    "market_snapshots": {
                        "ETHUSDT": {
                            "regime": {"trend_alignment": "aligned_up", "volatility_regime": "normal"},
                        }
                    }
                },
            },
            {
                "id": 32,
                "audit_log_id": 7002,
                "decision": "HOLD",
                "rationale_json": [],
                "risk_reason": "Safe action.",
                "estimated_pnl_pct": None,
                "loss_signal": 0,
                "market_state": {
                    "market_snapshots": {
                        "ETHUSDT": {
                            "regime": {"trend_alignment": "aligned_up", "volatility_regime": "normal"},
                        }
                    }
                },
            },
        ]
        replay_runs = [
            {
                "audit_log_id": 7001,
                "payload": {
                    "hold_opportunity_counterfactual": {
                        "baseline_beats_hold": True,
                        "simple_rule_baseline_beats_hold": True,
                        "missed_breakout_count": 2,
                        "missed_move_median_pct": 1.3,
                        "hold_opportunity_cost": 1.1,
                        "hold_quality": "missed_opportunity_hold",
                        "trigger_context": {"signature": "volume_surge+range_break+opportunity"},
                        "execution_context": {"signature": "acceptable_spread|deep_book|manageable_invalidation"},
                        "trend_context": {"signature": "UP|UP"},
                    }
                },
            },
            {
                "audit_log_id": 7002,
                "payload": {
                    "hold_opportunity_counterfactual": {
                        "baseline_beats_hold": True,
                        "simple_rule_baseline_beats_hold": False,
                        "missed_breakout_count": 1,
                        "missed_move_median_pct": 0.9,
                        "hold_opportunity_cost": 0.7,
                        "hold_quality": "probe_only_opportunity_hold",
                        "trigger_context": {"signature": "volume_surge+opportunity"},
                        "execution_context": {"signature": "acceptable_spread|deep_book|manageable_invalidation"},
                        "trend_context": {"signature": "UP|UP"},
                    }
                },
            },
        ]

        stats = self.updater._build_stats_package(
            journals=journals,
            window_hours=24,
            replay_runs=replay_runs,
        )

        self.assertEqual(stats["hold_opportunity_summary"]["evaluation_count"], 2)
        self.assertEqual(stats["hold_opportunity_summary"]["baseline_beats_hold_count"], 2)
        self.assertEqual(stats["hold_opportunity_summary"]["missed_breakout_count"], 3)
        self.assertEqual(stats["hold_opportunity_summary"]["simple_rule_beats_hold_count"], 1)
        self.assertGreater(stats["hold_opportunity_summary"]["hold_opportunity_cost"], 1.0)
        self.assertEqual(stats["hold_opportunity_summary"]["dominant_execution_context"], "acceptable_spread|deep_book|manageable_invalidation")
        undertrading_candidates = [
            item for item in stats["candidate_lessons"]
            if item.get("seed_origin") == "hold_opportunity_counterfactual"
        ]
        self.assertEqual(len(undertrading_candidates), 1)
        self.assertEqual(undertrading_candidates[0]["category"], "opportunity")
        self.assertEqual(undertrading_candidates[0]["trigger_context"], "volume_surge+range_break+opportunity")

    def test_build_trading_balance_summary_flags_overtrading(self):
        summary = self.updater._build_trading_balance_summary(
            journals=[
                {"decision": "BUY"},
                {"decision": "SELL"},
                {"decision": "HOLD"},
            ],
            trade_cases=[
                {
                    "status": "closed",
                    "entry_decision": "BUY",
                    "estimated_pnl_pct": -1.4,
                    "realized_pnl": -1.4,
                },
                {
                    "status": "closed",
                    "entry_decision": "SELL",
                    "estimated_pnl_pct": -0.9,
                    "realized_pnl": -0.9,
                },
            ],
            hold_opportunity_summary={
                "hold_opportunity_cost": 0.3,
                "missed_breakout_count": 0,
                "missed_move_median_pct": 0.0,
            },
        )

        self.assertEqual(summary["stance"], "overtrading")
        self.assertGreater(summary["false_entry_cost"], summary["hold_opportunity_cost"])
        self.assertEqual(summary["false_entry_cost_pct"], summary["false_entry_cost"])
        self.assertGreater(summary["false_entry_cost_amount"], 0.0)
        self.assertLess(summary["balance_score"], 1.0)
        self.assertLess(summary["pressure_ratio"], 1.0)

    def test_prepare_memory_entries_persists_hold_context_fields(self):
        stats = {
            "window_hours": 24,
            "journal_count": 4,
            "evaluated_trade_count": 2,
            "win_rate": 50.0,
            "avg_estimated_pnl_pct": 0.1,
            "ledger_stats": {"net_realized_pnl": 1.2},
            "trade_case_patterns": {},
            "trade_case_clusters": [],
            "trade_case_cluster_evaluation": {},
            "retrieval_evaluation_summary": {},
            "retrieval_counterfactual_summary": {},
            "hold_opportunity_summary": {},
            "trading_balance_summary": {},
            "execution_policy_summary": {},
            "candidate_lessons": [],
            "trade_case_count": 0,
            "retrieval_evaluation_count": 0,
            "replay_run_count": 0,
            "execution_policy_evaluation_count": 0,
            "closed_trade_case_count": 0,
        }
        lessons = [
            MemoryLesson(
                rule="Allow small probe entries when range breaks align with tradable execution.",
                regime="aligned_up_normal",
                category="opportunity",
                support_count=3,
                evidence_journal_ids=[11, 12, 13],
                apply_when=MemoryCondition(
                    regime=["aligned_up_normal"],
                    volatility=["normal"],
                    trend=["aligned_up"],
                ),
                ignore_when=["Ignore when spread widens materially."],
                trigger_context="volume_surge+range_break+opportunity",
                execution_context="acceptable_spread|deep_book|manageable_invalidation",
                trend_context="UP|UP",
                hold_quality="missed_opportunity_hold",
            )
        ]

        entries = self.updater._prepare_memory_entries(stats, lessons)

        self.assertEqual(entries[0]["metrics"]["trigger_context"], "volume_surge+range_break+opportunity")
        self.assertEqual(entries[0]["metrics"]["execution_context"], "acceptable_spread|deep_book|manageable_invalidation")
        self.assertEqual(entries[0]["metrics"]["trend_context"], "UP|UP")
        self.assertEqual(entries[0]["metrics"]["hold_quality"], "missed_opportunity_hold")

    def test_build_confidence_calibration_summary_tracks_alignment(self):
        summary = self.updater._build_confidence_calibration_summary(
            [
                {"decision": "BUY", "confidence": 0.9, "estimated_pnl_pct": -0.6},
                {"decision": "SELL", "confidence": 0.8, "estimated_pnl_pct": 0.7},
                {"decision": "HOLD", "confidence": 0.35, "estimated_pnl_pct": 0.05},
                {"decision": "HOLD", "confidence": 0.85, "estimated_pnl_pct": 0.9},
            ]
        )

        self.assertEqual(summary["evaluated_count"], 4)
        self.assertGreater(summary["calibration_gap"], 0.0)
        self.assertGreater(summary["high_conf_error_rate"], 0.0)
        self.assertLess(summary["alignment_score"], 1.0)

    def test_merge_candidate_lessons_preserves_seed_specificity_and_drops_unmatched_generated(self):
        candidates = [
            MemoryLesson(
                rule="Allow probes only when trigger pressure aligns with tradable execution.",
                regime="aligned_up_high",
                category="opportunity",
                support_count=4,
                evidence_journal_ids=[10, 11],
                apply_when=MemoryCondition(
                    regime=["aligned_up_high"],
                    volatility=["high"],
                    trend=["aligned_up"],
                ),
                ignore_when=["Ignore when spreads widen."],
                trigger_context="volume_surge+range_break+opportunity",
                execution_context="acceptable_spread|deep_book|manageable_invalidation",
                trend_context="UP|UP",
                hold_quality="missed_opportunity_hold",
            )
        ]
        generated = [
            MemoryLesson(
                rule="Allow probes when trigger pressure aligns.",
                regime="general",
                category="opportunity",
                support_count=5,
                evidence_journal_ids=[11, 12],
                apply_when=MemoryCondition(
                    regime=["general"],
                    volatility=["normal"],
                    trend=["mixed"],
                ),
                ignore_when=["Ignore rarely."],
            ),
            MemoryLesson(
                rule="Always reduce quickly after random volatility spikes.",
                regime="general",
                category="risk_control",
                support_count=4,
                evidence_journal_ids=[77],
                apply_when=MemoryCondition(
                    regime=["general"],
                    volatility=["high"],
                    trend=["mixed"],
                ),
                ignore_when=["Ignore when unknown."],
            ),
        ]

        merged = self.updater._merge_candidate_lessons(candidates, generated)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].trigger_context, "volume_surge+range_break+opportunity")
        self.assertEqual(merged[0].execution_context, "acceptable_spread|deep_book|manageable_invalidation")
        self.assertEqual(merged[0].apply_when.regime, ["aligned_up_high"])

    def test_promote_candidate_policies_is_disabled_for_fixed_policy_runtime(self):
        class DummyStore:
            def __init__(self):
                self.promotions = []

            def build_policy_key(self, domain, regime_key):
                return f"champion:{domain}:{regime_key}"

            def get_policy_registry_entry(self, policy_key):
                return {}

            def upsert_policy_registry(self, payload):
                self.promotions.append(payload)

            def save_policy_promotion(self, payload):
                self.promotions.append(payload)

        self.updater.state_store = DummyStore()
        candidates = [
            {
                "id": 11,
                "domain": "alpha_1h",
                "policy_type": "entry_filter",
                "regime_key": "aligned_up|normal",
                "rule_text": "Allow probes when trend is aligned.",
                "config": {},
            }
        ]
        evaluations = [{"candidate_policy_id": 11, "evaluation_scope": "seed", "sample_count": 6, "score": 0.4, "metrics": {}}]

        self.updater._promote_candidate_policies(candidates, evaluations)

        self.assertEqual(self.updater.state_store.promotions, [])

    def test_promote_candidate_policies_is_disabled_even_for_positive_candidate(self):
        class DummyStore:
            def __init__(self):
                self.promotions = []

            def build_policy_key(self, domain, regime_key):
                return f"champion:{domain}:{regime_key}"

            def get_policy_registry_entry(self, policy_key):
                return {}

            def upsert_policy_registry(self, payload):
                self.promotions.append(payload)

            def save_policy_promotion(self, payload):
                self.promotions.append(payload)

        self.updater.state_store = DummyStore()
        candidates = [
            {
                "id": 12,
                "domain": "alpha_1h",
                "policy_type": "entry_filter",
                "regime_key": "aligned_up|high",
                "rule_text": "Probe aligned breakouts with strong execution.",
                "config": {},
            }
        ]
        evaluations = [
            {
                "candidate_policy_id": 12,
                "evaluation_scope": "shadow",
                "sample_count": 8,
                "score": 0.9,
                "metrics": {
                    "regime_key": "aligned_up|high",
                    "confidence_alignment_score": 0.9,
                    "temporal_holdout_summary": {
                        "train_count": 5,
                        "holdout_count": 3,
                        "train_score": 0.8,
                        "holdout_score": 0.7,
                        "score_gap": 0.1,
                        "stability_score": 0.9,
                    },
                    "trading_balance_summary": {
                        "hold_opportunity_cost": 0.2,
                        "false_entry_cost": 0.1,
                        "balance_score": 0.9,
                        "pressure_ratio": 1.0,
                        "portfolio_utility_score": 0.9,
                    },
                },
            }
        ]

        self.updater._promote_candidate_policies(candidates, evaluations)

        self.assertEqual(self.updater.state_store.promotions, [])

    def test_retrieval_counterfactual_summary_uses_path_utility_not_only_pnl_sign(self):
        summary = self.updater._apply_retrieval_outcome_counterfactual_summary(
            summary={
                "evaluation_count": 1,
                "decision_changed_count": 1,
                "side_changed_count": 1,
                "avg_confidence_delta": 0.0,
                "avg_size_delta": 0.0,
                "outcome_linked_count": 0,
                "with_retrieval_better_count": 0,
                "without_retrieval_better_count": 0,
                "outcome_tie_count": 0,
                "avg_outcome_utility_delta": 0.0,
            },
            retrieval_evaluations=[
                {
                    "audit_log_id": 9010,
                    "outcome_trade_case_id": "BTCUSDT-live-case-2",
                    "outcome_status": "closed",
                    "outcome_realized_pnl": 4.0,
                    "outcome_observed_mfe_pct": 5.0,
                    "outcome_observed_mae_pct": -4.5,
                    "outcome_slippage_bps": 28.0,
                    "outcome_holding_minutes": 480,
                }
            ],
            replay_runs=[
                {
                    "audit_log_id": 9010,
                    "payload": {
                        "retrieval_counterfactual": {
                            "with_retrieval_intent": {"decision": "BUY"},
                            "without_retrieval_intent": {"decision": "EXIT"},
                        }
                    },
                }
            ],
        )

        self.assertEqual(summary["outcome_linked_count"], 1)
        self.assertEqual(summary["with_retrieval_better_count"], 0)
        self.assertEqual(summary["without_retrieval_better_count"], 1)
        self.assertLess(summary["avg_outcome_utility_delta"], 0.0)

    def test_build_trade_case_cluster_candidate_lessons_skips_retired_clusters(self):
        candidates = self.updater._build_trade_case_cluster_candidate_lessons(
            [
                {
                    "cluster_id": "cluster-1",
                    "symbol": "BTCUSDT",
                    "thesis_failure_reason": "execution_quality_degraded:wide_spread",
                    "trend_alignment": "aligned_up",
                    "volatility_regime": "high",
                    "count": 4,
                    "loss_count": 3,
                    "quality_score": 0.72,
                    "retired": False,
                    "avg_slippage_bps": 7.1,
                    "numeric_feature_centroid": {
                        "spread_percentile_1h": 91.0,
                        "volume_ratio_15m": 2.1,
                        "distance_to_invalid_level": 1.0,
                    },
                    "evaluation": {
                        "silhouette_score": 0.31,
                        "loss_rate_gap": 0.12,
                    },
                    "trade_case_ids": ["case-a", "case-b"],
                },
                {
                    "cluster_id": "cluster-2",
                    "symbol": "BTCUSDT",
                    "thesis_failure_reason": "execution_quality_degraded:wide_spread",
                    "trend_alignment": "aligned_up",
                    "volatility_regime": "high",
                    "count": 4,
                    "loss_count": 3,
                    "quality_score": 0.71,
                    "retired": True,
                    "retirement_reason": "holdout_not_confirmed",
                    "avg_slippage_bps": 7.2,
                    "numeric_feature_centroid": {
                        "spread_percentile_1h": 92.0,
                        "volume_ratio_15m": 2.2,
                        "distance_to_invalid_level": 1.1,
                    },
                    "evaluation": {
                        "silhouette_score": 0.28,
                        "loss_rate_gap": 0.61,
                    },
                    "trade_case_ids": ["case-c", "case-d"],
                },
            ]
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["trade_case_ids"], ["case-a", "case-b"])

    def test_generate_lessons_falls_back_to_candidate_lessons_on_failure(self):
        class DummyProvider:
            @staticmethod
            def generate_json(**kwargs):
                raise RuntimeError("boom")

        self.updater.provider_name = "gemini"
        self.updater.llm_provider = DummyProvider()
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

    def test_generate_lessons_normalizes_common_llm_schema_drift(self):
        class DummyResponse:
            text = """
            [
              {
                "title": "Enforce Symbol-specific risk controls when Safe action repeats under mixed conditions.",
                "support_count": "2",
                "evidence_journal_ids": ["12", "13"],
                "risk_control": true,
                "apply_when": {
                  "regime": "mixed",
                  "volatility": "normal",
                  "trend": "mixed"
                },
                "ignore_when": {
                  "risk_reason": "Safe action.",
                  "risk_mode": "normal"
                }
              }
            ]
            """

        class DummyProvider:
            @staticmethod
            def generate_json(**kwargs):
                return DummyResponse()

        self.updater.provider_name = "deepseek"
        self.updater.llm_provider = DummyProvider()
        self.updater.model_name = "deepseek-reasoner"

        lessons = self.updater._generate_lessons(
            {
                "journal_count": 2,
                "candidate_lessons": [],
            }
        )

        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0].category, "risk_control")
        self.assertEqual(
            lessons[0].rule,
            "Enforce Symbol-specific risk controls when Safe action repeats under mixed conditions.",
        )
        self.assertEqual(lessons[0].regime, "mixed")
        self.assertEqual(lessons[0].support_count, 2)
        self.assertEqual(lessons[0].evidence_journal_ids, [12, 13])
        self.assertEqual(lessons[0].apply_when.regime, ["mixed"])
        self.assertEqual(lessons[0].apply_when.volatility, ["normal"])
        self.assertEqual(lessons[0].apply_when.trend, ["mixed"])
        self.assertEqual(
            lessons[0].ignore_when,
            ["risk_reason: Safe action.", "risk_mode: normal"],
        )

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
        self.assertIn("confidence_score", entries[0]["metrics"])
        self.assertEqual(entries[0]["metrics"]["lifecycle_status"], "active")
        self.assertFalse(entries[0]["metrics"]["retired"])

    def test_prepare_memory_entries_merges_similar_lessons(self):
        stats = {
            "window_hours": 24,
            "journal_count": 8,
            "evaluated_trade_count": 6,
            "win_rate": 42.0,
            "avg_estimated_pnl_pct": -0.3,
            "trade_case_cluster_evaluation": {"avg_quality_score": 0.62},
            "retrieval_counterfactual_summary": {
                "with_retrieval_better_count": 3,
                "without_retrieval_better_count": 1,
                "outcome_tie_count": 1,
            },
        }
        lessons = [
            MemoryLesson(
                rule="Avoid chasing weak breakouts after a loss.",
                regime="high_volatility",
                category="anti_pattern",
                support_count=3,
                evidence_journal_ids=[7, 8, 9],
                hypothesis=False,
                apply_when=MemoryCondition(
                    regime=["high_volatility"],
                    volatility=["high"],
                    trend=["up"],
                ),
                ignore_when=["Ignore when breakout retest holds with rising volume."],
            ),
            MemoryLesson(
                rule="Avoid chasing weak breakout after losses when confirmation is thin.",
                regime="high_volatility",
                category="anti_pattern",
                support_count=4,
                evidence_journal_ids=[8, 10, 11],
                hypothesis=False,
                apply_when=MemoryCondition(
                    regime=["high_volatility"],
                    volatility=["high"],
                    trend=["up"],
                ),
                ignore_when=["Ignore when breakout retest holds with rising volume."],
            ),
        ]

        entries = self.updater._prepare_memory_entries(stats, lessons)

        self.assertEqual(len(entries), 1)
        self.assertGreaterEqual(entries[0]["metrics"]["support"], 4)
        self.assertGreaterEqual(entries[0]["metrics"]["merge_count"], 2)
        self.assertGreaterEqual(len(entries[0]["metrics"]["evidence_journal_ids"]), 4)

    def test_reconcile_long_term_memory_entries_retires_stale_and_revives_matching_memory(self):
        now = datetime(2026, 3, 7, 12, 0, tzinfo=UTC)
        stale_timestamp = (now - timedelta(days=95)).isoformat()
        revive_timestamp = (now - timedelta(days=40)).isoformat()
        existing = [
            {
                "id": 11,
                "timestamp": stale_timestamp,
                "rule": "Fade spikes only when trend is flat.",
                "version": 2,
                "metrics": {
                    "category": "risk_control",
                    "regime": "flat_medium",
                    "apply_when": {"regime": ["flat_medium"], "volatility": ["medium"], "trend": ["flat"]},
                    "ignore_when": ["Ignore in strong trend continuation."],
                    "support": 2,
                    "evidence_journal_ids": [1, 2],
                    "confidence_score": 0.28,
                    "validity_score": 0.31,
                    "freshness_score": 0.15,
                    "canonical_key": "risk_control|flat_medium|medium|flat|fade spikes only when trend is flat",
                    "lifecycle_status": "active",
                    "retired": False,
                },
            },
            {
                "id": 12,
                "timestamp": revive_timestamp,
                "rule": "Avoid thin-confirmation breakout chases.",
                "version": 3,
                "metrics": {
                    "category": "anti_pattern",
                    "regime": "high_volatility",
                    "apply_when": {"regime": ["high_volatility"], "volatility": ["high"], "trend": ["up"]},
                    "ignore_when": ["Ignore when volume confirms the retest."],
                    "support": 3,
                    "evidence_journal_ids": [3, 4, 5],
                    "confidence_score": 0.44,
                    "validity_score": 0.36,
                    "canonical_key": "anti_pattern|high_volatility|high|up|avoid thin confirmation breakout chases",
                    "lifecycle_status": "retired",
                    "retired": True,
                    "retirement_reason": "stale_low_confidence",
                    "revive_count": 1,
                },
            },
        ]
        new_entries = [
            {
                "rule": "Avoid thin confirmation breakout chases after a loss.",
                "version": 1,
                "metrics": {
                    "category": "anti_pattern",
                    "regime": "high_volatility",
                    "apply_when": {"regime": ["high_volatility"], "volatility": ["high"], "trend": ["up"]},
                    "ignore_when": ["Ignore when volume confirms the retest."],
                    "support": 5,
                    "evidence_journal_ids": [9, 10, 11],
                    "confidence_score": 0.74,
                    "validity_score": 0.74,
                    "canonical_key": "anti_pattern|high_volatility|high|up|avoid thin confirmation breakout chases",
                    "lifecycle_status": "active",
                    "retired": False,
                },
            }
        ]

        reconciled = self.updater._reconcile_long_term_memory_entries(new_entries, existing, now)

        self.assertEqual(len(reconciled), 2)
        revived = next(item for item in reconciled if item.get("id") == 12)
        retired = next(item for item in reconciled if item.get("id") == 11)
        self.assertFalse(revived["metrics"]["retired"])
        self.assertEqual(revived["metrics"]["lifecycle_status"], "active")
        self.assertGreaterEqual(revived["metrics"]["revive_count"], 2)
        self.assertTrue(retired["metrics"]["retired"])
        self.assertEqual(retired["metrics"]["retirement_reason"], "stale_timeout")


if __name__ == "__main__":
    unittest.main()
