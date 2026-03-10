import time
import unittest

from core.models import AccountState
from exchange.account_reader import AccountReader


class DummyExchange:
    def __init__(self):
        self.bulk_fetch_attempts = 0
        self.balance_calls = 0
        self.position_calls = 0
        self.open_order_calls = 0

    def fetch_balance(self):
        self.balance_calls += 1
        return {
            "USDT": {"total": 1500.0, "free": 900.0},
            "info": {
                "result": {
                    "list": [
                        {
                            "accountIMRate": "0.12",
                        }
                    ]
                }
            },
        }

    def fetch_positions(self, symbols):
        self.position_calls += 1
        self.last_symbols = symbols
        return [
            {
                "symbol": "BTCUSDT",
                "side": "long",
                "contracts": 0.1,
                "entryPrice": 30000,
                "markPrice": 30100,
                "unrealizedPnl": 12.5,
                "initialMargin": 300.0,
                "maintenanceMargin": 18.0,
                "liquidationPrice": 25000,
                "leverage": 3,
                "notional": 3000.0,
                "marginMode": "isolated",
                "info": {
                    "positionIM": "300",
                    "positionMM": "18",
                    "liqPrice": "25000",
                    "tradeMode": "1",
                    "positionValue": "3000",
                },
            }
        ]

    def fetch_open_orders(self, symbol=None):
        self.open_order_calls += 1
        if symbol is None:
            self.bulk_fetch_attempts += 1
            raise TypeError("symbol required in dummy exchange")
        if symbol != "BTCUSDT":
            return []
        return [
            {
                "symbol": "BTCUSDT",
                "side": "buy",
                "type": "limit",
                "amount": 0.05,
                "price": 29500,
                "reduceOnly": False,
                "status": "open",
                "clientOrderId": "gpt_order_1",
                "timestamp": 1710000000000,
                "info": {
                    "takeProfit": "31000",
                    "stopLoss": "28900",
                    "orderLinkId": "gpt_order_1",
                },
            }
        ]


class HealthyPrivateFeed:
    def __init__(self):
        self.seeded = None
        self.reconcile_required = False
        self.account_state = AccountState(
            total_balance=1600.0,
            available_balance=950.0,
            positions=[],
            open_orders=[],
            open_orders_count=0,
        )

    def get_health_status(self):
        return "healthy"

    def needs_periodic_reconcile(self):
        return self.reconcile_required

    def get_account_state(self, symbols):
        if self.reconcile_required:
            return None
        return self.account_state.model_copy(deep=True)

    def seed_from_account_state(self, state):
        self.seeded = state

    def mark_reconcile_required(self):
        self.reconcile_required = True

    def get_diagnostics(self):
        return {"health_status": "healthy", "event_counts": {"total": 3}}


class DegradedPrivateFeed(HealthyPrivateFeed):
    def __init__(self, health_status="degraded", reconcile_due=False):
        super().__init__()
        self.health_status = health_status
        self.reconcile_due = reconcile_due

    def get_health_status(self):
        return self.health_status

    def needs_periodic_reconcile(self):
        return self.reconcile_due


