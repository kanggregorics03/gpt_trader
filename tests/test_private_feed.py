import unittest

from core.models import AccountState, OpenOrderState, PositionState
from exchange.private_feed import BybitPrivateAccountFeed


class DummyPrivateWebSocket:
    def __init__(self, *args, **kwargs):
        self.subscriptions = []

    def wallet_stream(self, callback):
        self.subscriptions.append(("wallet", callback))

    def position_stream(self, callback):
        self.subscriptions.append(("position", callback))

    def order_stream(self, callback):
        self.subscriptions.append(("order", callback))

    def execution_stream(self, callback):
        self.subscriptions.append(("execution", callback))


class TestPrivateFeed(unittest.TestCase):
    def test_start_registers_private_subscriptions(self):
        feed = BybitPrivateAccountFeed(
            api_key="key",
            api_secret="secret",
            ws_factory=DummyPrivateWebSocket,
        )

        started = feed.start()

        self.assertTrue(started)
        self.assertEqual(len(feed._ws.subscriptions), 4)

    def test_seed_and_ws_updates_build_account_snapshot_and_execution_cache(self):
        feed = BybitPrivateAccountFeed(
            api_key="key",
            api_secret="secret",
            ws_factory=DummyPrivateWebSocket,
            reconcile_ttl_ms=60_000,
        )
        feed.seed_from_account_state(
            AccountState(
                snapshot_timestamp=1000,
                total_balance=1500.0,
                available_balance=900.0,
                margin_ratio=0.12,
                positions=[
                    PositionState(
                        symbol="BTCUSDT",
                        side="LONG",
                        size=0.1,
                        entry_price=30000.0,
                        unrealized_pnl=12.5,
                        unrealized_pnl_pct=4.16,
                    )
                ],
                open_orders=[
                    OpenOrderState(
                        symbol="BTCUSDT",
                        side="BUY",
                        order_type="LIMIT",
                        quantity=0.05,
                        price=29500.0,
                        client_order_id="gpt_order_1",
                    )
                ],
                open_orders_count=1,
            )
        )

        feed._handle_wallet_message(
            {
                "data": [
                    {
                        "totalEquity": "1510",
                        "totalAvailableBalance": "910",
                        "accountIMRate": "0.11",
                        "coin": [{"coin": "USDT", "equity": "1510"}],
                    }
                ]
            }
        )
        feed._handle_position_message(
            {
                "data": [
                    {
                        "symbol": "ETHUSDT",
                        "side": "Sell",
                        "size": "1.5",
                        "avgPrice": "2000",
                        "markPrice": "1980",
                        "unrealisedPnl": "8.0",
                        "positionIM": "300",
                        "positionMM": "20",
                        "positionValue": "3000",
                        "tradeMode": "0",
                    }
                ]
            }
        )
        feed._handle_execution_message(
            {
                "data": [
                    {
                        "symbol": "BTCUSDT",
                        "execId": "exec-1",
                        "orderId": "order-1",
                        "orderLinkId": "client-1",
                        "side": "Buy",
                        "execPrice": "30010",
                        "execQty": "0.1",
                        "execValue": "3001",
                        "execFee": "2.1",
                        "execTime": "123456",
                        "execPnl": "1.3",
                    }
                ]
            }
        )

        state = feed.get_account_state(["BTCUSDT", "ETHUSDT"])
        recent_execs = feed.get_recent_executions("BTCUSDT")

        self.assertEqual(state.total_balance, 1510.0)
        self.assertEqual(state.available_balance, 910.0)
        self.assertEqual(len(state.positions), 2)
        self.assertEqual(recent_execs[0]["trade_id"], "exec-1")
        self.assertEqual(recent_execs[0]["client_order_id"], "client-1")

    def test_mark_reconcile_required_disables_ws_snapshot_until_rest_seed(self):
        feed = BybitPrivateAccountFeed(
            api_key="key",
            api_secret="secret",
            ws_factory=DummyPrivateWebSocket,
            reconcile_ttl_ms=60_000,
        )
        feed.seed_from_account_state(
            AccountState(
                total_balance=1000.0,
                available_balance=900.0,
                positions=[],
                open_orders=[],
                open_orders_count=0,
            )
        )

        self.assertIsNotNone(feed.get_account_state(["BTCUSDT"]))
        feed.mark_reconcile_required()
        self.assertIsNone(feed.get_account_state(["BTCUSDT"]))
