import unittest
import time

from main import TradingOrchestrator
from core.models import AccountState, MarketSnapshot, PositionState


class DummyRiskEngine:
    policy = {
        "intervals": {
            "strategy_loop_minutes": 60,
            "position_loop_minutes": 15,
            "trigger_loop_minutes": 5,
        },
        "adaptive_loop": {
            "trigger_min_duration_minutes": 15,
            "trigger_clear_cycles": 3,
            "position_min_duration_minutes": 30,
            "flat_clear_cycles": 4,
            "recent_execution_window_minutes": 20,
            "recent_close_window_minutes": 20,
            "poll_seconds": 1,
        },
        "risk_limits": {
            "daily_max_loss_pct": 3.0,
            "max_consecutive_losses": 3,
        },
        "execution_guards": {
            "loop_lock_ttl_seconds": 90,
            "decision_cooldown_minutes": 7,
            "entry_mutex_ttl_seconds": 120,
            "unknown_order_block_minutes": 25,
            "recovery_sweep_minutes": 3,
        },
        "context_budget": {
            "journal_fetch_limit": 20,
            "journal_keep_limit": 4,
            "memory_keep_limit": 5,
            "similar_case_keep_limit": 3,
            "max_rationale_items_per_journal": 1,
            "max_evidence_per_rationale_item": 1,
            "max_ignore_when_per_memory": 1,
        },
        "market_data_cache": {
            "ticker_ttl_ms": 7000,
            "order_book_ttl_ms": 8000,
            "funding_ttl_ms": 180000,
            "open_interest_ttl_ms": 45000,
        },
    }