class TestAccountReader(unittest.TestCase):
    def test_get_account_state_includes_position_risk_and_open_order_details(self):
        reader = AccountReader.__new__(AccountReader)
        reader.exchange = DummyExchange()
        reader.cache_ttl_ms = 0
        reader.private_feed = None
        reader.invalidate_cache()

        state = reader.get_account_state(["BTCUSDT", "ETHUSDT"])

        self.assertEqual(state.total_balance, 1500.0)
        self.assertEqual(state.available_balance, 900.0)
        self.assertEqual(state.margin_ratio, 0.12)
        self.assertEqual(state.total_unrealized_pnl, 12.5)
        self.assertEqual(state.open_orders_count, 1)
        self.assertEqual(len(state.open_orders), 1)
        self.assertIsNotNone(state.snapshot_timestamp)

        position = state.positions[0]
        self.assertEqual(position.symbol, "BTCUSDT")
        self.assertEqual(position.mark_price, 30100.0)
        self.assertEqual(position.unrealized_pnl, 12.5)
        self.assertEqual(position.liquidation_price, 25000.0)
        self.assertEqual(position.leverage, 3.0)
        self.assertEqual(position.notional_value, 3000.0)
        self.assertEqual(position.initial_margin, 300.0)
        self.assertEqual(position.maintenance_margin, 18.0)
        self.assertEqual(position.margin_mode, "isolated")
        self.assertAlmostEqual(position.unrealized_pnl_pct, 12.5 / 300.0 * 100)
        self.assertAlmostEqual(position.account_unrealized_pnl_pct, 12.5 / 1500.0 * 100)

        open_order = state.open_orders[0]
        self.assertEqual(open_order.symbol, "BTCUSDT")
        self.assertEqual(open_order.side, "BUY")
        self.assertEqual(open_order.order_type, "LIMIT")
        self.assertEqual(open_order.client_order_id, "gpt_order_1")
        self.assertEqual(open_order.take_profit, 31000.0)
        self.assertEqual(open_order.stop_loss, 28900.0)
        self.assertEqual(reader.exchange.bulk_fetch_attempts, 1)

    def test_bulk_open_orders_fetch_filters_symbols_when_supported(self):
        class BulkExchange(DummyExchange):
            def fetch_open_orders(self, symbol=None):
                if symbol is not None:
                    return super().fetch_open_orders(symbol)
                return [
                    {
                        "symbol": "BTCUSDT",
                        "side": "buy",
                        "type": "limit",
                        "amount": 0.05,
                        "price": 29500,
                        "status": "open",
                        "clientOrderId": "btc_order",
                        "info": {},
                    },
                    {
                        "symbol": "SOLUSDT",
                        "side": "sell",
                        "type": "limit",
                        "amount": 5,
                        "price": 120,
                        "status": "open",
                        "clientOrderId": "sol_order",
                        "info": {},
                    },
                ]

        reader = AccountReader.__new__(AccountReader)
        reader.exchange = BulkExchange()
        reader.cache_ttl_ms = 0
        reader.private_feed = None
        reader.invalidate_cache()

        state = reader.get_account_state(["BTCUSDT"])

        self.assertEqual(state.open_orders_count, 1)
        self.assertEqual(state.open_orders[0].symbol, "BTCUSDT")

    def test_account_reader_normalizes_ccxt_unified_symbols(self):
        class UnifiedExchange(DummyExchange):
            def fetch_positions(self, symbols):
                self.position_calls += 1
                return [
                    {
                        "symbol": "BTC/USDT:USDT",
                        "side": "long",
                        "contracts": 0.1,
                        "entryPrice": 30000,
                        "markPrice": 30100,
                        "unrealizedPnl": 12.5,
                        "initialMargin": 300.0,
                        "info": {
                            "positionIM": "300",
                            "positionMM": "18",
                            "tradeMode": "1",
                            "positionValue": "3000",
                        },
                    }
                ]

            def fetch_open_orders(self, symbol=None):
                if symbol is None:
                    return [
                        {
                            "symbol": "BTC/USDT:USDT",
                            "side": "buy",
                            "type": "limit",
                            "amount": 0.05,
                            "price": 29500,
                            "status": "open",
                            "clientOrderId": "btc_order",
                            "info": {},
                        }
                    ]
                return []

        reader = AccountReader.__new__(AccountReader)
        reader.exchange = UnifiedExchange()
        reader.cache_ttl_ms = 0
        reader.private_feed = None
        reader.invalidate_cache()

        state = reader.get_account_state(["BTCUSDT"])

        self.assertEqual(state.positions[0].symbol, "BTCUSDT")
        self.assertEqual(state.open_orders[0].symbol, "BTCUSDT")

    def test_short_ttl_cache_reuses_account_snapshot_for_same_symbol_set(self):
        exchange = DummyExchange()
        reader = AccountReader(exchange, cache_ttl_ms=2000)

        first = reader.get_account_state(["ETHUSDT", "BTCUSDT"])
        second = reader.get_account_state(["BTCUSDT", "ETHUSDT"])

        self.assertEqual(exchange.balance_calls, 1)
        self.assertEqual(exchange.position_calls, 1)
        self.assertEqual(exchange.open_order_calls, 3)
        self.assertEqual(first.total_balance, second.total_balance)
        self.assertNotEqual(id(first), id(second))

    def test_invalidate_cache_forces_refetch(self):
        exchange = DummyExchange()
        reader = AccountReader(exchange, cache_ttl_ms=2000)

        reader.get_account_state(["BTCUSDT"])
        reader.invalidate_cache()
        reader.get_account_state(["BTCUSDT"])

        self.assertEqual(exchange.balance_calls, 2)
        self.assertEqual(exchange.position_calls, 2)

    def test_expired_cache_refetches_account_snapshot(self):
        exchange = DummyExchange()
        reader = AccountReader(exchange, cache_ttl_ms=10)

        reader.get_account_state(["BTCUSDT"])
        time.sleep(0.02)
        reader.get_account_state(["BTCUSDT"])

        self.assertEqual(exchange.balance_calls, 2)
        self.assertEqual(exchange.position_calls, 2)

    def test_healthy_private_feed_snapshot_is_used_before_rest_reconcile(self):
        exchange = DummyExchange()
        private_feed = HealthyPrivateFeed()
        reader = AccountReader(exchange, cache_ttl_ms=0, private_feed=private_feed)

        state = reader.get_account_state(["BTCUSDT"])

        self.assertEqual(state.total_balance, 1600.0)
        self.assertEqual(exchange.balance_calls, 0)

    def test_degraded_private_feed_falls_back_to_rest_and_seeds_feed(self):
        exchange = DummyExchange()
        private_feed = DegradedPrivateFeed(health_status="stale", reconcile_due=False)
        reader = AccountReader(exchange, cache_ttl_ms=0, private_feed=private_feed)

        state = reader.get_account_state(["BTCUSDT"])

        self.assertEqual(exchange.balance_calls, 1)
        self.assertIsNotNone(private_feed.seeded)
        self.assertEqual(state.total_balance, 1500.0)

    def test_reconcile_due_private_feed_uses_rest_even_when_ws_is_healthy(self):
        exchange = DummyExchange()
        private_feed = DegradedPrivateFeed(health_status="healthy", reconcile_due=True)
        reader = AccountReader(exchange, cache_ttl_ms=0, private_feed=private_feed)

        state = reader.get_account_state(["BTCUSDT"])

        self.assertEqual(exchange.balance_calls, 1)
        self.assertIsNotNone(private_feed.seeded)
        self.assertEqual(state.total_balance, 1500.0)

    def test_force_refresh_reconciles_rest_and_seeds_private_feed(self):
        exchange = DummyExchange()
        private_feed = HealthyPrivateFeed()
        reader = AccountReader(exchange, cache_ttl_ms=0, private_feed=private_feed)

        state = reader.get_account_state(["BTCUSDT"], force_refresh=True)

        self.assertEqual(exchange.balance_calls, 1)
        self.assertIsNotNone(private_feed.seeded)
        self.assertEqual(state.total_balance, 1500.0)

    def test_runtime_diagnostics_expose_cache_and_private_feed_health(self):
        exchange = DummyExchange()
        private_feed = HealthyPrivateFeed()
        reader = AccountReader(exchange, cache_ttl_ms=2000, private_feed=private_feed)

        reader.get_account_state(["BTCUSDT"])
        diagnostics = reader.get_runtime_diagnostics()

        self.assertTrue(diagnostics["cache"]["has_cached_state"])
        self.assertEqual(diagnostics["cache"]["cached_symbol_count"], 1)
        self.assertEqual(diagnostics["private_feed"]["health_status"], "healthy")


if __name__ == "__main__":
    unittest.main()
