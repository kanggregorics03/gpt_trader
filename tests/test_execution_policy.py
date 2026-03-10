import unittest

from core.execution_policy import ExecutionPolicyEngine
from core.models import MarketSnapshot, OrderBookSnapshot, OrderIntent, TradeIntent


class DummyStore:
    def get_execution_policy_profile(
        self,
        symbol,
        loop_mode,
        action_type,
        urgency_bucket,
        liquidity_bucket,
        risk_mode,
    ):
        return {
            "recommended_execution_profile": "limit_passive",
            "recommended_order_type": "LIMIT",
            "metrics": {"sample_count": 99},
        }


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

    def test_market_entry_stays_market_for_immediate_entry(self):
        engine = ExecutionPolicyEngine(policy={"execution_policy": {"allowed_order_types": ["market", "limit"]}})
        intent = TradeIntent(
            decision="BUY",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="BUY",
                    order_type="MARKET",
                    entry_style="immediate",
                    quantity=0.01,
                )
            ],
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
            is_guarded_attention=False,
        )

        self.assertTrue(summary["applied"])
        self.assertEqual(updated.orders[0].order_type, "MARKET")
        self.assertEqual(updated.orders[0].execution_profile, "market_urgent")
        self.assertEqual(summary["orders"][0]["profile_source"], "deterministic_contract")
        self.assertIsNone(summary["orders"][0]["contract_adjustment"])

    def test_limit_retest_entry_anchors_to_breakout_level(self):
        engine = ExecutionPolicyEngine(policy={"execution_policy": {"allowed_order_types": ["market", "limit"]}})
        intent = TradeIntent(
            decision="BUY",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="BUY",
                    order_type="LIMIT",
                    entry_style="retest",
                    quantity=0.01,
                    price=29980.0,
                )
            ],
            rationale=["Buy on breakout retest"],
            risk_notes=[],
            confidence=0.7,
        )

        updated, summary = engine.apply_to_intent(
            intent=intent,
            market_snapshots={
                "BTCUSDT": self._snapshot(spread_percentile=92.0, depth_percentile=12.0).model_copy(
                    update={
                        "distance_to_15m_breakout_level": 0.4,
                        "distance_to_1h_breakout_level": 0.9,
                    }
                )
            },
            account_state=None,
            loop_mode="1h",
            risk_mode="normal",
            is_guarded_attention=False,
        )

        self.assertEqual(updated.orders[0].order_type, "LIMIT")
        self.assertEqual(updated.orders[0].execution_profile, "limit_passive")
        self.assertTrue(updated.orders[0].post_only)
        self.assertEqual(updated.orders[0].price, 29960.0)
        self.assertEqual(summary["orders"][0]["entry_style"], "retest")
        self.assertEqual(summary["orders"][0]["profile_source"], "deterministic_contract")

    def test_limit_value_entry_stays_passive_with_offset_pricing(self):
        engine = ExecutionPolicyEngine(policy={"execution_policy": {"allowed_order_types": ["market", "limit"]}})
        intent = TradeIntent(
            decision="BUY",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="BUY",
                    order_type="LIMIT",
                    entry_style="value",
                    quantity=0.01,
                    price=29980.0,
                )
            ],
            rationale=["Buy on value retrace"],
            risk_notes=[],
            confidence=0.7,
        )

        updated, summary = engine.apply_to_intent(
            intent=intent,
            market_snapshots={"BTCUSDT": self._snapshot(spread_percentile=92.0, depth_percentile=12.0)},
            account_state=None,
            loop_mode="1h",
            risk_mode="normal",
            is_guarded_attention=False,
        )

        self.assertEqual(updated.orders[0].order_type, "LIMIT")
        self.assertEqual(updated.orders[0].execution_profile, "limit_passive")
        self.assertTrue(updated.orders[0].post_only)
        self.assertEqual(updated.orders[0].price, 29976.0)
        self.assertEqual(summary["orders"][0]["entry_style"], "value")
        self.assertEqual(summary["orders"][0]["profile_source"], "deterministic_contract")

    def test_limit_value_entry_widens_offset_when_spread_and_atr_expand(self):
        engine = ExecutionPolicyEngine(policy={"execution_policy": {"allowed_order_types": ["market", "limit"]}})
        intent = TradeIntent(
            decision="BUY",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="BUY",
                    order_type="LIMIT",
                    entry_style="value",
                    quantity=0.01,
                    price=29980.0,
                )
            ],
            rationale=["Buy on wider value retrace"],
            risk_notes=[],
            confidence=0.7,
        )

        updated, _ = engine.apply_to_intent(
            intent=intent,
            market_snapshots={
                "BTCUSDT": self._snapshot(spread_percentile=92.0, depth_percentile=12.0).model_copy(
                    update={
                        "atr_15m": 600.0,
                        "order_book": OrderBookSnapshot(
                            best_bid=29940.0,
                            best_ask=30060.0,
                            spread_bps=12.0,
                            mid_price=30000.0,
                        ),
                    }
                )
            },
            account_state=None,
            loop_mode="1h",
            risk_mode="normal",
            is_guarded_attention=False,
        )

        self.assertGreater(updated.orders[0].limit_offset_bps, 8.0)
        self.assertLess(updated.orders[0].price, 29976.0)

    def test_limit_immediate_entry_uses_aggressive_limit(self):
        engine = ExecutionPolicyEngine(policy={"execution_policy": {"allowed_order_types": ["market", "limit"]}})
        intent = TradeIntent(
            decision="BUY",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="BUY",
                    order_type="LIMIT",
                    entry_style="immediate",
                    quantity=0.01,
                    price=30010.0,
                )
            ],
            rationale=["Buy now but keep limit discipline"],
            risk_notes=[],
            confidence=0.7,
        )

        updated, summary = engine.apply_to_intent(
            intent=intent,
            market_snapshots={"BTCUSDT": self._snapshot(spread_percentile=92.0, depth_percentile=12.0)},
            account_state=None,
            loop_mode="1h",
            risk_mode="normal",
            is_guarded_attention=False,
        )

        self.assertEqual(updated.orders[0].order_type, "LIMIT")
        self.assertEqual(updated.orders[0].execution_profile, "limit_aggressive")
        self.assertFalse(updated.orders[0].post_only)
        self.assertEqual(summary["orders"][0]["profile_source"], "deterministic_contract")

    def test_exit_prefers_market(self):
        engine = ExecutionPolicyEngine(policy={"execution_policy": {"allowed_order_types": ["market", "limit"]}})
        intent = TradeIntent(
            decision="EXIT",
            orders=[OrderIntent(symbol="BTCUSDT", action="SELL", order_type="LIMIT", quantity=0.01, reduce_only=True)],
            rationale=["Risk off"],
            risk_notes=["liquidity risk"],
            confidence=0.8,
        )

        updated, summary = engine.apply_to_intent(
            intent=intent,
            market_snapshots={"BTCUSDT": self._snapshot()},
            account_state=None,
            loop_mode="15m",
            risk_mode="drawdown",
            is_guarded_attention=False,
        )

        self.assertEqual(updated.orders[0].order_type, "MARKET")
        self.assertEqual(updated.orders[0].execution_profile, "market_urgent")
        self.assertEqual(summary["orders"][0]["action_type"], "exit")

    def test_reduce_prefers_market(self):
        engine = ExecutionPolicyEngine(policy={"execution_policy": {"allowed_order_types": ["market", "limit"]}})
        intent = TradeIntent(
            decision="REDUCE",
            orders=[OrderIntent(symbol="BTCUSDT", action="SELL", order_type="LIMIT", quantity=0.01, reduce_only=True)],
            rationale=["Trim risk"],
            risk_notes=[],
            confidence=0.8,
        )

        updated, summary = engine.apply_to_intent(
            intent=intent,
            market_snapshots={"BTCUSDT": self._snapshot(spread_percentile=92.0, depth_percentile=12.0)},
            account_state=None,
            loop_mode="15m",
            risk_mode="normal",
            is_guarded_attention=False,
        )

        self.assertEqual(updated.orders[0].execution_profile, "market_urgent")
        self.assertEqual(updated.orders[0].order_type, "MARKET")
        self.assertEqual(summary["orders"][0]["action_type"], "reduce")

    def test_live_execution_ignores_learned_profile_recommendations(self):
        engine = ExecutionPolicyEngine(
            policy={"execution_policy": {"allowed_order_types": ["market", "limit"]}},
            state_store=DummyStore(),
        )
        intent = TradeIntent(
            decision="BUY",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="BUY",
                    order_type="MARKET",
                    entry_style="immediate",
                    quantity=0.01,
                )
            ],
            rationale=["Enter now"],
            risk_notes=[],
            confidence=0.9,
        )

        updated, summary = engine.apply_to_intent(
            intent=intent,
            market_snapshots={"BTCUSDT": self._snapshot(spread_percentile=92.0, depth_percentile=12.0)},
            account_state=None,
            loop_mode="1h",
            risk_mode="normal",
            is_guarded_attention=False,
        )

        self.assertEqual(updated.orders[0].execution_profile, "market_urgent")
        self.assertEqual(updated.orders[0].order_type, "MARKET")
        self.assertEqual(summary["orders"][0]["profile_source"], "deterministic_contract")
        self.assertIsNone(summary["orders"][0]["contract_adjustment"])

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
                OrderIntent(
                    symbol="BTCUSDT",
                    action="BUY",
                    order_type="MARKET",
                    entry_style="immediate",
                    quantity=0.01,
                ),
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
            is_guarded_attention=False,
        )

        self.assertTrue(summary["applied"])
        self.assertEqual(updated.orders[0].action, "CANCEL")
        self.assertEqual(updated.orders[1].execution_profile, "market_urgent")
        self.assertEqual(summary["orders"][1]["profile_source"], "deterministic_contract")


if __name__ == "__main__":
    unittest.main()
