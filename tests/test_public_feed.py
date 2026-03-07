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


if __name__ == "__main__":
    unittest.main()
