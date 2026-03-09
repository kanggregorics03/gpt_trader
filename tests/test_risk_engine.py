import unittest
import gc
import time
import yaml
from pathlib import Path
from unittest.mock import patch
from core.risk_engine import RiskEngine
from core.models import (
    AccountState,
    DerivativesSnapshot,
    EvidenceItem,
    MarketSnapshot,
    OpenOrderState,
    OrderIntent,
    PositionState,
    RationaleItem,
    RiskStateSnapshot,
    TradeIntent,
)

class TestRiskEngine(unittest.TestCase):
    POLICY_SUMMARY_SNAPSHOT_PATH = Path("tests/snapshots/policy_summary_snapshot.yaml")

    def setUp(self):
        # Create a dummy policy.yaml for testing
        self.dummy_policy_path = Path("test_policy.yaml")
        policy_data = {
            "trading": {
                "symbols": ["BTCUSDT"],
                "max_leverage": 3,
                "order_type": "market"
            },
            "risk_limits": {
                "daily_max_loss_pct": 3.0,
                "drawdown_activate_pct": 1.5,
                "max_consecutive_losses": 3,
                "cooldown_minutes": 30,
                "max_open_positions": 2,
                "mode_profiles": {
                    "normal": {
                        "max_open_positions": 2,
                        "max_order_notional_pct_balance": 100,
                        "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"],
                    },
                    "trigger": {
                        "max_open_positions": 1,
                        "max_order_notional_pct_balance": 30,
                        "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"],
                        "block_additional_entries_when_position_exists": True,
                    },
                    "cooldown": {
                        "max_open_positions": 2,
                        "max_order_notional_pct_balance": 0,
                        "allowed_actions": ["HOLD", "REDUCE", "EXIT"],
                    },
                    "drawdown": {
                        "max_open_positions": 1,
                        "max_order_notional_pct_balance": 20,
                        "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"],
                    },
                },
            }
        }
        with open(self.dummy_policy_path, "w") as f:
            yaml.dump(policy_data, f)
            
        self.engine = RiskEngine(config_path="test_policy.yaml")

    def test_policy_normalizes_to_single_symbol_project(self):
        self.assertEqual(self.engine.get_trading_symbols(), ["BTCUSDT"])
        self.assertEqual(self.engine.policy["risk_limits"]["max_open_positions"], 1)
        self.assertEqual(
            self.engine.policy["risk_limits"]["mode_profiles"]["normal"]["max_open_positions"],
            1,
        )

    def test_get_summary_returns_compact_policy_digest(self):
        summary = yaml.safe_load(self.engine.get_summary())

        self.assertEqual(summary["summary_version"], 2)
        self.assertEqual(summary["trading"]["symbols"], ["BTCUSDT"])
        self.assertEqual(summary["trading"]["allowed_order_types"], ["MARKET"])
        self.assertIn("mode_profiles", summary["risk_limits"])
        self.assertIn("execution_guards", summary)
        self.assertIn("stale_order_controls", summary)

    def test_prompt_policy_contract_registry_contains_critical_paths(self):
        registry = self.engine.get_prompt_policy_contract_registry()
        summary_paths = {item["summary_path"] for item in registry}

        self.assertIn("risk_limits.daily_max_loss_pct", summary_paths)
        self.assertIn("risk_limits.mode_profiles.trigger.max_order_notional_pct_balance", summary_paths)
        self.assertIn("execution_guards.symbol_reentry_loss_cooldown_minutes", summary_paths)

    def test_prompt_policy_contract_validation_fails_on_missing_critical_field(self):
        with patch.object(RiskEngine, "_build_policy_summary", return_value={"summary_version": 2}):
            with self.assertRaises(ValueError):
                RiskEngine(config_path="test_policy.yaml")

    def test_policy_yaml_prompt_digest_matches_snapshot(self):
        engine = RiskEngine(config_path="policy.yaml")
        expected = self.POLICY_SUMMARY_SNAPSHOT_PATH.read_text(encoding="utf-8")

        self.assertEqual(engine.get_summary(), expected)

    def tearDown(self):
        self.engine = None
        gc.collect()
        time.sleep(0.01)
        if self.dummy_policy_path.exists():
            self.dummy_policy_path.unlink(missing_ok=True)

    def test_hold_always_approved(self):
        intent = TradeIntent(
            decision="HOLD",
            orders=[],
            rationale=["Waiting"],
            risk_notes=[],
            confidence=0.5
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, _ = self.engine.evaluate_intent(intent, acc_state)
        self.assertTrue(approved)

    def test_reject_unapproved_symbol(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="DOGEUSDT", action="BUY", order_type="MARKET", quantity=100)],
            rationale=["Moon"],
            risk_notes=[],
            confidence=0.9
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(intent, acc_state)
        self.assertFalse(approved)
        self.assertIn("not in approved policy list", reason)

    def test_accepts_ccxt_unified_symbol_for_approved_policy_symbol(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTC/USDT:USDT", action="BUY", order_type="MARKET", quantity=0.01)],
            rationale=["Breakout"],
            risk_notes=[],
            confidence=0.8
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
        )

        self.assertTrue(approved)
        self.assertEqual(reason, "Approved")

    def test_reject_max_positions_exceeded(self):
        # Simulate having 2 open positions already
        pos1 = PositionState(symbol="ETHUSDT", side="LONG", size=1.0, entry_price=2000, unrealized_pnl_pct=0)
        pos2 = PositionState(symbol="SOLUSDT", side="SHORT", size=10.0, entry_price=100, unrealized_pnl_pct=0)
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[pos1, pos2], open_orders_count=0)
        
        # Try to open a new one
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.1)],
            rationale=["Setup"],
            risk_notes=[],
            confidence=0.8
        )
        approved, reason = self.engine.evaluate_intent(intent, acc_state)
        self.assertFalse(approved)
        self.assertIn("cannot open more than", reason.lower() if reason else "")

    def test_reject_entry_during_cooldown(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.1)],
            rationale=["Setup"],
            risk_notes=[],
            confidence=0.8
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            risk_state={"cooldown_active": True, "cooldown_remaining_minutes": 12},
        )
        self.assertFalse(approved)
        self.assertIn("cooldown", reason.lower())

    def test_cooldown_profile_blocks_entry_actions(self):
        intent = TradeIntent(
            decision="SELL",
            orders=[OrderIntent(symbol="BTCUSDT", action="SELL", order_type="MARKET", quantity=0.1)],
            rationale=["Short setup"],
            risk_notes=[],
            confidence=0.7
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            risk_state={"cooldown_active": True, "cooldown_remaining_minutes": 5},
        )
        self.assertFalse(approved)
        self.assertIn("cooldown", reason.lower())

    def test_reject_entry_on_daily_loss_kill_switch(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.1)],
            rationale=["Setup"],
            risk_notes=[],
            confidence=0.8
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            risk_state={"daily_pnl_pct": -4.1},
        )
        self.assertFalse(approved)
        self.assertIn("kill switch", reason.lower())

    def test_reject_entry_on_daily_loss_kill_switch_with_risk_state_snapshot_model(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.1)],
            rationale=["Setup"],
            risk_notes=[],
            confidence=0.8
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            risk_state=RiskStateSnapshot(daily_pnl_pct=-4.1),
        )
        self.assertFalse(approved)
        self.assertIn("kill switch", reason.lower())

    def test_reject_limit_order_when_policy_requires_market(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="LIMIT", quantity=0.1, price=29000)],
            rationale=["Setup"],
            risk_notes=[],
            confidence=0.8
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(intent, acc_state)
        self.assertFalse(approved)
        self.assertIn("order type", reason.lower())

    def test_reject_when_projected_exposure_exceeds_max_leverage(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.2)],
            rationale=["Setup"],
            risk_notes=[],
            confidence=0.8
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 20000},
        )
        self.assertFalse(approved)
        self.assertIn("projected exposure", reason.lower())

    def test_trigger_profile_rejects_candidate_size_over_profile_cap(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.02)],
            rationale=["Momentum"],
            risk_notes=[],
            confidence=0.8
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            risk_state={},
            is_trigger_mode=True,
        )
        self.assertFalse(approved)
        self.assertIn("candidate size", reason.lower())

    def test_drawdown_mode_is_detected_before_hard_kill_switch(self):
        mode = self.engine.get_active_risk_mode(
            risk_state={"daily_pnl_pct": -1.8, "drawdown_active": True},
            is_trigger_mode=False,
        )
        profile = self.engine.get_active_risk_profile(
            risk_state={"daily_pnl_pct": -1.8, "drawdown_active": True},
            is_trigger_mode=False,
        )

        self.assertEqual(mode, "drawdown")
        self.assertEqual(profile["max_open_positions"], 1)

    def test_active_risk_mode_accepts_risk_state_snapshot_model(self):
        mode = self.engine.get_active_risk_mode(
            risk_state=RiskStateSnapshot(cooldown_active=True, cooldown_remaining_minutes=10),
            is_trigger_mode=False,
        )

        self.assertEqual(mode, "cooldown")

    def test_drawdown_profile_reduces_entry_size_for_confidence_and_failure_pattern(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.006)],
            rationale=["Drawdown recovery attempt"],
            risk_notes=[],
            confidence=0.8,
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            risk_state={"drawdown_active": True, "failure_pattern_score": 0.8},
        )

        self.assertFalse(approved)
        self.assertIn("candidate size", reason.lower())
        self.assertIn("profile_adjustments", reason.lower())

    def test_drawdown_size_cap_summary_exposes_profile_adjustments(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.002)],
            rationale=["Recovery entry"],
            risk_notes=[],
            confidence=0.78,
        )
        profile = self.engine.get_active_risk_profile(
            risk_state={"drawdown_active": True},
            is_trigger_mode=False,
        )

        summary = self.engine.get_effective_max_order_notional_pct_balance(
            intent=intent,
            active_profile=profile,
            risk_state={"failure_pattern_score": 0.75},
        )

        self.assertTrue(summary["adjusted"])
        self.assertLess(summary["effective_cap_pct"], summary["base_cap_pct"])
        self.assertIn("mode_entry_size_multiplier", summary["profile_issues"])
        self.assertIn("confidence_cap:0.55", summary["profile_issues"])
        self.assertIn("failure_pattern_score:0.75", summary["profile_issues"])

    def test_exit_is_still_safe_during_kill_switch(self):
        intent = TradeIntent(
            decision="EXIT",
            orders=[],
            rationale=["Emergency exit"],
            risk_notes=[],
            confidence=0.9
        )
        pos = PositionState(symbol="BTCUSDT", side="LONG", size=0.1, entry_price=30000, unrealized_pnl_pct=-2.0)
        acc_state = AccountState(total_balance=950, available_balance=900, positions=[pos], open_orders_count=0)
        approved, _ = self.engine.evaluate_intent(
            intent,
            acc_state,
            risk_state={"daily_pnl_pct": -5.0, "cooldown_active": True, "consecutive_losses": 4},
        )
        self.assertTrue(approved)

    def test_cancel_is_allowed_for_live_open_orders_during_cooldown(self):
        intent = TradeIntent(
            decision="CANCEL",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="CANCEL",
                    order_type="MARKET",
                    quantity=0.0,
                    target_order_id="order-1",
                    target_client_order_id="client-1",
                )
            ],
            rationale=["Cancel stale entry"],
            risk_notes=[],
            confidence=0.8,
        )
        acc_state = AccountState(
            total_balance=1000,
            available_balance=1000,
            positions=[],
            open_orders=[
                OpenOrderState(
                    symbol="BTCUSDT",
                    order_id="order-1",
                    client_order_id="client-1",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=0.1,
                )
            ],
            open_orders_count=1,
        )

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            risk_state={"cooldown_active": True, "cooldown_remaining_minutes": 10},
        )

        self.assertTrue(approved)
        self.assertEqual(reason, "Approved")

    def test_mixed_cancel_and_reprice_entry_is_approved(self):
        self.engine.policy["execution_policy"] = {"allowed_order_types": ["market", "limit"]}
        intent = TradeIntent(
            decision="REPLACE_ENTRY",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="CANCEL",
                    order_type="MARKET",
                    quantity=0.0,
                    target_order_id="order-1",
                    target_client_order_id="client-1",
                    replace_group_id="replace-1",
                    lineage_role="cancel",
                ),
                OrderIntent(
                    symbol="BTCUSDT",
                    action="BUY",
                    order_type="LIMIT",
                    quantity=0.01,
                    price=30000.0,
                    replace_group_id="replace-1",
                    lineage_role="replacement",
                    lineage_parent_order_id="order-1",
                    lineage_parent_client_order_id="client-1",
                    wait_for_cancel_confirmation=True,
                ),
            ],
            rationale=["Reprice stale entry"],
            risk_notes=[],
            confidence=0.8,
        )
        acc_state = AccountState(
            total_balance=1000,
            available_balance=1000,
            positions=[],
            open_orders=[
                OpenOrderState(
                    symbol="BTCUSDT",
                    order_id="order-1",
                    client_order_id="client-1",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=0.01,
                    price=30100.0,
                )
            ],
            open_orders_count=1,
        )

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            risk_state={},
        )

        self.assertTrue(approved)
        self.assertEqual(reason, "Approved")

    def test_policy_validation_rejects_inverted_reentry_cooldowns(self):
        invalid_policy_path = Path("test_invalid_policy.yaml")
        policy_data = {
            "trading": {"symbols": ["BTCUSDT"]},
            "risk_limits": {"max_open_positions": 1, "mode_profiles": {"normal": {"max_open_positions": 1}}},
            "execution_guards": {
                "symbol_reentry_close_cooldown_minutes": 20,
                "symbol_reentry_loss_cooldown_minutes": 10,
                "symbol_reentry_loss_threshold_pct": 0.0,
            },
        }
        with open(invalid_policy_path, "w") as f:
            yaml.dump(policy_data, f)

        try:
            with self.assertRaises(ValueError):
                RiskEngine(config_path=str(invalid_policy_path))
        finally:
            invalid_policy_path.unlink(missing_ok=True)

    def test_wide_spread_is_passed_as_advisory_not_hard_block(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.01)],
            rationale=["Breakout"],
            risk_notes=[],
            confidence=0.7,
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        market_snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            current_price=30000,
            atr_15m=120,
            atr_1h=240,
            trend_1h="UP",
            recent_volume_surge=True,
            is_risk_trigger=True,
            risk_trigger_reasons=["wide_spread"],
        )

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_snapshot=market_snapshot,
            market_prices={"BTCUSDT": 30000},
        )

        self.assertTrue(approved)
        self.assertIn("risk advisories", reason.lower())
        self.assertIn("wide_spread", reason.lower())

    def test_elevated_positive_funding_is_passed_as_advisory_not_hard_block(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.01)],
            rationale=["Momentum"],
            risk_notes=[],
            confidence=0.7,
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        market_snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            current_price=30000,
            atr_15m=120,
            atr_1h=240,
            trend_1h="UP",
            recent_volume_surge=True,
            is_risk_trigger=True,
            risk_trigger_reasons=["elevated_funding"],
            derivatives=DerivativesSnapshot(funding_rate=0.0008),
        )

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_snapshot=market_snapshot,
            market_prices={"BTCUSDT": 30000},
        )

        self.assertTrue(approved)
        self.assertIn("risk advisories", reason.lower())
        self.assertIn("elevated_funding_unfavorable", reason.lower())

    def test_elevated_positive_funding_allows_short_entries(self):
        intent = TradeIntent(
            decision="SELL",
            orders=[OrderIntent(symbol="BTCUSDT", action="SELL", order_type="MARKET", quantity=0.01)],
            rationale=["Crowded longs"],
            risk_notes=[],
            confidence=0.7,
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        market_snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            current_price=30000,
            atr_15m=120,
            atr_1h=240,
            trend_1h="DOWN",
            recent_volume_surge=True,
            is_risk_trigger=True,
            risk_trigger_reasons=["elevated_funding"],
            derivatives=DerivativesSnapshot(funding_rate=0.0008),
        )

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_snapshot=market_snapshot,
            market_prices={"BTCUSDT": 30000},
        )

        self.assertTrue(approved)
        self.assertEqual(reason, "Approved")

    def test_market_snapshots_apply_risk_triggers_per_order_symbol(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.01)],
            rationale=["Breakout"],
            risk_notes=[],
            confidence=0.7,
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000,
                atr_15m=120,
                atr_1h=240,
                trend_1h="UP",
                recent_volume_surge=True,
            ),
            "ETHUSDT": MarketSnapshot(
                symbol="ETHUSDT",
                current_price=2000,
                atr_15m=20,
                atr_1h=45,
                trend_1h="UP",
                recent_volume_surge=True,
                is_risk_trigger=True,
                risk_trigger_reasons=["wide_spread"],
            ),
        }

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_snapshots=market_snapshots,
            market_prices={"BTCUSDT": 30000, "ETHUSDT": 2000},
        )

        self.assertTrue(approved)
        self.assertEqual(reason, "Approved")

    def test_multiple_new_symbols_respect_max_open_positions(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[
                OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.01),
                OrderIntent(symbol="ETHUSDT", action="BUY", order_type="MARKET", quantity=0.2),
                OrderIntent(symbol="SOLUSDT", action="BUY", order_type="MARKET", quantity=2.0),
            ],
            rationale=["Portfolio allocation"],
            risk_notes=[],
            confidence=0.7,
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000, "ETHUSDT": 2000, "SOLUSDT": 100},
        )

        self.assertFalse(approved)
        self.assertIn("cannot open more than", reason.lower())

    def test_structured_evidence_quality_can_tighten_entry_size_cap(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.03)],
            rationale=["Momentum setup"],
            rationale_items=[
                RationaleItem(
                    summary="Single weak signal",
                    why_it_matters="Momentum may continue.",
                    trade_implication="Consider a cautious long.",
                    evidence=[
                        EvidenceItem(
                            signal="rsi_14",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="gte",
                            observed_value=62.0,
                            threshold=60.0,
                            units="index",
                            source="market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                        )
                    ],
                )
            ],
            risk_notes=[],
            confidence=0.8,
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
        )

        self.assertFalse(approved)
        self.assertIn("candidate size", reason.lower())
        self.assertIn("evidence_quality", reason.lower())

    def test_strong_structured_evidence_does_not_reduce_size_cap(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.03)],
            rationale=["Momentum setup"],
            rationale_items=[
                RationaleItem(
                    summary="Momentum and volume align",
                    why_it_matters="Multiple confirmed signals support continuation.",
                    trade_implication="Allow a standard-sized long.",
                    failure_mode="Breakout fails back into range.",
                    evidence=[
                        EvidenceItem(
                            signal="rsi_14",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="gte",
                            observed_value=62.0,
                            threshold=60.0,
                            units="index",
                            source="market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                        ),
                        EvidenceItem(
                            signal="volume_ratio",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="gte",
                            observed_value=2.1,
                            threshold=1.8,
                            units="ratio",
                            source="market_snapshots.BTCUSDT.timeframes.15m.volume_ratio",
                        ),
                    ],
                )
            ],
            risk_notes=[],
            confidence=0.8,
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
        )

        self.assertTrue(approved)
        self.assertEqual(reason, "Approved")

    def test_effective_entry_size_cap_summary_exposes_adjustment(self):
        weak_intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.01)],
            rationale=["Weak setup"],
            rationale_items=[
                RationaleItem(
                    summary="Single signal",
                    why_it_matters="One signal is present.",
                    trade_implication="Entry is possible but low quality.",
                    evidence=[
                        EvidenceItem(
                            signal="rsi_14",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="gte",
                            observed_value=61.0,
                            threshold=60.0,
                            units="index",
                            source="market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                        )
                    ],
                )
            ],
            risk_notes=[],
            confidence=0.6,
        )

        summary = self.engine.get_effective_max_order_notional_pct_balance(
            intent=weak_intent,
            active_profile=self.engine.get_active_risk_profile(),
        )

        self.assertTrue(summary["adjusted"])
        self.assertLess(summary["effective_cap_pct"], summary["base_cap_pct"])
        self.assertIn("low_signal_diversity", summary["evidence_quality"]["issues"])

    def test_unresolved_unknown_orders_block_new_entry(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.01)],
            rationale=["Momentum"],
            risk_notes=[],
            confidence=0.7,
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            unresolved_timeout_count=1,
        )

        self.assertFalse(approved)
        self.assertIn("unresolved order lifecycle", reason.lower())

    def test_pending_open_orders_are_reserved_for_max_positions(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[
                OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.01),
                OrderIntent(symbol="SOLUSDT", action="BUY", order_type="MARKET", quantity=1.0),
            ],
            rationale=["Portfolio expansion"],
            risk_notes=[],
            confidence=0.7,
        )
        acc_state = AccountState(
            total_balance=1000,
            available_balance=1000,
            positions=[],
            open_orders=[
                OpenOrderState(
                    symbol="ETHUSDT",
                    side="BUY",
                    order_type="MARKET",
                    quantity=0.2,
                    status="open",
                    reduce_only=False,
                )
            ],
            open_orders_count=1,
        )

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000, "ETHUSDT": 2000, "SOLUSDT": 100},
        )

        self.assertFalse(approved)
        self.assertIn("cannot open more than", reason.lower())

    def test_pending_open_orders_are_reserved_for_candidate_size_cap(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.005)],
            rationale=["Trigger continuation"],
            risk_notes=[],
            confidence=0.7,
        )
        acc_state = AccountState(
            total_balance=1000,
            available_balance=1000,
            positions=[],
            open_orders=[
                OpenOrderState(
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="MARKET",
                    quantity=0.008,
                    price=30000,
                    status="open",
                    reduce_only=False,
                )
            ],
            open_orders_count=1,
        )

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            is_trigger_mode=True,
        )

        self.assertFalse(approved)
        self.assertIn("candidate size", reason.lower())

    def test_trigger_mode_blocks_additional_entry_when_live_position_exists(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.002)],
            rationale=["Trigger follow-through"],
            risk_notes=[],
            confidence=0.7,
        )
        acc_state = AccountState(
            total_balance=1000,
            available_balance=900,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.01,
                    entry_price=30000,
                    unrealized_pnl_pct=0,
                )
            ],
            open_orders_count=0,
        )

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            is_trigger_mode=True,
        )

        self.assertFalse(approved)
        self.assertIn("trigger mode blocks additional entry", reason.lower())

    def test_trigger_mode_can_allow_additional_entry_when_policy_flag_disabled(self):
        self.engine.policy["risk_limits"]["mode_profiles"]["trigger"][
            "block_additional_entries_when_position_exists"
        ] = False
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.002)],
            rationale=["Trigger follow-through"],
            risk_notes=[],
            confidence=0.7,
        )
        acc_state = AccountState(
            total_balance=1000,
            available_balance=900,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.01,
                    entry_price=30000,
                    unrealized_pnl_pct=0,
                )
            ],
            open_orders_count=0,
        )

        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            is_trigger_mode=True,
        )

        self.assertTrue(approved)
        self.assertEqual(reason, "Approved")

if __name__ == '__main__':
    unittest.main()
