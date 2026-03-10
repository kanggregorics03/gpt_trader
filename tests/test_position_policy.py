import time
import unittest
from datetime import UTC, datetime, timedelta

from core.models import AccountState, MarketSnapshot, OpenOrderState, PositionState
from core.position_policy import PositionPolicyEngine


class DummyStore:
    def __init__(self, opened_at="2026-03-08 16:00:00", market_history=None):
        self.active_case = {
            "trade_case_id": "BTCUSDT-case-1",
            "opened_at": opened_at,
            "thesis_id": "thesis-1",
            "management_template_id": "position_manage_adaptive",
            "payload": {},
        }
        self.market_history = list(market_history or [])

    def get_active_trade_case(self, symbol):
        return dict(self.active_case if symbol == "BTCUSDT" else {})

    def get_trade_case(self, trade_case_id):
        return {"trade_case_id": trade_case_id, "opened_at": self.active_case["opened_at"], "payload": {}}

    def get_policy_registry_entry(self, policy_key):
        return {}

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


class TestPositionPolicyEngine(unittest.TestCase):
    def test_losing_position_exits(self):
        engine = PositionPolicyEngine(state_store=DummyStore())
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=900.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000.0,
                    unrealized_pnl_pct=-18.0,
                    account_unrealized_pnl_pct=-1.8,
                )
            ],
            open_orders=[],
        )
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            current_price=29460.0,
            atr_15m=100.0,
            atr_1h=200.0,
            trend_1h="DOWN",
            recent_volume_surge=False,
        )
        result = engine.evaluate(
            account_state=account_state,
            market_snapshots={"BTCUSDT": snapshot},
            risk_mode="normal",
            loop_mode="15m",
        )
        self.assertFalse(result["escalate_to_ai"])
        self.assertEqual(result["decision_result"]["decision"], "EXIT")
        self.assertIn("account-loss threshold", result["decision_result"]["rationale"][0])

    def test_stalled_position_exits(self):
        engine = PositionPolicyEngine(
            policy={"position_policy": {"stalled_minutes": 1, "stalled_max_pnl_pct": 0.2}},
            state_store=DummyStore(opened_at="2026-03-08 00:00:00"),
        )
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=950.0,
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
        )
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            current_price=30010.0,
            atr_15m=100.0,
            atr_1h=200.0,
            trend_1h="UP",
            recent_volume_surge=False,
        )
        result = engine.evaluate(
            account_state=account_state,
            market_snapshots={"BTCUSDT": snapshot},
            risk_mode="normal",
            loop_mode="15m",
        )
        self.assertEqual(result["decision_result"]["decision"], "EXIT")

    def test_stale_open_orders_without_positions_are_cancelled(self):
        engine = PositionPolicyEngine(
            policy={"position_policy": {"stale_order_minutes": 1}},
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
            risk_mode="normal",
            loop_mode="15m",
        )

        self.assertFalse(result["escalate_to_ai"])
        self.assertEqual(result["decision_result"]["decision"], "CANCEL")
        self.assertEqual(result["decision_result"]["orders"][0].action, "CANCEL")
        self.assertEqual(result["decision_result"]["orders"][0].target_client_order_id, "client-1")

    def test_stale_open_order_is_cancelled_not_repriced(self):
        engine = PositionPolicyEngine(
            policy={"position_policy": {"stale_order_minutes": 1}},
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
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            current_price=30000.0,
            atr_15m=100.0,
            atr_1h=200.0,
            trend_1h="UP",
            recent_volume_surge=False,
            spread_percentile_1h=40.0,
            depth_percentile_1h=60.0,
        )

        result = engine.evaluate(
            account_state=account_state,
            market_snapshots={"BTCUSDT": snapshot},
            risk_mode="normal",
            loop_mode="15m",
        )

        self.assertFalse(result["escalate_to_ai"])
        self.assertEqual(result["decision_result"]["decision"], "CANCEL")
        self.assertEqual([order.action for order in result["decision_result"]["orders"]], ["CANCEL"])

    def test_negative_pnl_without_invalidation_proximity_holds(self):
        engine = PositionPolicyEngine(
            policy={"position_policy": {"weakened_minutes": 1, "stalled_minutes": 100000}},
            state_store=DummyStore(opened_at="2026-03-08 00:00:00"),
        )
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=950.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000.0,
                    unrealized_pnl_pct=-0.4,
                    account_unrealized_pnl_pct=-0.04,
                )
            ],
            open_orders=[],
        )
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            current_price=29920.0,
            atr_15m=100.0,
            atr_1h=200.0,
            trend_1h="UP",
            recent_volume_surge=False,
            distance_to_invalid_level=1.4,
        )

        result = engine.evaluate(
            account_state=account_state,
            market_snapshots={"BTCUSDT": snapshot},
            risk_mode="normal",
            loop_mode="15m",
        )

        self.assertEqual(result["decision_result"]["decision"], "HOLD")

    def test_negative_pnl_near_invalidation_exits(self):
        engine = PositionPolicyEngine(
            policy={
                "position_policy": {
                    "weakened_minutes": 1,
                    "stalled_minutes": 100000,
                    "invalidation_reduce_distance_atr": 0.8,
                }
            },
            state_store=DummyStore(opened_at="2026-03-08 00:00:00"),
        )
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=950.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000.0,
                    unrealized_pnl_pct=-0.6,
                    account_unrealized_pnl_pct=-0.06,
                )
            ],
            open_orders=[],
        )
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            current_price=29880.0,
            atr_15m=100.0,
            atr_1h=200.0,
            trend_1h="UP",
            recent_volume_surge=False,
            distance_to_invalid_level=0.5,
        )

        result = engine.evaluate(
            account_state=account_state,
            market_snapshots={"BTCUSDT": snapshot},
            risk_mode="normal",
            loop_mode="15m",
        )

        self.assertEqual(result["decision_result"]["decision"], "EXIT")
        self.assertIn("approached invalidation", result["decision_result"]["rationale"][0])

    def test_microstructure_exit_requires_confirmed_history(self):
        now = datetime.now(UTC).replace(tzinfo=None)
        history = [
            {
                "symbol": "BTCUSDT",
                "timestamp": now - timedelta(minutes=8),
                "spread_percentile_1h": 92.0,
                "depth_percentile_1h": 10.0,
                "payload": {"risk_trigger_reasons": ["wide_spread", "elevated_funding"]},
            }
        ]
        engine = PositionPolicyEngine(
            policy={"position_policy": {"stalled_minutes": 100000, "weakened_minutes": 100000}},
            state_store=DummyStore(market_history=history),
        )
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=950.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000.0,
                    unrealized_pnl_pct=-0.2,
                    account_unrealized_pnl_pct=-0.02,
                )
            ],
            open_orders=[],
        )
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            current_price=29940.0,
            atr_15m=100.0,
            atr_1h=200.0,
            trend_1h="UP",
            recent_volume_surge=False,
            spread_percentile_1h=95.0,
            depth_percentile_1h=9.0,
            risk_trigger_reasons=["wide_spread", "elevated_funding"],
        )

        result = engine.evaluate(
            account_state=account_state,
            market_snapshots={"BTCUSDT": snapshot},
            risk_mode="normal",
            loop_mode="15m",
        )

        self.assertEqual(result["decision_result"]["decision"], "HOLD")

        confirmed_engine = PositionPolicyEngine(
            policy={"position_policy": {"stalled_minutes": 100000, "weakened_minutes": 100000}},
            state_store=DummyStore(
                market_history=history
                + [
                    {
                        "symbol": "BTCUSDT",
                        "timestamp": now - timedelta(minutes=3),
                        "spread_percentile_1h": 94.0,
                        "depth_percentile_1h": 11.0,
                        "payload": {"risk_trigger_reasons": ["wide_spread", "elevated_funding"]},
                    }
                ]
            )
        )
        confirmed_result = confirmed_engine.evaluate(
            account_state=account_state,
            market_snapshots={"BTCUSDT": snapshot},
            risk_mode="normal",
            loop_mode="15m",
        )

        self.assertEqual(confirmed_result["decision_result"]["decision"], "EXIT")
        self.assertIn("confirmed liquidity deterioration", confirmed_result["decision_result"]["rationale"][0])


if __name__ == "__main__":
    unittest.main()