class TestTradingOrchestrator(unittest.TestCase):
    def _make_orchestrator(self):
        orchestrator = TradingOrchestrator.__new__(TradingOrchestrator)
        orchestrator.risk_engine = DummyRiskEngine()
        orchestrator._initialize_runtime_state()
        return orchestrator

    def test_build_execution_guards_reads_policy_overrides(self):
        orchestrator = self._make_orchestrator()

        guards = orchestrator._build_execution_guards()

        self.assertEqual(guards["loop_lock_ttl_seconds"], 90)
        self.assertEqual(guards["decision_cooldown_minutes"], 7)
        self.assertEqual(guards["entry_mutex_ttl_seconds"], 120)
        self.assertEqual(guards["unknown_order_block_minutes"], 25)
        self.assertEqual(guards["recovery_sweep_minutes"], 3)

    def test_build_context_budget_reads_policy_overrides(self):
        orchestrator = self._make_orchestrator()

        budget = orchestrator._build_context_budget()

        self.assertEqual(budget["journal_fetch_limit"], 20)
        self.assertEqual(budget["journal_keep_limit"], 4)
        self.assertEqual(budget["memory_keep_limit"], 5)
        self.assertEqual(budget["similar_case_keep_limit"], 3)
        self.assertEqual(budget["max_rationale_items_per_journal"], 1)
        self.assertEqual(budget["max_evidence_per_rationale_item"], 1)
        self.assertEqual(budget["max_ignore_when_per_memory"], 1)

    def test_build_market_data_cache_config_maps_policy_keys_to_collector_keys(self):
        orchestrator = self._make_orchestrator()

        cache_config = orchestrator._build_market_data_cache_config()

        self.assertEqual(
            cache_config,
            {
                "ticker": 7000,
                "order_book": 8000,
                "funding_rate": 180000,
                "open_interest": 45000,
            },
        )

    def test_select_recent_journal_context_prioritizes_relevant_rows_and_compacts(self):
        orchestrator = self._make_orchestrator()
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=120.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
            )
        }
        rows = [
            {
                "journal_id": 1,
                "decision": "HOLD",
                "rationale": ["Waiting"],
                "rationale_items": [],
                "risk_reason": "Safe action.",
                "risk_mode": "normal",
                "loss_signal": False,
                "trigger_symbols": [],
                "risk_trigger_symbols": [],
                "summary": "HOLD: Waiting",
            },
            {
                "journal_id": 2,
                "decision": "BUY",
                "rationale": ["Momentum long"],
                "rationale_items": [
                    {
                        "summary": "BTC trigger aligned",
                        "why_it_matters": "Momentum+volume",
                        "trade_implication": "Enter long",
                        "failure_mode": "Breakdown",
                        "evidence": [
                            {
                                "signal": "volume_ratio",
                                "symbol": "BTCUSDT",
                                "timeframe": "15m",
                                "operator": "gte",
                                "observed_value": 2.1,
                                "threshold": 1.8,
                                "units": "ratio",
                                "source": "market_snapshots.BTCUSDT.timeframes.15m.volume_ratio",
                            },
                            {
                                "signal": "rsi_14",
                                "symbol": "BTCUSDT",
                                "timeframe": "15m",
                                "operator": "gte",
                                "observed_value": 58.0,
                                "threshold": 50.0,
                                "units": "index",
                                "source": "market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                            },
                        ],
                    }
                ],
                "risk_reason": "Approved",
                "risk_mode": "trigger",
                "loss_signal": True,
                "estimated_pnl_pct": -0.7,
                "trigger_symbols": ["BTCUSDT"],
                "risk_trigger_symbols": [],
                "summary": "BUY: Momentum long",
            },
        ]

        selected = orchestrator._select_recent_journal_context(
            rows=rows,
            market_snapshots=market_snapshots,
            risk_mode="trigger",
            limit=1,
            max_rationale_items=1,
            max_evidence_per_item=1,
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["journal_id"], 2)
        self.assertEqual(len(selected[0]["rationale_items"]), 1)
        self.assertEqual(len(selected[0]["rationale_items"][0]["evidence"]), 1)

    def test_select_long_term_memory_context_prioritizes_matching_rule_and_compacts(self):
        orchestrator = self._make_orchestrator()
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=120.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
            )
        }
        market_snapshots["BTCUSDT"].regime.trend_alignment = "aligned_up"
        market_snapshots["BTCUSDT"].regime.volatility_regime = "high"
        rows = [
            {
                "rule": "For BTCUSDT, avoid low-evidence breakout entries in high volatility.",
                "category": "risk_control",
                "status": "rule",
                "support": 6,
                "regime": "trigger",
                "apply_when": {
                    "regime": ["trigger"],
                    "volatility": ["high"],
                    "trend": ["aligned_up"],
                },
                "ignore_when": ["Ignore when confirmation quality is high.", "Ignore when drawdown is zero."],
                "evidence_journal_ids": [1, 2, 3, 4],
                "metrics": {
                    "candidate_lessons": [{"seed": "large payload should be dropped"}],
                    "support": 6,
                },
            },
            {
                "rule": "Generic observation with weak support.",
                "category": "lesson",
                "status": "hypothesis",
                "support": 1,
                "regime": "general",
                "apply_when": {
                    "regime": ["general"],
                    "volatility": ["normal"],
                    "trend": ["mixed"],
                },
                "ignore_when": ["n/a"],
                "evidence_journal_ids": [],
                "metrics": {},
            },
        ]

        selected = orchestrator._select_long_term_memory_context(
            rows=rows,
            market_snapshots=market_snapshots,
            risk_mode="trigger",
            limit=1,
            max_ignore_when=1,
        )

        self.assertEqual(len(selected), 1)
        self.assertIn("BTCUSDT", selected[0]["rule"])
        self.assertEqual(len(selected[0]["ignore_when"]), 1)
        self.assertNotIn("candidate_lessons", selected[0]["metrics"])

    def test_build_similar_past_cases_applies_limit(self):
        class DummyRagEngine:
            def get_similar_cases_batch(self, *_args, **_kwargs):
                return {
                    "BTCUSDT": ["c1", "c2", "c3"],
                    "ETHUSDT": ["c4", "c5"],
                }

        orchestrator = self._make_orchestrator()
        orchestrator.rag_engine = DummyRagEngine()
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=120.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
                is_opportunity_trigger=True,
            ),
            "ETHUSDT": MarketSnapshot(
                symbol="ETHUSDT",
                current_price=2000.0,
                atr_15m=20.0,
                atr_1h=40.0,
                trend_1h="UP",
                recent_volume_surge=False,
                is_opportunity_trigger=True,
            ),
        }

        cases = orchestrator._build_similar_past_cases(
            market_snapshots=market_snapshots,
            account_state=AccountState(total_balance=1000.0, available_balance=900.0),
            risk_state={},
            is_trigger_mode=True,
            limit=3,
        )

        self.assertEqual(len(cases), 3)

    def test_deterministic_skip_when_no_exposure_and_entries_blocked(self):
        orchestrator = self._make_orchestrator()

        reason = orchestrator._get_deterministic_skip_reason(
            account_state=AccountState(
                total_balance=1000.0,
                available_balance=1000.0,
                positions=[],
                open_orders=[],
                open_orders_count=0,
            ),
            market_snapshots={
                "BTCUSDT": MarketSnapshot(
                    symbol="BTCUSDT",
                    current_price=30000.0,
                    atr_15m=120.0,
                    atr_1h=240.0,
                    trend_1h="UP",
                    recent_volume_surge=False,
                )
            },
            risk_state={"cooldown_active": True, "cooldown_remaining_minutes": 10},
            risk_mode="cooldown",
            risk_profile={"allowed_actions": ["HOLD", "REDUCE", "EXIT"]},
        )

        self.assertEqual(reason, "cooldown_active")

    def test_deterministic_skip_does_not_block_only_wide_spread_context(self):
        orchestrator = self._make_orchestrator()

        reason = orchestrator._get_deterministic_skip_reason(
            account_state=AccountState(
                total_balance=1000.0,
                available_balance=1000.0,
                positions=[],
                open_orders=[],
                open_orders_count=0,
            ),
            market_snapshots={
                "BTCUSDT": MarketSnapshot(
                    symbol="BTCUSDT",
                    current_price=30000.0,
                    atr_15m=120.0,
                    atr_1h=240.0,
                    trend_1h="UP",
                    recent_volume_surge=False,
                    risk_trigger_reasons=["wide_spread"],
                ),
                "ETHUSDT": MarketSnapshot(
                    symbol="ETHUSDT",
                    current_price=2000.0,
                    atr_15m=20.0,
                    atr_1h=40.0,
                    trend_1h="UP",
                    recent_volume_surge=False,
                    risk_trigger_reasons=["wide_spread"],
                ),
            },
            risk_state={},
            risk_mode="normal",
            risk_profile={"allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"]},
        )

        self.assertIsNone(reason)

    def test_deterministic_skip_does_not_apply_when_position_exists(self):
        orchestrator = self._make_orchestrator()

        reason = orchestrator._get_deterministic_skip_reason(
            account_state=AccountState(
                total_balance=1000.0,
                available_balance=900.0,
                positions=[
                    PositionState(
                        symbol="BTCUSDT",
                        side="LONG",
                        size=0.1,
                        entry_price=30000.0,
                        unrealized_pnl_pct=0.0,
                    )
                ],
                open_orders=[],
                open_orders_count=0,
            ),
            market_snapshots={},
            risk_state={"cooldown_active": True},
            risk_mode="cooldown",
            risk_profile={"allowed_actions": ["HOLD", "REDUCE", "EXIT"]},
        )

        self.assertIsNone(reason)

    def test_determine_desired_loop_mode_prefers_trigger_then_management_then_idle(self):
        orchestrator = self._make_orchestrator()
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=900.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000.0,
                    unrealized_pnl_pct=0.0,
                )
            ],
            open_orders=[],
            open_orders_count=0,
        )

        desired_trigger = orchestrator._determine_desired_loop_mode(
            {"trigger_active": True, "final_account_state": account_state, "has_unknown_orders": False}
        )
        desired_management = orchestrator._determine_desired_loop_mode(
            {"trigger_active": False, "final_account_state": account_state, "has_unknown_orders": False}
        )
        desired_idle = orchestrator._determine_desired_loop_mode(
            {
                "trigger_active": False,
                "final_account_state": AccountState(
                    total_balance=1000.0,
                    available_balance=1000.0,
                    positions=[],
                    open_orders=[],
                    open_orders_count=0,
                ),
                "has_unknown_orders": False,
            }
        )

        self.assertEqual(desired_trigger, "5m")
        self.assertEqual(desired_management, "15m")
        self.assertEqual(desired_idle, "1h")

    def test_trigger_mode_downgrade_requires_min_hold_and_clear_streak(self):
        orchestrator = self._make_orchestrator()
        orchestrator.active_loop_mode = "5m"
        orchestrator.active_loop_mode_since = time.time() - (16 * 60)

        mode_first = orchestrator._apply_adaptive_loop_mode("15m", time.time())
        mode_second = orchestrator._apply_adaptive_loop_mode("15m", time.time())
        mode_third = orchestrator._apply_adaptive_loop_mode("15m", time.time())

        self.assertEqual(mode_first, "5m")
        self.assertEqual(mode_second, "5m")
        self.assertEqual(mode_third, "15m")

    def test_position_mode_downgrade_requires_min_hold_and_flat_streak(self):
        orchestrator = self._make_orchestrator()
        orchestrator.active_loop_mode = "15m"
        orchestrator.active_loop_mode_since = time.time() - (31 * 60)

        observed = [
            orchestrator._apply_adaptive_loop_mode("1h", time.time())
            for _ in range(4)
        ]

        self.assertEqual(observed[:3], ["15m", "15m", "15m"])
        self.assertEqual(observed[3], "1h")

    def test_position_management_cycle_profile_blocks_new_entries(self):
        orchestrator = self._make_orchestrator()
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=900.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000.0,
                    unrealized_pnl_pct=0.0,
                )
            ],
            open_orders=[],
            open_orders_count=0,
        )

        effective_profile = orchestrator._build_effective_risk_profile(
            "15m",
            account_state=account_state,
            risk_profile={"allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"]},
        )

        self.assertEqual(effective_profile["cycle_role"], "position_management")
        self.assertEqual(effective_profile["allowed_actions"], ["HOLD", "REDUCE", "EXIT"])
