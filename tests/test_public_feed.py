import unittest

from exchange.public_feed import BybitPublicMarketFeed


class DummyPublicWebSocket:
    def __init__(self, *args, **kwargs):
        self.subscriptions = []

    def ticker_stream(self, symbol, callback):
        self.subscriptions.append(("ticker", symbol, callback))

    def orderbook_stream(self, depth, symbol, callback):
        self.subscriptions.append(("orderbook", symbol, depth, callback))

    def kline_stream(self, interval, symbol, callback):
        self.subscriptions.append(("kline", symbol, interval, callback))


class ControlledTimePublicFeed(BybitPublicMarketFeed):
    def __init__(self, *args, now_ms: int = 1_000_000, **kwargs):
        self._test_now_ms = now_ms
        super().__init__(*args, **kwargs)

    def advance_ms(self, delta_ms: int):
        self._test_now_ms += delta_ms

    def _now_ms(self) -> int:
        return self._test_now_ms


class TestPublicFeed(unittest.TestCase):
    def test_start_respects_reduced_subscription_configuration(self):
        feed = BybitPublicMarketFeed(
            symbols=["BTCUSDT"],
            timeframes=[],
            orderbook_depth=10,
            stream_ticker=True,
            stream_orderbook=True,
            ws_factory=DummyPublicWebSocket,
        )

        started = feed.start()

        self.assertTrue(started)
        self.assertEqual(
            [entry[:2] for entry in feed._ws.subscriptions],
            [("ticker", "BTCUSDT"), ("orderbook", "BTCUSDT")],
        )
        self.assertEqual(feed._ws.subscriptions[1][2], 10)

    def test_start_can_disable_orderbook_and_limit_kline_streams(self):
        feed = BybitPublicMarketFeed(
            symbols=["BTCUSDT"],
            timeframes=["5m"],
            orderbook_depth=10,
            stream_ticker=True,
            stream_orderbook=False,
            ws_factory=DummyPublicWebSocket,
        )

        started = feed.start()

        self.assertTrue(started)
        self.assertEqual(
            [entry[:3] for entry in feed._ws.subscriptions],
            [("ticker", "BTCUSDT", feed._handle_ticker_message), ("kline", "BTCUSDT", "5")],
        )

    def test_diagnostics_track_stream_freshness_counts_and_lag(self):
        feed = ControlledTimePublicFeed(
            symbols=["BTCUSDT"],
            timeframes=["5m"],
            ws_factory=DummyPublicWebSocket,
            ws_stale_after_seconds=30,
        )

        feed.start()
        feed._handle_ticker_message(
            {
                "topic": "tickers.BTCUSDT",
                "ts": 999_980,
                "data": {
                    "symbol": "BTCUSDT",
                    "lastPrice": "30000",
                    "highPrice24h": "30500",
                    "lowPrice24h": "29000",
                },
            }
        )
        feed.advance_ms(10)
        feed._handle_orderbook_message(
            {
                "topic": "orderbook.50.BTCUSDT",
                "ts": 999_995,
                "type": "snapshot",
                "data": {
                    "s": "BTCUSDT",
                    "b": [["29999", "1.5"]],
                    "a": [["30001", "1.2"]],
                },
            }
        )

        diagnostics = feed.get_diagnostics()

        self.assertTrue(diagnostics["started"])
        self.assertEqual(diagnostics["message_counts"]["total"], 2)
        self.assertEqual(diagnostics["message_counts"]["ticker"], 1)
        self.assertEqual(diagnostics["message_counts"]["orderbook"], 1)
        self.assertEqual(diagnostics["cache_sizes"]["ticker_symbols"], 1)
        self.assertEqual(diagnostics["cache_sizes"]["orderbook_symbols"], 1)
        self.assertEqual(diagnostics["stream_ages_ms"]["orderbook"], 0)
        self.assertEqual(diagnostics["symbol_ages_ms"]["BTCUSDT"], 0)
        self.assertEqual(diagnostics["exchange_lag_ms"]["ticker"]["last_ms"], 20.0)
        self.assertEqual(diagnostics["exchange_lag_ms"]["orderbook"]["last_ms"], 15.0)
        self.assertEqual(diagnostics["stale_symbols"], [])


if __name__ == "__main__":
    unittest.main()
