import time
import unittest

from core.models import AccountState, MarketSnapshot, OpenOrderState, OrderBookSnapshot, PositionState
from core.safety_policy import SafetyPolicyEngine


class DummyStore:
    def __init__(self, registry=None):
        self.registry = dict(registry or {})

    def get_policy_registry_entry(self, policy_key):
        return dict(self.registry.get(policy_key, {}))

    def get_policy_registry_entry_for_regime(self, domain, regime_key=None):
        regime_text = str(regime_key or "")
        scoped_key = f"champion:{domain}:{regime_text}" if regime_text else f"champion:{domain}"
        return dict(self.registry.get(scoped_key, {}))


class TestSafetyPolicyEngine(unittest.TestCase):
    def test_flat_portfolio_returns_deterministic_hold(self):
        engine = SafetyPolicyEngine(policy={}, state_store=DummyStore())

        result = engine.evaluate(
            account_state=AccountState(total_balance=1000.0, available_balance=1000.0, positions=[], open_orders=[]),
            market_snapshots={},
            risk_mode="trigger",
            loop_mode="5m",
        )

        self.assertFalse(result["escalate_to_ai"])
        self.assertEqual(result["decision_result"]["decision"], "HOLD")
        self.assertIn("1h alpha loop", result["decision_result"]["rationale"][0])

    def test_position_exits_on_acute_loss(self):
        engine = SafetyPolicyEngine(policy={}, state_store=DummyStore())
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=920.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000.0,
                    unrealized_pnl_pct=-1.2,
                )
            ],
            open_orders=[],
        )
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=29640.0,
                atr_15m=110.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
            )
        }

        result = engine.evaluate(
            account_state=account_state,
            market_snapshots=market_snapshots,
            risk_mode="trigger",
            loop_mode="5m",
        )

        self.assertEqual(result["decision_result"]["decision"], "EXIT")
        self.assertEqual(result["decision_result"]["exit_reason"], "emergency_exit")
        self.assertEqual(result["decision_result"]["orders"][0].reduce_only, True)

    def test_registry_override_can_reduce_on_profit_protection(self):
        store = DummyStore(
            {
                "champion:safety_5m:aligned_up|high": {
                    "config": {
                        "profit_protect_trigger_pct": 0.5,
                        "profit_protect_reduce_fraction": 0.5,
                        "risk_trigger_exit_count": 2,
                    }
                }
            }
        )
        engine = SafetyPolicyEngine(policy={}, state_store=store)
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=900.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.2,
                    entry_price=30000.0,
                    unrealized_pnl_pct=0.8,
                )
            ],
            open_orders=[
                OpenOrderState(
                    symbol="BTCUSDT",
                    side="SELL",
                    order_type="LIMIT",
                    quantity=0.1,
                    timestamp=int((time.time() - (5 * 60)) * 1000),
                )
            ],
        )
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30240.0,
                atr_15m=110.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
                spread_percentile_1h=88.0,
                depth_percentile_1h=10.0,
                risk_trigger_reasons=["wide_spread"],
                order_book=OrderBookSnapshot(spread_bps=12.0),
            )
        }

        result = engine.evaluate(
            account_state=account_state,
            market_snapshots=market_snapshots,
            risk_mode="trigger",
            loop_mode="5m",
            regime_key="aligned_up|high",
        )

        self.assertEqual(result["decision_result"]["decision"], "REDUCE")
        self.assertEqual(result["decision_result"]["orders"][0].quantity, 0.1)

    def test_stale_working_orders_without_positions_are_cancelled(self):
        engine = SafetyPolicyEngine(
            policy={"safety_policy": {"stale_order_minutes": 1, "stale_order_reprice_enabled": False}},
            state_store=DummyStore(),
        )
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=1000.0,
            positions=[],
            open_orders=[
                OpenOrderState(
                    symbol="BTCUSDT",
                    order_id="order-1",
                    client_order_id="client-1",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=0.1,
                    timestamp=int((time.time() - (5 * 60)) * 1000),
                )
            ],
        )

        result = engine.evaluate(
            account_state=account_state,
            market_snapshots={},
            risk_mode="trigger",
            loop_mode="5m",
        )

        self.assertFalse(result["escalate_to_ai"])
        self.assertEqual(result["decision_result"]["decision"], "CANCEL")
        self.assertEqual(result["decision_result"]["orders"][0].action, "CANCEL")

    def test_stale_working_order_can_be_repriced(self):
        engine = SafetyPolicyEngine(policy={"safety_policy": {"stale_order_minutes": 1}}, state_store=DummyStore())
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=1000.0,
            positions=[],
            open_orders=[
                OpenOrderState(
                    symbol="BTCUSDT",
                    order_id="order-1",
                    client_order_id="client-1",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=0.1,
                    timestamp=int((time.time() - (5 * 60)) * 1000),
                )
            ],
        )
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            current_price=30000.0,
            atr_15m=100.0,
            atr_1h=200.0,
            trend_1h="UP",
            recent_volume_surge=False,
            spread_percentile_1h=35.0,
            depth_percentile_1h=70.0,
            order_book=OrderBookSnapshot(best_bid=29990.0, best_ask=30010.0),
        )

        result = engine.evaluate(
            account_state=account_state,
            market_snapshots={"BTCUSDT": snapshot},
            risk_mode="trigger",
            loop_mode="5m",
        )

        self.assertFalse(result["escalate_to_ai"])
        self.assertEqual(result["decision_result"]["decision"], "REPLACE_ENTRY")
        self.assertEqual([order.action for order in result["decision_result"]["orders"]], ["CANCEL", "BUY"])
        self.assertEqual(result["decision_result"]["orders"][0].lineage_role, "cancel")
        self.assertEqual(result["decision_result"]["orders"][1].lineage_role, "replacement")
        self.assertTrue(result["decision_result"]["orders"][1].wait_for_cancel_confirmation)


if __name__ == "__main__":
    unittest.main()
