import unittest

from exchange.public_feed import BybitPublicMarketFeed


class DummyWebSocket:
    def __init__(self, *args, **kwargs):
        self.subscriptions = []

    def ticker_stream(self, symbol, callback):
        self.subscriptions.append(("ticker", symbol, callback))

    def orderbook_stream(self, depth, symbol, callback):
        self.subscriptions.append(("orderbook", depth, symbol, callback))

    def kline_stream(self, interval, symbol, callback):
        self.subscriptions.append(("kline", interval, symbol, callback))


class TestPublicFeed(unittest.TestCase):
    def test_start_registers_expected_subscriptions(self):
        feed = BybitPublicMarketFeed(
            symbols=["BTCUSDT"],
            timeframes=["5m", "1h"],
            ws_factory=DummyWebSocket,
        )

        started = feed.start()

        self.assertTrue(started)
        self.assertEqual(len(feed._ws.subscriptions), 4)

    def test_handlers_build_ticker_orderbook_and_kline_caches(self):
        feed = BybitPublicMarketFeed(
            symbols=["BTCUSDT"],
            timeframes=["5m"],
            ws_factory=DummyWebSocket,
        )

        feed._handle_ticker_message(
            {
                "topic": "tickers.BTCUSDT",
                "ts": 1000,
                "data": {
                    "symbol": "BTCUSDT",
                    "lastPrice": "30000",
                    "highPrice24h": "31000",
                    "lowPrice24h": "29000",
                    "price24hPcnt": "0.05",
                    "volume24h": "1200",
                    "turnover24h": "36000000",
                    "markPrice": "30010",
                    "indexPrice": "30005",
                    "fundingRate": "0.0003",
                    "openInterest": "250000",
                },
            }
        )
        feed._handle_orderbook_message(
            {
                "topic": "orderbook.50.BTCUSDT",
                "type": "snapshot",
                "data": {
                    "s": "BTCUSDT",
                    "b": [["29999", "1.5"], ["29998", "2.0"]],
                    "a": [["30001", "1.2"], ["30002", "1.8"]],
                },
            }
        )
        feed._handle_kline_message(
            {
                "topic": "kline.5.BTCUSDT",
                "data": [
                    {
                        "start": 1000,
                        "open": "29900",
                        "high": "30100",
                        "low": "29850",
                        "close": "30000",
                        "volume": "123.4",
                    }
                ],
            }
        )

        ticker = feed.get_ticker("BTCUSDT")
        order_book = feed.get_order_book("BTCUSDT", limit=2)
        klines = feed.get_ohlcv_rows("BTCUSDT", "5m", limit=5)

        self.assertEqual(ticker["last"], "30000")
        self.assertEqual(ticker["percentage"], 5.0)
        self.assertEqual(order_book["bids"][0], [29999.0, 1.5])
        self.assertEqual(order_book["asks"][0], [30001.0, 1.2])
        self.assertEqual(klines[0], [1000, 29900.0, 30100.0, 29850.0, 30000.0, 123.4])
