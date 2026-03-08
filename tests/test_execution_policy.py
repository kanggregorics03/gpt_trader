import unittest

from core.execution_policy import ExecutionPolicyEngine
from core.models import MarketSnapshot, OrderBookSnapshot, OrderIntent, TradeIntent


class DummyStore:
    def __init__(self, profile=None):
        self.profile = profile or {}

    def get_execution_policy_profile(
        self,
        symbol,
        loop_mode,
        action_type,
        urgency_bucket,
        liquidity_bucket,
        risk_mode,
    ):
        return dict(self.profile)


class TestExecutionPolicyEngine(unittest.TestCase):
    def _snapshot(self, spread_percentile=50.0, depth_percentile=50.0):
        return MarketSnapshot(
            symbol="BTCUSDT",
            current_price=30000.0,
            atr_15m=100.0,
            atr_1h=200.0,
            trend_1h="UP",
            recent_volume_surge=False,
            spread_percentile_1h=spread_percentile,
            depth_percentile_1h=depth_percentile,
            order_book=OrderBookSnapshot(
                best_bid=29995.0,
                best_ask=30005.0,
                spread_bps=3.0,
                mid_price=30000.0,
            ),
        )

    def test_poor_liquidity_entry_prefers_limit_passive(self):
        engine = ExecutionPolicyEngine(policy={"execution_policy": {"allowed_order_types": ["market", "limit"]}})
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.01)],
            rationale=["Entry"],
            risk_notes=[],
            confidence=0.7,
        )
        updated, summary = engine.apply_to_intent(
            intent=intent,
            market_snapshots={"BTCUSDT": self._snapshot(spread_percentile=92.0, depth_percentile=12.0)},
            account_state=None,
            loop_mode="1h",
            risk_mode="normal",
            is_trigger_mode=False,
        )
        self.assertTrue(summary["applied"])
        self.assertEqual(updated.orders[0].order_type, "LIMIT")
        self.assertEqual(updated.orders[0].execution_profile, "limit_passive")
        self.assertIsNotNone(updated.orders[0].price)

    def test_exit_prefers_market(self):
        engine = ExecutionPolicyEngine(policy={"execution_policy": {"allowed_order_types": ["market", "limit"]}})
        intent = TradeIntent(
            decision="EXIT",
            orders=[OrderIntent(symbol="BTCUSDT", action="SELL", order_type="LIMIT", quantity=0.01, reduce_only=True)],
            rationale=["Risk off"],
            risk_notes=["liquidity risk"],
            confidence=0.8,
        )
        updated, _ = engine.apply_to_intent(
            intent=intent,
            market_snapshots={"BTCUSDT": self._snapshot()},
            account_state=None,
            loop_mode="15m",
            risk_mode="drawdown",
            is_trigger_mode=False,
        )
        self.assertEqual(updated.orders[0].order_type, "MARKET")
        self.assertEqual(updated.orders[0].execution_profile, "market_urgent")
        self.assertIsNone(updated.orders[0].price)

    def test_profile_override_wins_when_supported(self):
        store = DummyStore(
            profile={
                "recommended_execution_profile": "limit_aggressive",
                "recommended_order_type": "LIMIT",
                "metrics": {"sample_count": 8},
            }
        )
        engine = ExecutionPolicyEngine(
            policy={"execution_policy": {"allowed_order_types": ["market", "limit"], "profile_min_support": 4}},
            state_store=store,
        )
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.01)],
            rationale=["Trigger entry"],
            risk_notes=[],
            confidence=0.9,
        )
        updated, summary = engine.apply_to_intent(
            intent=intent,
            market_snapshots={"BTCUSDT": self._snapshot(spread_percentile=20.0, depth_percentile=80.0)},
            account_state=None,
            loop_mode="5m",
            risk_mode="trigger",
            is_trigger_mode=True,
        )
        self.assertEqual(updated.orders[0].execution_profile, "limit_aggressive")
        self.assertEqual(updated.orders[0].order_type, "LIMIT")
        self.assertEqual(summary["orders"][0]["profile_source"], "symbol")

    def test_cancel_orders_are_left_untouched_in_reprice_intents(self):
        engine = ExecutionPolicyEngine(policy={"execution_policy": {"allowed_order_types": ["market", "limit"]}})
        intent = TradeIntent(
            decision="BUY",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="CANCEL",
                    order_type="MARKET",
                    quantity=0.0,
                    target_order_id="order-1",
                ),
                OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.01),
            ],
            rationale=["Reprice entry"],
            risk_notes=[],
            confidence=0.8,
        )

        updated, summary = engine.apply_to_intent(
            intent=intent,
            market_snapshots={"BTCUSDT": self._snapshot()},
            account_state=None,
            loop_mode="15m",
            risk_mode="normal",
            is_trigger_mode=False,
        )

        self.assertTrue(summary["applied"])
        self.assertEqual(updated.orders[0].action, "CANCEL")
        self.assertEqual(updated.orders[1].execution_profile, "limit_passive")


if __name__ == "__main__":
    unittest.main()
