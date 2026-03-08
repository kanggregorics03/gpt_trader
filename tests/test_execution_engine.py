import unittest

from core.models import AccountState, OrderIntent, PositionState, TradeIntent
from exchange.execution_engine import ExecutionEngine


class DummyExchange:
    has = {"fetchClosedOrders": False, "fetchMyTrades": True, "cancelOrder": True}

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

    def cancel_order(self, order_id, symbol, params=None):
        params = dict(params or {})
        return {
            "id": order_id or "cancelled-via-client-id",
            "clientOrderId": params.get("clientOrderId"),
            "symbol": symbol,
            "status": "canceled",
            "info": {
                "orderStatus": "Cancelled",
                "orderLinkId": params.get("orderLinkId"),
            },
        }


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

    def test_cancel_intent_executes_cancel_order(self):
        intent = TradeIntent(
            decision="CANCEL",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="CANCEL",
                    order_type="MARKET",
                    quantity=0.0,
                    target_order_id="order-123",
                    target_client_order_id="client-123",
                )
            ],
            rationale=["Cancel stale order"],
            risk_notes=[],
            confidence=0.9,
        )

        receipts = self.engine.execute_intent(intent)
        transitions = self.engine.build_order_state_transitions(intent.orders, receipts, fills=[])

        self.assertEqual(receipts[0]["status"].lower(), "canceled")
        self.assertEqual(transitions[0]["to_state"], "cancel_requested")
        self.assertEqual(transitions[1]["to_state"], "cancelled")

    def test_mixed_cancel_and_entry_intent_executes_in_order(self):
        class EntryExchange(DummyExchange):
            def __init__(self):
                self.calls = []

            def create_order(self, symbol, type, side, amount, price=None, params=None):
                self.calls.append(("create", symbol, type, side, amount, price, dict(params or {})))
                return {
                    "id": "new-order-1",
                    "clientOrderId": (params or {}).get("clientOrderId"),
                    "symbol": symbol,
                    "status": "open",
                    "type": type,
                    "side": side,
                    "amount": amount,
                    "price": price,
                    "params": params or {},
                }

            def cancel_order(self, order_id, symbol, params=None):
                self.calls.append(("cancel", symbol, order_id, dict(params or {})))
                return super().cancel_order(order_id, symbol, params=params)

        exchange = EntryExchange()
        engine = ExecutionEngine(exchange)
        intent = TradeIntent(
            decision="REPLACE_ENTRY",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="CANCEL",
                    order_type="MARKET",
                    quantity=0.0,
                    target_order_id="order-123",
                    target_client_order_id="client-123",
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
                    lineage_parent_order_id="order-123",
                    lineage_parent_client_order_id="client-123",
                    wait_for_cancel_confirmation=True,
                ),
            ],
            rationale=["Reprice stale order"],
            risk_notes=[],
            confidence=0.8,
        )

        receipts = engine.execute_intent(intent)

        self.assertEqual(exchange.calls[0][0], "cancel")
        self.assertEqual(exchange.calls[1][0], "create")
        self.assertEqual(receipts[0]["status"].lower(), "canceled")
        self.assertEqual(receipts[1]["status"].lower(), "open")
        self.assertEqual(receipts[1]["replaceGroupId"], "replace-1")
        self.assertEqual(receipts[1]["lineageRole"], "replacement")

    def test_replace_entry_waits_when_cancel_is_not_confirmed(self):
        class DeferredExchange(DummyExchange):
            def __init__(self):
                self.calls = []

            def create_order(self, symbol, type, side, amount, price=None, params=None):
                self.calls.append(("create", symbol, type, side, amount, price, dict(params or {})))
                return {
                    "id": "new-order-1",
                    "clientOrderId": (params or {}).get("clientOrderId"),
                    "symbol": symbol,
                    "status": "open",
                }

            def cancel_order(self, order_id, symbol, params=None):
                self.calls.append(("cancel", symbol, order_id, dict(params or {})))
                return {
                    "id": order_id,
                    "clientOrderId": (params or {}).get("clientOrderId"),
                    "symbol": symbol,
                    "status": "open",
                    "info": {
                        "orderStatus": "New",
                        "orderLinkId": (params or {}).get("orderLinkId"),
                    },
                }

        exchange = DeferredExchange()
        engine = ExecutionEngine(exchange)
        intent = TradeIntent(
            decision="REPLACE_ENTRY",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="CANCEL",
                    order_type="MARKET",
                    quantity=0.0,
                    target_order_id="order-123",
                    target_client_order_id="client-123",
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
                    lineage_parent_order_id="order-123",
                    lineage_parent_client_order_id="client-123",
                    wait_for_cancel_confirmation=True,
                ),
            ],
            rationale=["Wait for cancel confirmation before replace"],
            risk_notes=[],
            confidence=0.8,
        )

        receipts = engine.execute_intent(intent)
        transitions = engine.build_order_state_transitions(intent.orders, receipts, fills=[])

        self.assertEqual(exchange.calls, [("cancel", "BTCUSDT", "order-123", {"orderLinkId": "client-123", "clientOrderId": "client-123"})])
        self.assertEqual(receipts[0]["status"].lower(), "open")
        self.assertEqual(receipts[1]["status"], "deferred_waiting_cancel")
        self.assertEqual(transitions[-1]["to_state"], "deferred")
        self.assertEqual(transitions[-1]["event_type"], "replace_deferred")


if __name__ == "__main__":
    unittest.main()
