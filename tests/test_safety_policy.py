import time
import unittest
from datetime import UTC, datetime, timedelta

from core.models import AccountState, MarketSnapshot, OpenOrderState, OrderBookSnapshot, PositionState
from core.safety_policy import SafetyPolicyEngine


class DummyStore:
    def __init__(self, registry=None, market_history=None):
        self.registry = dict(registry or {})
        self.market_history = list(market_history or [])

    def get_policy_registry_entry(self, policy_key):
        return dict(self.registry.get(policy_key, {}))

    def get_policy_registry_entry_for_regime(self, domain, regime_key=None):
        regime_text = str(regime_key or "")
        scoped_key = f"champion:{domain}:{regime_text}" if regime_text else f"champion:{domain}"
        return dict(self.registry.get(scoped_key, {}))

    def get_market_snapshot_history_between(self, symbol, start_timestamp, end_timestamp, limit=None):
        rows = [
            dict(row)
            for row in self.market_history
            if row.get("symbol") == symbol
            and start_timestamp <= row.get("timestamp", start_timestamp) <= end_timestamp
        ]
        if limit is not None:
            rows = rows[:limit]
        return rows


class TestSafetyPolicyEngine(unittest.TestCase):
    def test_emergency_state_does_not_promote_acute_loss_alone_to_5m_emergency(self):
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
                    unrealized_pnl_pct=-12.0,
                    account_unrealized_pnl_pct=-1.2,
                )
            ],
            open_orders=[],
        )

        state = engine.get_emergency_state(
            account_state=account_state,
            market_snapshots={},
        )

        self.assertFalse(state["active"])
        self.assertEqual(state["reasons"], [])

    def test_flat_portfolio_returns_deterministic_hold(self):
        engine = SafetyPolicyEngine(policy={}, state_store=DummyStore())

        result = engine.evaluate(
            account_state=AccountState(total_balance=1000.0, available_balance=1000.0, positions=[], open_orders=[]),
            market_snapshots={},
            risk_mode="guarded",
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
                    unrealized_pnl_pct=-12.0,
                    account_unrealized_pnl_pct=-1.2,
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
            risk_mode="guarded",
            loop_mode="5m",
        )

        self.assertEqual(result["decision_result"]["decision"], "EXIT")
        self.assertEqual(result["decision_result"]["exit_reason"], "emergency_exit")
        self.assertEqual(result["decision_result"]["orders"][0].reduce_only, True)
        self.assertIn("account-loss threshold", result["decision_result"]["rationale"][0])

    def test_liquidity_exit_requires_confirmed_history(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        history = [
            {
                "symbol": "BTCUSDT",
                "timestamp": now - timedelta(minutes=6),
                "spread_percentile_1h": 88.0,
                "depth_percentile_1h": 12.0,
                "payload": {"risk_trigger_reasons": ["wide_spread"]},
            }
        ]
        engine = SafetyPolicyEngine(policy={}, state_store=DummyStore(market_history=history))
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
                    side="BUY",
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
                order_book=OrderBookSnapshot(spread_bps=12.0, best_bid=30230.0, best_ask=30250.0),
            )
        }

        result = engine.evaluate(
            account_state=account_state,
            market_snapshots=market_snapshots,
            risk_mode="guarded",
            loop_mode="5m",
        )

        self.assertEqual(result["decision_result"]["decision"], "HOLD")

        confirmed_engine = SafetyPolicyEngine(
            policy={},
            state_store=DummyStore(
                market_history=history
                + [
                    {
                        "symbol": "BTCUSDT",
                        "timestamp": now - timedelta(minutes=2),
                        "spread_percentile_1h": 90.0,
                        "depth_percentile_1h": 10.0,
                        "payload": {"risk_trigger_reasons": ["wide_spread"]},
                    }
                ]
            ),
        )
        confirmed_result = confirmed_engine.evaluate(
            account_state=account_state,
            market_snapshots=market_snapshots,
            risk_mode="guarded",
            loop_mode="5m",
        )

        self.assertEqual(confirmed_result["decision_result"]["decision"], "EXIT")
        self.assertEqual(confirmed_result["decision_result"]["exit_reason"], "liquidity_exit")

    def test_stale_order_risk_requires_confirmed_history(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        engine = SafetyPolicyEngine(
            policy={"safety_policy": {"stale_order_minutes": 1}},
            state_store=DummyStore(
                market_history=[
                    {
                        "symbol": "BTCUSDT",
                        "timestamp": now - timedelta(minutes=6),
                        "spread_percentile_1h": 89.0,
                        "depth_percentile_1h": 11.0,
                        "payload": {"risk_trigger_reasons": ["wide_spread"]},
                    },
                    {
                        "symbol": "BTCUSDT",
                        "timestamp": now - timedelta(minutes=2),
                        "spread_percentile_1h": 91.0,
                        "depth_percentile_1h": 10.0,
                        "payload": {"risk_trigger_reasons": []},
                    },
                ]
            ),
        )
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=900.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.2,
                    entry_price=30000.0,
                    unrealized_pnl_pct=0.1,
                    account_unrealized_pnl_pct=0.01,
                )
            ],
            open_orders=[
                OpenOrderState(
                    symbol="BTCUSDT",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=0.1,
                    timestamp=int((time.time() - (5 * 60)) * 1000),
                )
            ],
        )
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30020.0,
                atr_15m=110.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
                spread_percentile_1h=91.0,
                depth_percentile_1h=10.0,
                risk_trigger_reasons=["wide_spread"],
            )
        }

        result = engine.evaluate(
            account_state=account_state,
            market_snapshots=market_snapshots,
            risk_mode="guarded",
            loop_mode="5m",
        )

        self.assertEqual(result["decision_result"]["decision"], "HOLD")

    def test_stale_working_orders_without_positions_are_cancelled(self):
        engine = SafetyPolicyEngine(
            policy={"safety_policy": {"stale_order_minutes": 1}},
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
            risk_mode="guarded",
            loop_mode="5m",
        )

        self.assertFalse(result["escalate_to_ai"])
        self.assertEqual(result["decision_result"]["decision"], "CANCEL")
        self.assertEqual(result["decision_result"]["orders"][0].action, "CANCEL")

        state = engine.get_emergency_state(
            account_state=account_state,
            market_snapshots={},
        )

        self.assertFalse(state["active"])
        self.assertEqual(state["reasons"], [])

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
            risk_mode="guarded",
            loop_mode="5m",
        )

        self.assertFalse(result["escalate_to_ai"])
        self.assertEqual(result["decision_result"]["decision"], "CANCEL")
        self.assertEqual([order.action for order in result["decision_result"]["orders"]], ["CANCEL"])


if __name__ == "__main__":
    unittest.main()
