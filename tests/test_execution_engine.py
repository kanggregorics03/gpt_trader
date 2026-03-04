import unittest

from core.models import AccountState, PositionState, TradeIntent
from exchange.execution_engine import ExecutionEngine


class DummyExchange:
    has = {"fetchClosedOrders": False, "fetchMyTrades": True}

    def fetch_my_trades(self, symbol, limit=50):
        if symbol == "BTCUSDT":
            return [
                {
                    "id": "trade-1",
                    "order": "order-1",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "price": 30000,
                    "amount": 0.1,
                    "cost": 3000,
                    "fee": {"cost": 2.1, "currency": "USDT"},
                    "timestamp": 1111,
                    "info": {"orderLinkId": "client-1", "execPnl": "1.5"},
                }
            ]
        return []


class TestExecutionEngine(unittest.TestCase):
    def setUp(self):
        self.engine = ExecutionEngine(DummyExchange())

    def test_exit_without_explicit_orders_builds_reduce_only_market_closes(self):
        intent = TradeIntent(
            decision="EXIT",
            orders=[],
            rationale=["Close everything"],
            risk_notes=[],
            confidence=0.9,
        )
        account_state = AccountState(
            total_balance=1000,
            available_balance=1000,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000,
                    unrealized_pnl_pct=-1.2,
                ),
                PositionState(
                    symbol="ETHUSDT",
                    side="SHORT",
                    size=1.5,
                    entry_price=2000,
                    unrealized_pnl_pct=0.8,
                ),
            ],
            open_orders_count=0,
        )

        resolved = self.engine._resolve_orders(intent, account_state)

        self.assertEqual(len(resolved), 2)
        self.assertEqual(resolved[0].symbol, "BTCUSDT")
        self.assertEqual(resolved[0].action, "SELL")
        self.assertTrue(resolved[0].reduce_only)
        self.assertEqual(resolved[1].symbol, "ETHUSDT")
        self.assertEqual(resolved[1].action, "BUY")
        self.assertTrue(resolved[1].reduce_only)

    def test_summarize_receipts_aggregates_execution_stats(self):
        receipts = [
            {
                "symbol": "BTCUSDT",
                "clientOrderId": "a1",
                "status": "closed",
                "filled": 0.1,
                "average": 30000,
                "cost": 3000,
                "fee_cost": 2.4,
            },
            {
                "symbol": "ETHUSDT",
                "clientOrderId": "a2",
                "status": "timeout_unresolved",
                "filled": 0.0,
                "average": None,
                "cost": 0.0,
                "fee_cost": 0.0,
            },
            {
                "symbol": "BTCUSDT",
                "clientOrderId": "a3",
                "status": "recovered",
                "filled": 0.0,
                "average": None,
                "cost": 0.0,
                "fee_cost": 0.0,
            },
        ]

        summary = self.engine.summarize_receipts(receipts)

        self.assertEqual(summary["submitted_orders"], 3)
        self.assertEqual(summary["filled_orders"], 1)
        self.assertEqual(summary["recovered_orders"], 1)
        self.assertEqual(summary["unresolved_timeouts"], 1)
        self.assertEqual(summary["notional_value"], 3000.0)
        self.assertEqual(summary["fee_cost"], 2.4)
        self.assertEqual(summary["symbols"], ["BTCUSDT", "ETHUSDT"])

    def test_fetch_fills_for_receipts_matches_order_and_client_order_id(self):
        receipts = [
            {
                "id": "order-1",
                "clientOrderId": "client-1",
                "symbol": "BTCUSDT",
                "status": "closed",
            }
        ]

        fills = self.engine.fetch_fills_for_receipts(receipts)

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["trade_id"], "trade-1")
        self.assertEqual(fills[0]["client_order_id"], "client-1")
        self.assertEqual(fills[0]["realized_pnl"], 1.5)

    def test_state_transition_builders_capture_order_and_position_lifecycle(self):
        intent = TradeIntent(
            decision="EXIT",
            orders=[],
            rationale=["Close risk"],
            risk_notes=[],
            confidence=0.8,
        )
        account_state = AccountState(
            total_balance=1000,
            available_balance=900,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000,
                    unrealized_pnl_pct=1.0,
                )
            ],
            open_orders_count=0,
        )
        post_account_state = AccountState(
            total_balance=1010,
            available_balance=1010,
            positions=[],
            open_orders_count=0,
        )
        planned_orders = self.engine.resolve_orders(intent, account_state)
        receipts = [
            {
                "id": "order-1",
                "clientOrderId": "client-1",
                "symbol": "BTCUSDT",
                "status": "closed",
                "filled": 0.1,
            }
        ]
        fills = self.engine.fetch_fills_for_receipts(receipts)

        order_states = self.engine.build_order_state_transitions(planned_orders, receipts, fills)
        position_states = self.engine.build_position_state_transitions(
            intent,
            account_state,
            post_account_state,
            planned_orders,
            receipts,
        )

        self.assertEqual(order_states[0]["to_state"], "submitted")
        self.assertEqual(order_states[1]["to_state"], "completed")
        self.assertEqual(position_states[0]["from_state"], "open")
        self.assertEqual(position_states[0]["to_state"], "closed")

    def test_private_feed_recent_executions_avoid_rest_trade_fetch(self):
        class NoTradeExchange(DummyExchange):
            def fetch_my_trades(self, symbol, limit=50):
                raise AssertionError("REST trades should not be fetched when private feed has executions")

        class DummyPrivateFeed:
            def get_recent_executions(self, symbol, limit=50):
                return [
                    {
                        "trade_id": "trade-1",
                        "order_id": "order-1",
                        "client_order_id": "client-1",
                        "symbol": symbol,
                        "side": "buy",
                        "price": 30000.0,
                        "amount": 0.1,
                        "cost": 3000.0,
                        "fee_cost": 2.1,
                        "fee_currency": "USDT",
                        "realized_pnl": 1.5,
                        "timestamp": 1111,
                        "payload": {},
                    }
                ]

            def get_recent_orders(self, symbol):
                return []

        engine = ExecutionEngine(NoTradeExchange(), private_feed=DummyPrivateFeed())
        receipts = [{"id": "order-1", "clientOrderId": "client-1", "symbol": "BTCUSDT", "status": "closed"}]

        fills = engine.fetch_fills_for_receipts(receipts)

        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["trade_id"], "trade-1")


if __name__ == "__main__":
    unittest.main()
