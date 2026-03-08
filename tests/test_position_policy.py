import time
import unittest

from core.models import AccountState, MarketSnapshot, OpenOrderState, PositionState
from core.position_policy import PositionPolicyEngine


class DummyStore:
    def __init__(self, opened_at="2026-03-08 16:00:00"):
        self.active_case = {
            "trade_case_id": "BTCUSDT-case-1",
            "opened_at": opened_at,
            "thesis_id": "thesis-1",
            "management_template_id": "position_manage_adaptive",
            "payload": {},
        }

    def get_active_trade_case(self, symbol):
        return dict(self.active_case if symbol == "BTCUSDT" else {})

    def get_trade_case(self, trade_case_id):
        return {"trade_case_id": trade_case_id, "opened_at": self.active_case["opened_at"], "payload": {}}

    def get_policy_registry_entry(self, policy_key):
        return {}


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
                    unrealized_pnl_pct=-1.8,
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

    def test_stalled_position_reduces(self):
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
        self.assertEqual(result["decision_result"]["decision"], "REDUCE")

    def test_stale_open_orders_without_positions_are_cancelled(self):
        engine = PositionPolicyEngine(
            policy={"position_policy": {"stale_order_minutes": 1, "stale_order_reprice_enabled": False}},
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

    def test_stale_open_order_can_be_repriced(self):
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
        self.assertEqual(result["decision_result"]["decision"], "REPLACE_ENTRY")
        self.assertEqual([order.action for order in result["decision_result"]["orders"]], ["CANCEL", "BUY"])
        self.assertEqual(result["decision_result"]["orders"][0].lineage_role, "cancel")
        self.assertEqual(result["decision_result"]["orders"][1].lineage_role, "replacement")
        self.assertTrue(result["decision_result"]["orders"][1].wait_for_cancel_confirmation)
        self.assertEqual(result["decision_result"]["orders"][1].order_type, "LIMIT")


if __name__ == "__main__":
    unittest.main()
