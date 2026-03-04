import threading
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
        ws_factory: Optional[Any] = None,
    ):
        self.symbols = sorted(set(symbols))
        self.timeframes = timeframes or ["5m", "15m", "1h", "4h"]
        self.orderbook_depth = orderbook_depth
        self.ws_factory = ws_factory or BybitWebSocket
        self._lock = threading.RLock()
        self._started = False
        self._ws = None
        self._ticker_cache: Dict[str, Dict[str, Any]] = {}
        self._orderbook_cache: Dict[str, Dict[str, Dict[float, float]]] = {}
        self._kline_cache: Dict[tuple[str, str], Dict[int, List[float]]] = {}

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
                self._ws.ticker_stream(symbol=symbol, callback=self._handle_ticker_message)
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
            logger.info(
                f"Bybit public WebSocket feed started for {len(self.symbols)} symbols and "
                f"{len(self.timeframes)} timeframes."
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

    def _handle_ticker_message(self, message: Dict[str, Any]):
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

    def _handle_orderbook_message(self, message: Dict[str, Any]):
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

    def _handle_kline_message(self, message: Dict[str, Any]):
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
            self._trim_kline_cache(cache, max_items=240)

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

    def _safe_int(self, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
