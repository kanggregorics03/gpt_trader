import threading
import time
from typing import Any, Dict, List, Optional

from loguru import logger

try:
    from pybit.unified_trading import WebSocket as BybitWebSocket
except ImportError:  # pragma: no cover - optional runtime dependency
    BybitWebSocket = None


TIMEFRAME_TO_INTERVAL = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "6h": "360",
    "12h": "720",
    "1d": "D",
}


class BybitPublicMarketFeed:
    def __init__(
        self,
        symbols: List[str],
        timeframes: Optional[List[str]] = None,
        orderbook_depth: int = 50,
        stream_ticker: bool = True,
        stream_orderbook: bool = True,
        max_kline_rows: int = 240,
        ws_factory: Optional[Any] = None,
        ws_stale_after_seconds: int = 30,
    ):
        self.symbols = sorted(set(symbols))
        self.timeframes = list(timeframes) if timeframes is not None else ["5m", "15m", "1h", "4h"]
        self.orderbook_depth = orderbook_depth
        self.stream_ticker = bool(stream_ticker)
        self.stream_orderbook = bool(stream_orderbook)
        self.max_kline_rows = max(20, int(max_kline_rows or 240))
        self.ws_factory = ws_factory or BybitWebSocket
        self.ws_stale_after_ms = max(1, int(ws_stale_after_seconds or 30)) * 1000
        self._lock = threading.RLock()
        self._started = False
        self._ws = None
        self._ticker_cache: Dict[str, Dict[str, Any]] = {}
        self._orderbook_cache: Dict[str, Dict[str, Dict[float, float]]] = {}
        self._kline_cache: Dict[tuple[str, str], Dict[int, List[float]]] = {}
        self._started_at = 0
        self._last_message_at = 0
        self._last_message_by_stream = {"ticker": 0, "orderbook": 0, "kline": 0}
        self._last_message_by_symbol: Dict[str, int] = {}
        self._message_counts = {"total": 0, "ticker": 0, "orderbook": 0, "kline": 0}
        self._handler_stats = {
            "ticker": {"count": 0, "total_ms": 0.0, "max_ms": 0.0},
            "orderbook": {"count": 0, "total_ms": 0.0, "max_ms": 0.0},
            "kline": {"count": 0, "total_ms": 0.0, "max_ms": 0.0},
        }
        self._exchange_lag_stats = {
            "ticker": {"last_ms": None, "max_ms": 0.0},
            "orderbook": {"last_ms": None, "max_ms": 0.0},
            "kline": {"last_ms": None, "max_ms": 0.0},
        }

    @property
    def available(self) -> bool:
        return self.ws_factory is not None

    def start(self) -> bool:
        if self._started:
            return True
        if not self.available:
            logger.warning("pybit is not installed. Public WebSocket feed disabled; using REST fallback.")
            return False

        try:
            self._ws = self.ws_factory(testnet=False, channel_type="linear")
            for symbol in self.symbols:
                if self.stream_ticker:
                    self._ws.ticker_stream(symbol=symbol, callback=self._handle_ticker_message)
                if self.stream_orderbook:
                    self._ws.orderbook_stream(
                        depth=self.orderbook_depth,
                        symbol=symbol,
                        callback=self._handle_orderbook_message,
                    )
                for timeframe in self.timeframes:
                    interval = TIMEFRAME_TO_INTERVAL.get(timeframe)
                    if interval is None:
                        continue
                    self._ws.kline_stream(
                        interval=interval,
                        symbol=symbol,
                        callback=self._handle_kline_message,
                    )
            self._started = True
            self._started_at = self._now_ms()
            logger.info(
                f"Bybit public WebSocket feed started for {len(self.symbols)} symbols and "
                f"{len(self.timeframes)} timeframes (ticker={self.stream_ticker}, "
                f"orderbook={self.stream_orderbook}, depth={self.orderbook_depth})."
            )
            return True
        except Exception as exc:
            logger.warning(f"Failed to start Bybit public WebSocket feed: {exc}")
            self._ws = None
            self._started = False
            return False

    def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cached = self._ticker_cache.get(symbol)
            if not cached:
                return None
            return dict(cached)

    def get_order_book(self, symbol: str, limit: int = 10) -> Optional[Dict[str, Any]]:
        with self._lock:
            state = self._orderbook_cache.get(symbol)
            if not state:
                return None
            bids_map = state.get("bids", {})
            asks_map = state.get("asks", {})
            bids = [[price, size] for price, size in sorted(bids_map.items(), key=lambda item: item[0], reverse=True)[:limit]]
            asks = [[price, size] for price, size in sorted(asks_map.items(), key=lambda item: item[0])[:limit]]
            return {"bids": bids, "asks": asks}

    def get_ohlcv_rows(self, symbol: str, timeframe: str, limit: int = 80) -> Optional[List[List[float]]]:
        with self._lock:
            rows = self._kline_cache.get((symbol, timeframe))
            if not rows:
                return None
            ordered = [rows[key] for key in sorted(rows.keys())]
            return ordered[-limit:]

    def get_diagnostics(self) -> Dict[str, Any]:
        with self._lock:
            now_ms = self._now_ms()
            stream_ages = {
                stream: self._age_ms(now_ms, timestamp)
                for stream, timestamp in self._last_message_by_stream.items()
            }
            symbol_ages = {
                symbol: self._age_ms(now_ms, timestamp)
                for symbol, timestamp in self._last_message_by_symbol.items()
            }
            stale_symbols = sorted(
                symbol
                for symbol, age_ms in symbol_ages.items()
                if age_ms is not None and age_ms > self.ws_stale_after_ms
            )
            handler_stats = {}
            for stream, stats in self._handler_stats.items():
                count = int(stats["count"])
                avg_ms = (float(stats["total_ms"]) / count) if count > 0 else 0.0
                handler_stats[stream] = {
                    "count": count,
                    "avg_ms": round(avg_ms, 3),
                    "max_ms": round(float(stats["max_ms"]), 3),
                }
            return {
                "started": self._started,
                "available": self.available,
                "started_at": self._started_at,
                "ws_stale_after_ms": self.ws_stale_after_ms,
                "last_message_at": self._last_message_at,
                "last_message_age_ms": self._age_ms(now_ms, self._last_message_at),
                "stream_ages_ms": stream_ages,
                "symbol_ages_ms": symbol_ages,
                "stale_symbols": stale_symbols,
                "message_counts": dict(self._message_counts),
                "handler_stats": handler_stats,
                "exchange_lag_ms": {
                    stream: {
                        "last_ms": value["last_ms"],
                        "max_ms": round(float(value["max_ms"]), 3),
                    }
                    for stream, value in self._exchange_lag_stats.items()
                },
                "cache_sizes": {
                    "ticker_symbols": len(self._ticker_cache),
                    "orderbook_symbols": len(self._orderbook_cache),
                    "kline_streams": len(self._kline_cache),
                },
            }

    def _handle_ticker_message(self, message: Dict[str, Any]):
        started_at = self._now_ms()
        data = message.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        symbol = data.get("symbol") or self._topic_symbol(message.get("topic"))
        if not symbol:
            return

        normalized = {
            "symbol": symbol,
            "last": data.get("lastPrice"),
            "close": data.get("lastPrice"),
            "high": data.get("highPrice24h"),
            "low": data.get("lowPrice24h"),
            "percentage": self._scaled_percentage(data.get("price24hPcnt")),
            "baseVolume": data.get("volume24h"),
            "quoteVolume": data.get("turnover24h"),
            "markPrice": data.get("markPrice"),
            "indexPrice": data.get("indexPrice"),
            "bid1Price": data.get("bid1Price"),
            "bid1Size": data.get("bid1Size"),
            "ask1Price": data.get("ask1Price"),
            "ask1Size": data.get("ask1Size"),
            "fundingRate": data.get("fundingRate"),
            "openInterest": data.get("openInterest"),
            "openInterestValue": data.get("openInterestValue"),
            "nextFundingTime": data.get("nextFundingTime"),
            "timestamp": message.get("ts"),
        }

        with self._lock:
            current = dict(self._ticker_cache.get(symbol, {}))
            for key, value in normalized.items():
                if value not in (None, ""):
                    current[key] = value
            self._ticker_cache[symbol] = current
            self._record_message_locked(
                stream="ticker",
                symbol=symbol,
                exchange_timestamp_ms=self._safe_int(message.get("ts")),
                handler_started_at_ms=started_at,
            )

    def _handle_orderbook_message(self, message: Dict[str, Any]):
        started_at = self._now_ms()
        payload = message.get("data") or {}
        symbol = payload.get("s") or self._topic_symbol(message.get("topic"))
        if not symbol:
            return

        message_type = str(message.get("type") or "snapshot").lower()
        bids = payload.get("b") or []
        asks = payload.get("a") or []

        with self._lock:
            if message_type == "snapshot" or symbol not in self._orderbook_cache:
                self._orderbook_cache[symbol] = {"bids": {}, "asks": {}}

            state = self._orderbook_cache[symbol]
            self._apply_orderbook_side(state["bids"], bids)
            self._apply_orderbook_side(state["asks"], asks)
            self._record_message_locked(
                stream="orderbook",
                symbol=symbol,
                exchange_timestamp_ms=self._safe_int(message.get("ts"), payload.get("ts")),
                handler_started_at_ms=started_at,
            )

    def _handle_kline_message(self, message: Dict[str, Any]):
        started_at = self._now_ms()
        topic = str(message.get("topic") or "")
        symbol = self._topic_symbol(topic)
        interval = self._topic_interval(topic)
        timeframe = self._interval_to_timeframe(interval)
        if not symbol or not timeframe:
            return

        rows = message.get("data") or []
        if not isinstance(rows, list):
            return

        with self._lock:
            cache = self._kline_cache.setdefault((symbol, timeframe), {})
            for item in rows:
                start = self._safe_int(item.get("start"))
                if start is None:
                    continue
                cache[start] = [
                    start,
                    self._safe_float(item.get("open"), 0.0),
                    self._safe_float(item.get("high"), 0.0),
                    self._safe_float(item.get("low"), 0.0),
                    self._safe_float(item.get("close"), 0.0),
                    self._safe_float(item.get("volume"), 0.0),
                ]
            self._trim_kline_cache(cache, max_items=self.max_kline_rows)
            self._record_message_locked(
                stream="kline",
                symbol=symbol,
                exchange_timestamp_ms=self._safe_int(message.get("ts")),
                handler_started_at_ms=started_at,
            )

    def _record_message_locked(
        self,
        *,
        stream: str,
        symbol: str,
        exchange_timestamp_ms: Optional[int],
        handler_started_at_ms: int,
    ):
        now_ms = self._now_ms()
        duration_ms = max(0.0, float(now_ms - handler_started_at_ms))
        self._last_message_at = now_ms
        self._last_message_by_stream[stream] = now_ms
        self._last_message_by_symbol[symbol] = now_ms
        self._message_counts["total"] += 1
        self._message_counts[stream] += 1
        stats = self._handler_stats[stream]
        stats["count"] += 1
        stats["total_ms"] += duration_ms
        stats["max_ms"] = max(float(stats["max_ms"]), duration_ms)
        if exchange_timestamp_ms is not None:
            lag_ms = max(0.0, float(now_ms - exchange_timestamp_ms))
            lag_stats = self._exchange_lag_stats[stream]
            lag_stats["last_ms"] = round(lag_ms, 3)
            lag_stats["max_ms"] = max(float(lag_stats["max_ms"]), lag_ms)

    def _apply_orderbook_side(self, side_state: Dict[float, float], deltas: List[List[Any]]):
        for level in deltas:
            if len(level) < 2:
                continue
            price = self._safe_float(level[0], None)
            size = self._safe_float(level[1], None)
            if price is None or size is None:
                continue
            if size == 0:
                side_state.pop(price, None)
            else:
                side_state[price] = size

    def _trim_kline_cache(self, cache: Dict[int, List[float]], max_items: int):
        if len(cache) <= max_items:
            return
        stale_keys = sorted(cache.keys())[:-max_items]
        for key in stale_keys:
            cache.pop(key, None)

    def _topic_symbol(self, topic: Optional[str]) -> Optional[str]:
        if not topic:
            return None
        parts = str(topic).split(".")
        if not parts:
            return None
        return parts[-1]

    def _topic_interval(self, topic: Optional[str]) -> Optional[str]:
        if not topic:
            return None
        parts = str(topic).split(".")
        if len(parts) < 3:
            return None
        return parts[1]

    def _interval_to_timeframe(self, interval: Optional[str]) -> Optional[str]:
        if not interval:
            return None
        inverse = {value: key for key, value in TIMEFRAME_TO_INTERVAL.items()}
        return inverse.get(str(interval))

    def _scaled_percentage(self, value: Any) -> Optional[float]:
        raw = self._safe_float(value, None)
        if raw is None:
            return None
        return raw * 100.0

    def _safe_float(self, value: Any, default: Optional[float]) -> Optional[float]:
        if value in (None, ""):
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _safe_int(self, *values: Any) -> Optional[int]:
        for value in values:
            if value in (None, ""):
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    def _age_ms(self, now_ms: int, timestamp_ms: int) -> Optional[int]:
        if timestamp_ms <= 0:
            return None
        return max(0, int(now_ms - timestamp_ms))

    def _now_ms(self) -> int:
        return int(time.time() * 1000)
