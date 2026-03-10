import unittest

from batch.memory_updater import MemoryUpdater
from db.state_store import StateStore


class TestExecutionPolicyLearning(unittest.TestCase):
    def test_memory_updater_builds_execution_policy_recommendations(self):
        updater = MemoryUpdater.__new__(MemoryUpdater)
        rows = [
            {
                "symbol": "BTCUSDT",
                "loop_mode": "1h",
                "risk_mode": "normal",
                "action_type": "entry",
                "urgency_bucket": "low",
                "liquidity_bucket": "good",
                "execution_profile": "limit_passive",
                "order_type": "LIMIT",
                "fill_ratio": 1.0,
                "slippage_bps": 1.0,
                "outcome_estimated_pnl_pct": 0.6,
                "status": "filled",
            },
            {
                "symbol": "BTCUSDT",
                "loop_mode": "1h",
                "risk_mode": "normal",
                "action_type": "entry",
                "urgency_bucket": "low",
                "liquidity_bucket": "good",
                "execution_profile": "market_urgent",
                "order_type": "MARKET",
                "fill_ratio": 1.0,
                "slippage_bps": 8.5,
                "outcome_estimated_pnl_pct": 0.1,
                "status": "filled",
            },
        ]
        recommendations = updater._build_execution_policy_recommendations(rows)
        self.assertTrue(recommendations)
        target = next(item for item in recommendations if item["symbol"] == "BTCUSDT")
        self.assertEqual(target["recommended_execution_profile"], "limit_passive")
        self.assertEqual(target["recommended_order_type"], "LIMIT")

    def test_state_store_round_trips_execution_policy_profiles(self):
        store = StateStore(":memory:")
        try:
            store.upsert_execution_policy_profile(
                {
                    "symbol": "BTCUSDT",
                    "loop_mode": "1h",
                    "risk_mode": "normal",
                    "action_type": "entry",
                    "urgency_bucket": "low",
                    "liquidity_bucket": "good",
                    "recommended_order_type": "LIMIT",
                    "recommended_execution_profile": "limit_passive",
                    "metrics": {"sample_count": 7, "utility_score": 42.0},
                }
            )
            row = store.get_execution_policy_profile(
                symbol="BTCUSDT",
                loop_mode="1h",
                action_type="entry",
                urgency_bucket="low",
                liquidity_bucket="good",
                risk_mode="normal",
            )
            self.assertEqual(row["recommended_execution_profile"], "limit_passive")
            self.assertEqual(row["metrics"]["sample_count"], 7)
        finally:
            store.close()

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
            execution_policy_recommendations=[],
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
            execution_policy_recommendations=[],
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
            execution_policy_recommendations=[],
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

    def test_memory_updater_builds_exposure_budget_candidate_for_normal_mode(self):
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
            execution_policy_recommendations=[],
        )

        exposure_candidate = next(item for item in candidates if item["domain"] == "exposure_budget")
        self.assertEqual(exposure_candidate["regime_key"], "normal|aligned_up|high")
        self.assertLess(exposure_candidate["config"]["max_order_notional_pct_balance"], 100.0)

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
                    "risk_mode": "trigger",
                    "estimated_pnl_pct": -1.2,
                    "risk_trigger_reasons": ["wide_spread", "thin_depth"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {"spread_percentile_1h": 82.0, "depth_percentile_1h": 18.0},
                    },
                },
                {
                    "status": "closed",
                    "risk_mode": "trigger",
                    "estimated_pnl_pct": -0.8,
                    "risk_trigger_reasons": ["wide_spread"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {"spread_percentile_1h": 79.0, "depth_percentile_1h": 20.0},
                    },
                },
                {
                    "status": "closed",
                    "risk_mode": "trigger",
                    "estimated_pnl_pct": 0.1,
                    "risk_trigger_reasons": ["wide_spread", "thin_depth"],
                    "payload": {
                        "regime_tags": ["trend:aligned_up", "volatility:high"],
                        "feature_snapshot": {"spread_percentile_1h": 81.0, "depth_percentile_1h": 17.0},
                    },
                },
            ],
            execution_policy_recommendations=[],
        )

        safety_candidate = next(item for item in candidates if item["domain"] == "safety_5m")
        self.assertEqual(safety_candidate["regime_key"], "aligned_up|high")
        self.assertGreater(safety_candidate["config"]["loss_exit_pct"], -0.7)

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
