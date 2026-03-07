import unittest

from exchange.collector import MarketCollector


def build_ohlcv_series(base_price: float, step: float, volume: float, count: int = 80):
    rows = []
    for index in range(count):
        open_price = base_price + (index * step)
        close_price = open_price + (step * 0.6)
        high_price = close_price + (abs(step) * 0.5) + 2
        low_price = open_price - (abs(step) * 0.5) - 2
        candle_volume = volume + index
        rows.append([index, open_price, high_price, low_price, close_price, candle_volume])

    rows[-2][5] = volume * 4
    return rows


class RichExchange:
    has = {"fetchOrderBook": True}

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=80):
        series = {
            "5m": build_ohlcv_series(100, 0.8, 100, count=limit),
            "15m": build_ohlcv_series(100, 1.2, 100, count=limit),
            "1h": build_ohlcv_series(95, 2.5, 200, count=limit),
            "4h": build_ohlcv_series(80, 4.0, 300, count=limit),
        }
        return series[timeframe]

    def fetch_ticker(self, symbol):
        return {
            "last": 205.0,
            "high": 212.0,
            "low": 160.0,
            "percentage": 4.8,
            "baseVolume": 15320.0,
            "quoteVolume": 3012400.0,
            "markPrice": 205.4,
            "indexPrice": 204.9,
        }

    def fetch_order_book(self, symbol, limit=10):
        return {
            "bids": [[204.8, 12], [204.7, 15], [204.6, 11], [204.5, 10], [204.4, 8]],
            "asks": [[205.2, 7], [205.3, 8], [205.4, 7], [205.5, 6], [205.6, 5]],
        }

    def fetch_funding_rate(self, symbol):
        return {
            "fundingRate": 0.0006,
            "nextFundingRate": 0.0005,
            "markPrice": 205.4,
            "indexPrice": 204.9,
        }

    def fetch_open_interest(self, symbol):
        return {
            "openInterestAmount": 125000.0,
            "openInterestValue": 25600000.0,
        }


class MinimalExchange:
    has = {"fetchOrderBook": False}

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=80):
        return build_ohlcv_series(100, 0.5, 50, count=limit)

    def fetch_ticker(self, symbol):
        return {"last": 140.0, "high": 150.0, "low": 120.0}


class HysteresisExchange:
    has = {"fetchOrderBook": False}

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=80):
        rows = []
        for index in range(limit):
            open_price = 100.0
            close_price = 100.0
            high_price = 100.2
            low_price = 99.8
            volume = 100.0
            rows.append([index, open_price, high_price, low_price, close_price, volume])

        if timeframe == "15m":
            rows[-3] = [limit - 3, 100.0, 100.2, 99.8, 100.0, 100.0]
            rows[-2] = [limit - 2, 100.0, 100.5, 99.9, 100.4, 100.0]
            rows[-1] = [limit - 1, 100.0, 100.3, 99.9, 100.1, 100.0]
        return rows

    def fetch_ticker(self, symbol):
        return {"last": 100.1, "high": 101.0, "low": 99.0, "percentage": 0.4}


class VolumeHysteresisExchange:
    has = {"fetchOrderBook": False}

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=80):
        rows = []
        for index in range(limit):
            rows.append([index, 100.0, 100.4, 99.6, 100.0, 100.0])

        if timeframe == "15m" and limit >= 2:
            rows[-2][5] = 140.0
        return rows

    def fetch_ticker(self, symbol):
        return {"last": 100.0, "high": 101.0, "low": 99.0, "percentage": 0.2}


class RangeBreakHysteresisExchange:
    has = {"fetchOrderBook": False}

    def fetch_ohlcv(self, symbol, timeframe="15m", limit=80):
        rows = []
        for index in range(limit):
            open_price = 100.0
            close_price = 100.0
            high_price = 100.6
            low_price = 99.4
            volume = 100.0
            rows.append([index, open_price, high_price, low_price, close_price, volume])

        if timeframe == "1h" and limit >= 3:
            rows[-3] = [limit - 3, 100.0, 100.6, 99.4, 100.0, 100.0]
            rows[-2] = [limit - 2, 100.2, 100.7, 99.9, 100.64, 100.0]
            rows[-1] = [limit - 1, 100.0, 100.5, 99.9, 100.2, 100.0]
        return rows

    def fetch_ticker(self, symbol):
        return {"last": 100.2, "high": 101.0, "low": 99.0, "percentage": 0.2}


