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


class ControlledTimeFeed(BybitPrivateAccountFeed):
    def __init__(self, *args, now_ms: int = 1_000_000, **kwargs):
        self._test_now_ms = now_ms
        super().__init__(*args, **kwargs)

    def advance_ms(self, delta_ms: int):
        self._test_now_ms += delta_ms

    def _now_ms(self) -> int:
        return self._test_now_ms


class TestPrivateFeed(unittest.TestCase):
    def _seed_rest_snapshot(self, feed: ControlledTimeFeed):
        feed.seed_from_account_state(
            AccountState(
                snapshot_timestamp=feed._now_ms(),
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

    def test_start_registers_private_subscriptions(self):
        feed = ControlledTimeFeed(
            api_key="key",
            api_secret="secret",
            ws_factory=DummyPrivateWebSocket,
        )

        started = feed.start()

        self.assertTrue(started)
        self.assertEqual(len(feed._ws.subscriptions), 4)
        self.assertEqual(feed.get_health_status(), "reconnecting")

    def test_feed_recovers_after_reconcile_and_stability_window_without_private_events(self):
        feed = ControlledTimeFeed(
            api_key="key",
            api_secret="secret",
            ws_factory=DummyPrivateWebSocket,
            reconcile_ttl_ms=300_000,
            ws_stale_after_seconds=25,
            ws_recover_stability_seconds=12,
            forced_reconcile_on_recover=True,
        )
        feed.start()
        self._seed_rest_snapshot(feed)

        self.assertEqual(feed.get_health_status(), "reconnecting")
        self.assertIsNone(feed.get_account_state(["BTCUSDT"]))

        feed.advance_ms(12_000)
        self.assertEqual(feed.get_health_status(), "healthy")

        state = feed.get_account_state(["BTCUSDT"])
        self.assertIsNotNone(state)
        self.assertEqual(state.total_balance, 1500.0)

    def test_feed_stays_healthy_when_first_private_event_arrives_after_reconcile_bootstrap(self):
        feed = ControlledTimeFeed(
            api_key="key",
            api_secret="secret",
            ws_factory=DummyPrivateWebSocket,
            reconcile_ttl_ms=300_000,
            ws_stale_after_seconds=25,
            ws_recover_stability_seconds=12,
            forced_reconcile_on_recover=True,
        )
        feed.start()
        self._seed_rest_snapshot(feed)
        feed.advance_ms(12_000)
        self.assertEqual(feed.get_health_status(), "healthy")

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

        self.assertEqual(feed.get_health_status(), "healthy")

        state = feed.get_account_state(["BTCUSDT"])
        self.assertIsNotNone(state)
        self.assertEqual(state.total_balance, 1510.0)

    def test_feed_keeps_recent_reconciled_state_healthy_until_reconcile_ttl_expires(self):
        feed = ControlledTimeFeed(
            api_key="key",
            api_secret="secret",
            ws_factory=DummyPrivateWebSocket,
            reconcile_ttl_ms=300_000,
            ws_stale_after_seconds=25,
            ws_recover_stability_seconds=10,
            forced_reconcile_on_recover=True,
        )
        feed.start()
        self._seed_rest_snapshot(feed)
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
        feed.seed_from_account_state(
            AccountState(
                snapshot_timestamp=feed._now_ms(),
                total_balance=1510.0,
                available_balance=910.0,
                positions=[],
                open_orders=[],
                open_orders_count=0,
            )
        )
        feed.advance_ms(10_000)
        self.assertEqual(feed.get_health_status(), "healthy")

        feed.advance_ms(26_000)
        self.assertEqual(feed.get_health_status(), "healthy")
        self.assertIsNotNone(feed.get_account_state(["BTCUSDT"]))

        feed.advance_ms(300_000)
        self.assertEqual(feed.get_health_status(), "stale")
        self.assertIsNone(feed.get_account_state(["BTCUSDT"]))

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

        self.assertEqual(feed.get_health_status(), "degraded")
        feed.seed_from_account_state(
            AccountState(
                snapshot_timestamp=feed._now_ms(),
                total_balance=1520.0,
                available_balance=920.0,
                positions=[],
                open_orders=[],
                open_orders_count=0,
            )
        )
        self.assertEqual(feed.get_health_status(), "reconnecting")
        feed.advance_ms(10_000)
        self.assertEqual(feed.get_health_status(), "healthy")
        self.assertEqual(feed.get_recent_executions("BTCUSDT")[0]["trade_id"], "exec-1")

    def test_mark_reconcile_required_forces_rest_fallback_until_reconciled(self):
        feed = ControlledTimeFeed(
            api_key="key",
            api_secret="secret",
            ws_factory=DummyPrivateWebSocket,
            reconcile_ttl_ms=300_000,
            ws_stale_after_seconds=25,
            ws_recover_stability_seconds=1,
            forced_reconcile_on_recover=True,
        )
        feed.start()
        self._seed_rest_snapshot(feed)
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
        feed.seed_from_account_state(
            AccountState(
                snapshot_timestamp=feed._now_ms(),
                total_balance=1500.0,
                available_balance=900.0,
                positions=[],
                open_orders=[],
                open_orders_count=0,
            )
        )
        feed.advance_ms(1_000)
        self.assertEqual(feed.get_health_status(), "healthy")

        feed.mark_reconcile_required()

        self.assertEqual(feed.get_health_status(), "degraded")
        self.assertTrue(feed.needs_periodic_reconcile())
        self.assertIsNone(feed.get_account_state(["BTCUSDT"]))

    def test_private_feed_normalizes_unified_symbols_for_filters(self):
        feed = ControlledTimeFeed(
            api_key="key",
            api_secret="secret",
            ws_factory=DummyPrivateWebSocket,
            ws_recover_stability_seconds=1,
        )
        feed.start()
        feed.seed_from_account_state(
            AccountState(
                snapshot_timestamp=feed._now_ms(),
                total_balance=1500.0,
                available_balance=900.0,
                positions=[],
                open_orders=[],
                open_orders_count=0,
            )
        )
        feed.advance_ms(1_000)

        feed._handle_position_message(
            {
                "data": [
                    {
                        "symbol": "BTC/USDT:USDT",
                        "side": "Buy",
                        "size": "0.1",
                        "avgPrice": "30000",
                        "markPrice": "30100",
                        "unrealisedPnl": "10",
                        "positionIM": "300",
                        "positionMM": "18",
                        "positionValue": "3000",
                        "tradeMode": "1",
                    }
                ]
            }
        )

        state = feed.get_account_state(["BTCUSDT"])

        self.assertIsNotNone(state)
        self.assertEqual(state.positions[0].symbol, "BTCUSDT")

    def test_transport_heartbeat_keeps_feed_healthy_without_account_events(self):
        feed = ControlledTimeFeed(
            api_key="key",
            api_secret="secret",
            ws_factory=DummyPrivateWebSocket,
            reconcile_ttl_ms=300_000,
            ws_stale_after_seconds=25,
            ws_recover_stability_seconds=1,
            forced_reconcile_on_recover=True,
        )
        feed.start()
        self._seed_rest_snapshot(feed)
        feed.advance_ms(1_000)
        self.assertEqual(feed.get_health_status(), "healthy")

        feed.advance_ms(26_000)
        self.assertEqual(feed.get_health_status(), "healthy")

        feed._record_transport_activity()
        self.assertEqual(feed.get_health_status(), "healthy")
        self.assertIsNotNone(feed.get_account_state(["BTCUSDT"]))

    def test_diagnostics_include_event_counts_lag_and_handler_stats(self):
        feed = ControlledTimeFeed(
            api_key="key",
            api_secret="secret",
            ws_factory=DummyPrivateWebSocket,
            now_ms=1_000_000,
        )
        feed.start()
        feed._handle_execution_message(
            {
                "creationTime": "999980",
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
                        "execTime": "999980",
                    }
                ],
            }
        )

        diagnostics = feed.get_diagnostics()

        self.assertEqual(diagnostics["event_counts"]["total"], 1)
        self.assertEqual(diagnostics["event_counts"]["execution"], 1)
        self.assertEqual(diagnostics["last_private_event_age_ms"], 0)
        self.assertEqual(diagnostics["exchange_lag_ms"]["execution"]["last_ms"], 20.0)
        self.assertEqual(diagnostics["cache_sizes"]["execution_symbols"], 1)
        self.assertEqual(diagnostics["handler_stats"]["execution"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
