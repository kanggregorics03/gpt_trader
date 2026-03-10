import unittest

from batch.memory_updater import MemoryUpdater


class TestExecutionPolicyLearning(unittest.TestCase):
    def test_execution_policy_summary_tracks_cancel_outcomes(self):
        updater = MemoryUpdater.__new__(MemoryUpdater)
        summary = updater._build_execution_policy_summary(
            [
                {
                    "symbol": "BTCUSDT",
                    "action_type": "cancel",
                    "order_type": "CANCEL",
                    "execution_profile": "cancel_live_order",
                    "status": "canceled",
                },
                {
                    "symbol": "BTCUSDT",
                    "action_type": "cancel",
                    "order_type": "CANCEL",
                    "execution_profile": "cancel_live_order",
                    "status": "timeout_unresolved",
                },
            ]
        )

        self.assertEqual(summary["cancel_count"], 2)
        self.assertEqual(summary["cancel_success_rate"], 0.5)
        self.assertEqual(summary["cancel_timeout_rate"], 0.5)

    def test_memory_updater_builds_position_candidate_policy(self):
        updater = MemoryUpdater.__new__(MemoryUpdater)
        candidates = updater._build_candidate_policies(
            stats_package_seed={
                "candidate_lessons": [],
                "trade_case_patterns": {"failure_reason_counts": {"adverse_price_follow_through": {"count": 3}}},
            },
            trade_cases=[],
        )
        self.assertTrue(any(item["domain"] == "position_15m" for item in candidates))

    def test_memory_updater_builds_position_candidate_with_invalidation_controls(self):
        updater = MemoryUpdater.__new__(MemoryUpdater)
        candidates = updater._build_candidate_policies(
            stats_package_seed={
                "candidate_lessons": [],
                "trade_case_patterns": {"failure_reason_counts": {}},
            },
            trade_cases=[
                {
                    "status": "closed",
                    "risk_mode": "normal",
                    "estimated_pnl_pct": -1.1,
                    "risk_trigger_reasons": ["wide_spread", "elevated_funding"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {
                            "distance_to_invalid_level": 0.62,
                            "spread_percentile_1h": 92.0,
                            "depth_percentile_1h": 11.0,
                        },
                    },
                },
                {
                    "status": "closed",
                    "risk_mode": "normal",
                    "estimated_pnl_pct": -0.7,
                    "risk_trigger_reasons": ["wide_spread", "elevated_funding"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {
                            "distance_to_invalid_level": 0.55,
                            "spread_percentile_1h": 90.0,
                            "depth_percentile_1h": 12.0,
                        },
                    },
                },
                {
                    "status": "closed",
                    "risk_mode": "normal",
                    "estimated_pnl_pct": 0.1,
                    "risk_trigger_reasons": ["wide_spread"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {
                            "distance_to_invalid_level": 0.9,
                            "spread_percentile_1h": 89.0,
                            "depth_percentile_1h": 13.0,
                        },
                    },
                },
            ],
        )

        position_candidate = next(
            item
            for item in candidates
            if item["domain"] == "position_15m" and item["metrics"].get("seed_type") == "trade_case_position"
        )
        self.assertEqual(position_candidate["regime_key"], "aligned_up|high")
        self.assertLess(position_candidate["config"]["invalidation_reduce_distance_atr"], 0.75)
        self.assertLess(position_candidate["config"]["microstructure_exit_spread_percentile"], 90.0)
        self.assertGreater(position_candidate["config"]["microstructure_exit_depth_percentile"], 12.0)
        self.assertEqual(position_candidate["config"]["microstructure_exit_confirmations"], 2)

    def test_memory_updater_builds_alpha_candidate_policy_from_trade_cases(self):
        updater = MemoryUpdater.__new__(MemoryUpdater)
        candidates = updater._build_candidate_policies(
            stats_package_seed={
                "candidate_lessons": [],
                "trade_case_patterns": {"failure_reason_counts": {}},
            },
            trade_cases=[
                {
                    "status": "closed",
                    "entry_decision": "BUY",
                    "estimated_pnl_pct": -1.1,
                    "payload": {"feature_snapshot": {"spread_percentile_1h": 88.0, "volume_ratio_15m": 0.9}},
                },
                {
                    "status": "closed",
                    "entry_decision": "BUY",
                    "estimated_pnl_pct": -0.8,
                    "payload": {"feature_snapshot": {"spread_percentile_1h": 84.0, "volume_ratio_15m": 1.0}},
                },
                {
                    "status": "closed",
                    "entry_decision": "BUY",
                    "estimated_pnl_pct": 0.6,
                    "payload": {"feature_snapshot": {"spread_percentile_1h": 42.0, "volume_ratio_15m": 1.8}},
                },
            ],
        )
        self.assertTrue(any(item["domain"] == "alpha_1h" for item in candidates))
        spread_candidate = next(item for item in candidates if item["policy_name"] == "alpha_max_spread_percentile")
        self.assertLess(spread_candidate["config"]["max_spread_percentile_1h"], 88.0)

    def test_memory_updater_evaluates_alpha_shadow_as_avoided_loss(self):
        updater = MemoryUpdater.__new__(MemoryUpdater)
        updater.state_store = type("Store", (), {})()
        candidates = [
            {
                "id": 11,
                "domain": "alpha_1h",
                "policy_type": "entry_filter",
                "metrics": {"support_count": 4, "utility_score": 0.0},
            }
        ]

        evaluations = updater._evaluate_candidate_policies(
            candidates,
            shadow_decisions=[
                {
                    "candidate_policy_id": 11,
                    "divergence_type": "entry_block",
                    "outcome_estimated_pnl_pct": -1.25,
                }
            ],
        )

        self.assertEqual(evaluations[0]["evaluation_scope"], "shadow")
        self.assertEqual(evaluations[0]["score"], 1.25)

    def test_memory_updater_does_not_build_exposure_budget_candidate(self):
        updater = MemoryUpdater.__new__(MemoryUpdater)
        candidates = updater._build_candidate_policies(
            stats_package_seed={
                "candidate_lessons": [],
                "trade_case_patterns": {"failure_reason_counts": {}},
            },
            trade_cases=[
                {
                    "status": "closed",
                    "risk_mode": "normal",
                    "entry_decision": "BUY",
                    "estimated_pnl_pct": -0.9,
                    "slippage_bps": 5.0,
                    "payload": {"regime_tags": ["trend:aligned_up", "volatility:high"], "feature_snapshot": {}},
                },
                {
                    "status": "closed",
                    "risk_mode": "normal",
                    "entry_decision": "BUY",
                    "estimated_pnl_pct": -0.6,
                    "slippage_bps": 4.0,
                    "payload": {"regime_tags": ["trend:aligned_up", "volatility:high"], "feature_snapshot": {}},
                },
                {
                    "status": "closed",
                    "risk_mode": "normal",
                    "entry_decision": "BUY",
                    "estimated_pnl_pct": 0.1,
                    "slippage_bps": 4.5,
                    "payload": {"regime_tags": ["trend:aligned_up", "volatility:high"], "feature_snapshot": {}},
                },
            ],
        )
        self.assertFalse(any(item["domain"] == "exposure_budget" for item in candidates))

    def test_memory_updater_builds_safety_candidate(self):
        updater = MemoryUpdater.__new__(MemoryUpdater)
        candidates = updater._build_candidate_policies(
            stats_package_seed={
                "candidate_lessons": [],
                "trade_case_patterns": {"failure_reason_counts": {}},
            },
            trade_cases=[
                {
                    "status": "closed",
                    "loop_mode": "5m",
                    "safety_mode": True,
                    "risk_mode": "normal",
                    "estimated_pnl_pct": -1.2,
                    "risk_trigger_reasons": ["wide_spread", "thin_depth"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {"spread_percentile_1h": 82.0, "depth_percentile_1h": 18.0},
                    },
                },
                {
                    "status": "closed",
                    "loop_mode": "5m",
                    "safety_mode": True,
                    "risk_mode": "normal",
                    "estimated_pnl_pct": -0.8,
                    "risk_trigger_reasons": ["wide_spread"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {"spread_percentile_1h": 79.0, "depth_percentile_1h": 20.0},
                    },
                },
                {
                    "status": "closed",
                    "loop_mode": "5m",
                    "safety_mode": True,
                    "risk_mode": "normal",
                    "estimated_pnl_pct": 0.1,
                    "risk_trigger_reasons": ["wide_spread", "thin_depth"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {"spread_percentile_1h": 81.0, "depth_percentile_1h": 17.0},
                    },
                },
            ],
        )

        safety_candidate = next(item for item in candidates if item["domain"] == "safety_5m")
        self.assertEqual(safety_candidate["regime_key"], "aligned_up|high")
        self.assertGreater(safety_candidate["config"]["loss_exit_pct"], -0.7)

    def test_memory_updater_excludes_safety_cases_from_position_candidates(self):
        updater = MemoryUpdater.__new__(MemoryUpdater)
        candidates = updater._build_candidate_policies(
            stats_package_seed={
                "candidate_lessons": [],
                "trade_case_patterns": {"failure_reason_counts": {}},
            },
            trade_cases=[
                {
                    "status": "closed",
                    "loop_mode": "5m",
                    "safety_mode": True,
                    "risk_mode": "normal",
                    "estimated_pnl_pct": -1.1,
                    "risk_trigger_reasons": ["wide_spread", "elevated_funding"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {
                            "distance_to_invalid_level": 0.62,
                            "spread_percentile_1h": 92.0,
                            "depth_percentile_1h": 11.0,
                        },
                    },
                },
                {
                    "status": "closed",
                    "loop_mode": "5m",
                    "safety_mode": True,
                    "risk_mode": "normal",
                    "estimated_pnl_pct": -0.7,
                    "risk_trigger_reasons": ["wide_spread", "elevated_funding"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {
                            "distance_to_invalid_level": 0.55,
                            "spread_percentile_1h": 90.0,
                            "depth_percentile_1h": 12.0,
                        },
                    },
                },
                {
                    "status": "closed",
                    "loop_mode": "5m",
                    "safety_mode": True,
                    "risk_mode": "normal",
                    "estimated_pnl_pct": 0.1,
                    "risk_trigger_reasons": ["wide_spread"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {
                            "distance_to_invalid_level": 0.9,
                            "spread_percentile_1h": 89.0,
                            "depth_percentile_1h": 13.0,
                        },
                    },
                },
            ],
        )

        self.assertFalse(
            any(
                item["domain"] == "position_15m" and item["metrics"].get("seed_type") == "trade_case_position"
                for item in candidates
            )
        )

    def test_memory_updater_evaluates_safety_shadow_as_avoided_loss(self):
        updater = MemoryUpdater.__new__(MemoryUpdater)
        updater.state_store = type("Store", (), {})()
        evaluations = updater._evaluate_candidate_policies(
            [
                {
                    "id": 21,
                    "domain": "safety_5m",
                    "policy_type": "execution_safety_fsm",
                    "metrics": {"support_count": 4, "utility_score": 0.0},
                }
            ],
            shadow_decisions=[
                {
                    "candidate_policy_id": 21,
                    "divergence_type": "risk_exit",
                    "outcome_estimated_pnl_pct": -1.1,
                }
            ],
        )

        self.assertEqual(evaluations[0]["evaluation_scope"], "shadow")
        self.assertEqual(evaluations[0]["score"], 1.1)

    def test_memory_updater_does_not_reward_legacy_safety_risk_reduce_shadow(self):
        updater = MemoryUpdater.__new__(MemoryUpdater)
        updater.state_store = type("Store", (), {})()
        evaluations = updater._evaluate_candidate_policies(
            [
                {
                    "id": 22,
                    "domain": "safety_5m",
                    "policy_type": "execution_safety_fsm",
                    "metrics": {"support_count": 4, "utility_score": 0.0},
                }
            ],
            shadow_decisions=[
                {
                    "candidate_policy_id": 22,
                    "divergence_type": "risk_reduce",
                    "outcome_estimated_pnl_pct": -1.1,
                }
            ],
        )

        self.assertEqual(evaluations[0]["evaluation_scope"], "shadow")
        self.assertEqual(evaluations[0]["score"], -1.1)


if __name__ == "__main__":
    unittest.main()