class TestMarketCollector(unittest.TestCase):
    def test_market_snapshot_contains_rich_multi_timeframe_context(self):
        collector = MarketCollector(RichExchange())

        snapshot = collector.get_market_snapshot("BTCUSDT")

        self.assertEqual(snapshot.symbol, "BTCUSDT")
        self.assertEqual(set(snapshot.timeframes.keys()), {"5m", "15m", "1h", "4h"})
        self.assertGreater(snapshot.atr_15m, 0.0)
        self.assertGreater(snapshot.atr_1h, 0.0)
        self.assertEqual(snapshot.trend_1h, "UP")
        self.assertTrue(snapshot.recent_volume_surge)
        self.assertTrue(snapshot.is_trigger_event)
        self.assertTrue(snapshot.is_opportunity_trigger)
        self.assertTrue(snapshot.is_risk_trigger)
        self.assertIn("volume_surge_15m", snapshot.opportunity_trigger_reasons)
        self.assertIn("wide_spread", snapshot.risk_trigger_reasons)
        self.assertIn("elevated_funding", snapshot.risk_trigger_reasons)
        self.assertGreater(snapshot.timeframes["15m"].rsi_14, 50.0)
        self.assertIn("last_close", snapshot.timeframes["15m"].previous_values)
        self.assertIn("rsi_14", snapshot.timeframes["15m"].previous_values)
        self.assertIn("volume_ratio", snapshot.timeframes["15m"].previous_values)
        self.assertIn("spread_bps", snapshot.order_book.previous_values)
        self.assertIn("funding_rate", snapshot.derivatives.previous_values)
        self.assertIn("open_interest", snapshot.derivatives.previous_values)
        self.assertIsNotNone(snapshot.order_book.spread_bps)
        self.assertGreater(snapshot.order_book.imbalance_top5, 0.5)
        self.assertIsNotNone(snapshot.derivatives.basis_pct)
        self.assertGreater(snapshot.derivatives.open_interest, 0.0)
        self.assertEqual(snapshot.regime.trend_alignment, "bullish")

    def test_market_snapshot_handles_missing_optional_market_feeds(self):
        collector = MarketCollector(MinimalExchange())

        snapshot = collector.get_market_snapshot("ETHUSDT")

        self.assertEqual(snapshot.symbol, "ETHUSDT")
        self.assertEqual(snapshot.order_book.bid_depth_top5, 0.0)
        self.assertIsNone(snapshot.derivatives.mark_price)
        self.assertIn("15m", snapshot.timeframes)
        self.assertEqual(snapshot.risk_trigger_reasons, [])

    def test_fast_price_move_hysteresis_keeps_trigger_active_within_off_band(self):
        collector = MarketCollector(HysteresisExchange())

        active_snapshot = collector.get_market_snapshot(
            "BTCUSDT",
            trigger_state={"fast_price_move_15m_active": True},
        )
        inactive_snapshot = collector.get_market_snapshot(
            "BTCUSDT",
            trigger_state={"fast_price_move_15m_active": False},
        )

        self.assertIn("fast_price_move_15m", active_snapshot.opportunity_trigger_reasons)
        self.assertNotIn("fast_price_move_15m", inactive_snapshot.opportunity_trigger_reasons)
        self.assertTrue(collector.build_next_trigger_state(active_snapshot)["fast_price_move_15m_active"])
        self.assertFalse(collector.build_next_trigger_state(inactive_snapshot)["fast_price_move_15m_active"])

    def test_volume_surge_hysteresis_keeps_trigger_active_above_release_band(self):
        collector = MarketCollector(VolumeHysteresisExchange())

        active_snapshot = collector.get_market_snapshot(
            "BTCUSDT",
            trigger_state={"volume_surge_15m_active": True},
        )
        inactive_snapshot = collector.get_market_snapshot(
            "BTCUSDT",
            trigger_state={"volume_surge_15m_active": False},
        )

        self.assertIn("volume_surge_15m", active_snapshot.opportunity_trigger_reasons)
        self.assertNotIn("volume_surge_15m", inactive_snapshot.opportunity_trigger_reasons)
        self.assertTrue(collector.build_next_trigger_state(active_snapshot)["volume_surge_15m_active"])
        self.assertFalse(collector.build_next_trigger_state(inactive_snapshot)["volume_surge_15m_active"])

    def test_range_break_hysteresis_keeps_trigger_active_near_release_band(self):
        collector = MarketCollector(RangeBreakHysteresisExchange())

        active_snapshot = collector.get_market_snapshot(
            "BTCUSDT",
            trigger_state={"range_break_1h_active": True},
        )
        inactive_snapshot = collector.get_market_snapshot(
            "BTCUSDT",
            trigger_state={"range_break_1h_active": False},
        )

        self.assertIn("range_break_1h", active_snapshot.opportunity_trigger_reasons)
        self.assertNotIn("range_break_1h", inactive_snapshot.opportunity_trigger_reasons)
        self.assertTrue(collector.build_next_trigger_state(active_snapshot)["range_break_1h_active"])
        self.assertFalse(collector.build_next_trigger_state(inactive_snapshot)["range_break_1h_active"])

    def test_ohlcv_cache_uses_incremental_fetch_after_initial_load(self):
        class RecordingExchange(MinimalExchange):
            def __init__(self):
                self.calls = []

            def fetch_ohlcv(self, symbol, timeframe="15m", limit=80):
                self.calls.append((timeframe, limit))
                return super().fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

        exchange = RecordingExchange()
        collector = MarketCollector(exchange)

        collector.get_market_snapshot("BTCUSDT")
        collector.get_market_snapshot("BTCUSDT")

        first_round = exchange.calls[:4]
        second_round = exchange.calls[4:8]
        self.assertEqual(len(first_round), 4)
        self.assertTrue(all(limit == 80 for _, limit in first_round))
        self.assertEqual(len(second_round), 4)
        self.assertTrue(all(limit <= 3 for _, limit in second_round))

    def test_market_snapshot_supports_lightweight_timeframe_probe(self):
        class RecordingExchange(MinimalExchange):
            def __init__(self):
                self.calls = []

            def fetch_ohlcv(self, symbol, timeframe="15m", limit=80):
                self.calls.append((timeframe, limit))
                return super().fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

        exchange = RecordingExchange()
        collector = MarketCollector(exchange)

        snapshot = collector.get_market_snapshot(
            "BTCUSDT",
            timeframes_to_fetch={"15m": 80, "1h": 80},
        )

        self.assertEqual(set(snapshot.timeframes.keys()), {"15m", "1h"})
        self.assertEqual({timeframe for timeframe, _ in exchange.calls}, {"15m", "1h"})

    def test_market_snapshot_reuses_existing_higher_timeframes_when_refreshing_subset(self):
        collector = MarketCollector(RichExchange())

        baseline = collector.get_market_snapshot("BTCUSDT")
        refreshed = collector.get_market_snapshot(
            "BTCUSDT",
            timeframes_to_fetch={"5m": 80},
            existing_snapshot=baseline,
        )

        self.assertEqual(set(refreshed.timeframes.keys()), {"5m", "15m", "1h", "4h"})
        self.assertEqual(
            refreshed.timeframes["15m"].closed_at,
            baseline.timeframes["15m"].closed_at,
        )
        self.assertEqual(
            refreshed.timeframes["1h"].closed_at,
            baseline.timeframes["1h"].closed_at,
        )

    def test_short_ttl_response_cache_reuses_ticker_and_microstructure_fetches(self):
        class RecordingExchange(RichExchange):
            def __init__(self):
                self.ticker_calls = 0
                self.order_book_calls = 0
                self.funding_calls = 0
                self.open_interest_calls = 0

            def fetch_ticker(self, symbol):
                self.ticker_calls += 1
                return super().fetch_ticker(symbol)

            def fetch_order_book(self, symbol, limit=10):
                self.order_book_calls += 1
                return super().fetch_order_book(symbol, limit=limit)

            def fetch_funding_rate(self, symbol):
                self.funding_calls += 1
                return super().fetch_funding_rate(symbol)

            def fetch_open_interest(self, symbol):
                self.open_interest_calls += 1
                return super().fetch_open_interest(symbol)

        exchange = RecordingExchange()
        collector = MarketCollector(exchange)

        collector.get_market_snapshot("BTCUSDT")
        collector.get_market_snapshot("BTCUSDT")

        self.assertEqual(exchange.ticker_calls, 1)
        self.assertEqual(exchange.order_book_calls, 1)
        self.assertEqual(exchange.funding_calls, 1)
        self.assertEqual(exchange.open_interest_calls, 1)

    def test_public_feed_reduces_rest_calls_for_market_snapshot(self):
        class RecordingExchange(RichExchange):
            def __init__(self):
                self.ticker_calls = 0
                self.order_book_calls = 0
                self.funding_calls = 0
                self.open_interest_calls = 0

            def fetch_ticker(self, symbol):
                self.ticker_calls += 1
                return super().fetch_ticker(symbol)

            def fetch_order_book(self, symbol, limit=10):
                self.order_book_calls += 1
                return super().fetch_order_book(symbol, limit=limit)

            def fetch_funding_rate(self, symbol):
                self.funding_calls += 1
                return super().fetch_funding_rate(symbol)

            def fetch_open_interest(self, symbol):
                self.open_interest_calls += 1
                return super().fetch_open_interest(symbol)

        class FakePublicFeed:
            def get_ticker(self, symbol):
                return {
                    "symbol": symbol,
                    "last": 205.0,
                    "close": 205.0,
                    "high": 212.0,
                    "low": 160.0,
                    "percentage": 4.8,
                    "baseVolume": 15320.0,
                    "quoteVolume": 3012400.0,
                    "markPrice": 205.4,
                    "indexPrice": 204.9,
                    "fundingRate": 0.0006,
                    "openInterest": 125000.0,
                    "openInterestValue": 25600000.0,
                }

            def get_order_book(self, symbol, limit=10):
                return {
                    "bids": [[204.8, 12], [204.7, 15], [204.6, 11], [204.5, 10], [204.4, 8]],
                    "asks": [[205.2, 7], [205.3, 8], [205.4, 7], [205.5, 6], [205.6, 5]],
                }

            def get_ohlcv_rows(self, symbol, timeframe, limit=80):
                return None

        exchange = RecordingExchange()
        collector = MarketCollector(exchange, public_feed=FakePublicFeed())

        snapshot = collector.get_market_snapshot("BTCUSDT")

        self.assertEqual(snapshot.current_price, 205.0)
        self.assertEqual(exchange.ticker_calls, 0)
        self.assertEqual(exchange.order_book_calls, 0)
        self.assertEqual(exchange.funding_calls, 0)
        self.assertEqual(exchange.open_interest_calls, 0)


if __name__ == "__main__":
    unittest.main()
