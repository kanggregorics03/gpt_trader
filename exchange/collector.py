import time
from typing import Any, Dict, Optional

import ccxt
import pandas as pd
from loguru import logger

from exchange.public_feed import BybitPublicMarketFeed
from core.models import (
    DerivativesSnapshot,
    MarketRegimeSnapshot,
    MarketSnapshot,
    OrderBookSnapshot,
    TimeframeSnapshot,
)

class MarketCollector:
    DEFAULT_RESPONSE_CACHE_TTLS_MS = {
        "ticker": 5_000,
        "order_book": 5_000,
        "funding_rate": 300_000,
        "open_interest": 30_000,
    }

    def __init__(
        self,
        exchange: ccxt.bybit,
        public_feed: Optional[BybitPublicMarketFeed] = None,
        response_cache_ttls_ms: Optional[Dict[str, int]] = None,
    ):
        self.exchange = exchange
        self.public_feed = public_feed
        cache_ttls = dict(self.DEFAULT_RESPONSE_CACHE_TTLS_MS)
        cache_ttls.update(response_cache_ttls_ms or {})
        self.response_cache_ttls_ms = {
            key: max(250, int(value or self.DEFAULT_RESPONSE_CACHE_TTLS_MS[key]))
            for key, value in cache_ttls.items()
        }
        self._ohlcv_cache: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._response_cache: Dict[tuple[str, str], Dict[str, Any]] = {}
        self._previous_order_books: Dict[str, OrderBookSnapshot] = {}
        self._previous_derivatives: Dict[str, DerivativesSnapshot] = {}

    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        if len(df) < period + 1:
            return 0.0
            
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = ranges.max(axis=1)
        
        atr = true_range.rolling(window=period).mean()
        return float(atr.iloc[-1])

    def calculate_rsi(self, df: pd.DataFrame, period: int = 14) -> float:
        if len(df) < period + 1:
            return 50.0

        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.rolling(window=period).mean()
        avg_loss = loss.rolling(window=period).mean()
        latest_gain = avg_gain.iloc[-1]
        latest_loss = avg_loss.iloc[-1]
        if pd.isna(latest_gain) or pd.isna(latest_loss):
            return 50.0
        if latest_loss == 0:
            return 100.0 if latest_gain > 0 else 50.0
        rs = latest_gain / latest_loss
        return float(100 - (100 / (1 + rs)))

    def calculate_realized_volatility(self, df: pd.DataFrame, window: int = 20) -> float:
        if len(df) < 3:
            return 0.0
        returns = df["close"].pct_change().dropna()
        if returns.empty:
            return 0.0
        return float(returns.tail(window).std(ddof=0) * 100)

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _pct_change(self, current: Any, reference: Any) -> float:
        current_value = self._safe_float(current)
        reference_value = self._safe_float(reference)
        if reference_value == 0:
            return 0.0
        return ((current_value - reference_value) / reference_value) * 100

    def _apply_hysteresis(
        self,
        current_value: float,
        on_threshold: float,
        off_threshold: float,
        was_active: bool,
    ) -> bool:
        if was_active:
            return current_value >= off_threshold
        return current_value >= on_threshold

    def _breakout_trigger_active(
        self,
        tf_snapshot: Optional[TimeframeSnapshot],
        was_active: bool,
    ) -> bool:
        if not tf_snapshot:
            return False

        breakout_buffer_pct = max(0.15, tf_snapshot.atr_pct * 0.35)
        breakout_release_buffer_pct = max(0.05, breakout_buffer_pct * 0.5)
        breakout_up_confirmed = tf_snapshot.distance_to_range_high_pct <= -breakout_buffer_pct
        breakout_down_confirmed = tf_snapshot.distance_to_range_low_pct <= -breakout_buffer_pct
        if breakout_up_confirmed or breakout_down_confirmed:
            return True
        if not was_active:
            return False

        # Keep the breakout trigger alive until price re-enters the prior range
        # by a wider margin than the release buffer.
        near_high_breakout = tf_snapshot.distance_to_range_high_pct <= breakout_release_buffer_pct
        near_low_breakout = tf_snapshot.distance_to_range_low_pct <= breakout_release_buffer_pct
        return near_high_breakout or near_low_breakout

    def _to_dataframe(self, ohlcv: Any) -> pd.DataFrame:
        columns = ["timestamp", "open", "high", "low", "close", "volume"]
        df = pd.DataFrame(ohlcv or [], columns=columns)
        if df.empty:
            return df
        numeric_columns = ["open", "high", "low", "close", "volume"]
        for column in numeric_columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        return df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)

    def _timeframe_to_milliseconds(self, timeframe: str) -> int:
        unit = timeframe[-1]
        value = int(timeframe[:-1])
        unit_multipliers = {
            "m": 60_000,
            "h": 3_600_000,
            "d": 86_400_000,
        }
        return value * unit_multipliers[unit]

    def _merge_ohlcv_rows(self, existing_rows: list, new_rows: list, max_items: int) -> list:
        merged: Dict[Any, Any] = {}
        for row in existing_rows:
            if row:
                merged[row[0]] = row
        for row in new_rows:
            if row:
                merged[row[0]] = row
        ordered = [merged[key] for key in sorted(merged.keys())]
        return ordered[-max_items:]

    def _get_cached_response(self, key: tuple[str, str], ttl_ms: int):
        entry = self._response_cache.get(key)
        if not entry:
            return None
        now_ms = int(time.time() * 1000)
        if now_ms - int(entry.get("cached_at_ms", 0)) > ttl_ms:
            self._response_cache.pop(key, None)
            return None
        return entry.get("value")

    def _set_cached_response(self, key: tuple[str, str], value: Any):
        self._response_cache[key] = {
            "cached_at_ms": int(time.time() * 1000),
            "value": value,
        }

    def _fetch_ohlcv_cached(self, symbol: str, timeframe: str, full_limit: int) -> pd.DataFrame:
        cache_key = (symbol, timeframe)
        cache_entry = self._ohlcv_cache.get(cache_key)
        now_ms = int(time.time() * 1000)
        timeframe_ms = self._timeframe_to_milliseconds(timeframe)
        ws_rows = None
        if self.public_feed is not None:
            ws_rows = self.public_feed.get_ohlcv_rows(symbol, timeframe, limit=full_limit)

        if cache_entry is None:
            rows = ws_rows if ws_rows and len(ws_rows) >= full_limit else self.exchange.fetch_ohlcv(
                symbol,
                timeframe=timeframe,
                limit=full_limit,
            )
            self._ohlcv_cache[cache_key] = {
                "rows": rows[-full_limit:],
                "fetched_at_ms": now_ms,
            }
            return self._to_dataframe(rows)

        if ws_rows:
            rows = ws_rows
        else:
            elapsed_ms = max(0, now_ms - int(cache_entry.get("fetched_at_ms", now_ms)))
            estimated_new_bars = int(elapsed_ms // timeframe_ms)
            incremental_limit = min(full_limit, max(3, estimated_new_bars + 3))
            rows = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=incremental_limit)
        merged_rows = self._merge_ohlcv_rows(cache_entry.get("rows", []), rows, max_items=full_limit)
        self._ohlcv_cache[cache_key] = {
            "rows": merged_rows,
            "fetched_at_ms": now_ms,
        }
        return self._to_dataframe(merged_rows)

    def _closed_candles(self, df: pd.DataFrame) -> pd.DataFrame:
        if len(df) > 1:
            return df.iloc[:-1].copy()
        return df.copy()

    def _infer_trend(self, df: pd.DataFrame, sma_period: int = 14, flat_band_pct: float = 0.5) -> str:
        if len(df) < sma_period:
            return "FLAT"
        sma = df["close"].rolling(sma_period).mean().iloc[-1]
        last_close = df["close"].iloc[-1]
        if pd.isna(sma):
            return "FLAT"
        upper = sma * (1 + flat_band_pct / 100)
        lower = sma * (1 - flat_band_pct / 100)
        if last_close > upper:
            return "UP"
        if last_close < lower:
            return "DOWN"
        return "FLAT"

    def _build_timeframe_snapshot(self, timeframe: str, df_closed: pd.DataFrame) -> TimeframeSnapshot:
        if df_closed.empty:
            return TimeframeSnapshot(timeframe=timeframe)

        last = df_closed.iloc[-1]
        closed_at = int(last["timestamp"]) if "timestamp" in df_closed.columns else None
        prev_close = df_closed["close"].iloc[-2] if len(df_closed) >= 2 else last["close"]
        close_3 = df_closed["close"].iloc[-4] if len(df_closed) >= 4 else prev_close
        close_6 = df_closed["close"].iloc[-7] if len(df_closed) >= 7 else prev_close
        atr = self.calculate_atr(df_closed, 14)
        last_close = self._safe_float(last["close"])
        sma_14 = self._safe_float(df_closed["close"].rolling(14).mean().iloc[-1], last_close)
        ema_14 = self._safe_float(df_closed["close"].ewm(span=14, adjust=False).mean().iloc[-1], last_close)
        rsi_14 = self.calculate_rsi(df_closed, 14)
        realized_volatility_pct = self.calculate_realized_volatility(df_closed, 20)
        volume_mean_20 = self._safe_float(df_closed["volume"].tail(20).mean())
        volume_last = self._safe_float(last["volume"])
        volume_ratio = volume_last / volume_mean_20 if volume_mean_20 > 0 else 0.0
        previous_row = df_closed.iloc[-2] if len(df_closed) >= 2 else last
        previous_rsi = self.calculate_rsi(df_closed.iloc[:-1], 14) if len(df_closed) >= 3 else rsi_14
        previous_volume_mean_20 = self._safe_float(df_closed["volume"].iloc[:-1].tail(20).mean(), volume_mean_20)
        previous_volume_last = self._safe_float(previous_row["volume"], volume_last)
        previous_volume_ratio = (
            previous_volume_last / previous_volume_mean_20 if previous_volume_mean_20 > 0 else volume_ratio
        )

        prior_window = df_closed.iloc[:-1].tail(20) if len(df_closed) > 1 else df_closed.tail(20)
        range_window = df_closed.tail(20)
        range_high = self._safe_float(range_window["high"].max(), last_close)
        range_low = self._safe_float(range_window["low"].min(), last_close)
        prior_high = self._safe_float(prior_window["high"].max(), range_high)
        prior_low = self._safe_float(prior_window["low"].min(), range_low)
        range_span = range_high - range_low
        candle_range = self._safe_float(last["high"] - last["low"])
        body_size = abs(self._safe_float(last["close"]) - self._safe_float(last["open"]))
        upper_wick = self._safe_float(last["high"]) - max(self._safe_float(last["open"]), self._safe_float(last["close"]))
        lower_wick = min(self._safe_float(last["open"]), self._safe_float(last["close"])) - self._safe_float(last["low"])
        typical_price = (df_closed["high"] + df_closed["low"] + df_closed["close"]) / 3
        cumulative_volume = df_closed["volume"].tail(20).sum()
        rolling_vwap = (
            (typical_price.tail(20) * df_closed["volume"].tail(20)).sum() / cumulative_volume
            if cumulative_volume > 0
            else last_close
        )

        return TimeframeSnapshot(
            timeframe=timeframe,
            closed_at=closed_at,
            last_close=last_close,
            previous_values={
                "last_close": self._safe_float(previous_row["close"], last_close),
                "rsi_14": previous_rsi,
                "volume_ratio": previous_volume_ratio,
                "close_vs_vwap_pct": self._pct_change(
                    self._safe_float(previous_row["close"], last_close),
                    rolling_vwap,
                ),
            },
            atr=atr,
            atr_pct=(atr / last_close) * 100 if last_close > 0 else 0.0,
            realized_volatility_pct=realized_volatility_pct,
            sma_14=sma_14,
            ema_14=ema_14,
            rsi_14=rsi_14,
            trend=self._infer_trend(df_closed),
            change_1bar_pct=self._pct_change(last_close, prev_close),
            change_3bar_pct=self._pct_change(last_close, close_3),
            change_6bar_pct=self._pct_change(last_close, close_6),
            range_20_high=range_high,
            range_20_low=range_low,
            distance_to_range_high_pct=self._pct_change(range_high, last_close),
            distance_to_range_low_pct=self._pct_change(last_close, range_low),
            range_position_pct=((last_close - range_low) / range_span * 100) if range_span > 0 else 50.0,
            breakout_up=last_close > prior_high if len(prior_window) > 0 else False,
            breakout_down=last_close < prior_low if len(prior_window) > 0 else False,
            volume_last=volume_last,
            volume_mean_20=volume_mean_20,
            volume_ratio=volume_ratio,
            volume_surge=volume_ratio >= 1.8,
            candle_body_pct=(body_size / candle_range * 100) if candle_range > 0 else 0.0,
            upper_wick_pct=(upper_wick / candle_range * 100) if candle_range > 0 else 0.0,
            lower_wick_pct=(lower_wick / candle_range * 100) if candle_range > 0 else 0.0,
            close_vs_vwap_pct=self._pct_change(last_close, rolling_vwap),
        )

    def _build_order_book_snapshot(self, symbol: str) -> OrderBookSnapshot:
        try:
            order_book = None
            if self.public_feed is not None:
                order_book = self.public_feed.get_order_book(symbol, limit=10)
            if order_book is None:
                has_capability = getattr(self.exchange, "has", {}).get("fetchOrderBook", False)
                if not has_capability:
                    return OrderBookSnapshot()
                cache_key = (symbol, "order_book")
                order_book = self._get_cached_response(
                    cache_key,
                    ttl_ms=self.response_cache_ttls_ms["order_book"],
                )
                if order_book is None:
                    order_book = self.exchange.fetch_order_book(symbol, limit=10)
                    self._set_cached_response(cache_key, order_book)
        except Exception as exc:
            logger.warning(f"Failed to fetch order book for {symbol}: {exc}")
            return OrderBookSnapshot()

        bids = order_book.get("bids") or []
        asks = order_book.get("asks") or []
        best_bid = self._safe_float(bids[0][0], default=None) if bids else None
        best_ask = self._safe_float(asks[0][0], default=None) if asks else None
        bid_size = self._safe_float(bids[0][1]) if bids else 0.0
        ask_size = self._safe_float(asks[0][1]) if asks else 0.0
        mid_price = ((best_bid + best_ask) / 2) if best_bid is not None and best_ask is not None else None
        spread_bps = (((best_ask - best_bid) / mid_price) * 10000) if mid_price and best_ask and best_bid else None
        bid_depth_top5 = sum(self._safe_float(level[1]) for level in bids[:5])
        ask_depth_top5 = sum(self._safe_float(level[1]) for level in asks[:5])
        bid_notional_top5 = sum(self._safe_float(level[0]) * self._safe_float(level[1]) for level in bids[:5])
        ask_notional_top5 = sum(self._safe_float(level[0]) * self._safe_float(level[1]) for level in asks[:5])
        total_depth = bid_depth_top5 + ask_depth_top5
        imbalance_top5 = (bid_depth_top5 / total_depth) if total_depth > 0 else 0.5
        microprice = None
        if best_bid is not None and best_ask is not None and (bid_size + ask_size) > 0:
            microprice = ((best_ask * bid_size) + (best_bid * ask_size)) / (bid_size + ask_size)

        previous_snapshot = self._previous_order_books.get(symbol, OrderBookSnapshot())
        snapshot = OrderBookSnapshot(
            previous_values={
                "best_bid": self._safe_float(previous_snapshot.best_bid, default=best_bid),
                "best_ask": self._safe_float(previous_snapshot.best_ask, default=best_ask),
                "spread_bps": self._safe_float(previous_snapshot.spread_bps, default=spread_bps),
                "imbalance_top5": self._safe_float(previous_snapshot.imbalance_top5, default=imbalance_top5),
                "microprice": self._safe_float(previous_snapshot.microprice, default=microprice),
            },
            best_bid=best_bid,
            best_ask=best_ask,
            spread_bps=spread_bps,
            mid_price=mid_price,
            bid_depth_top5=bid_depth_top5,
            ask_depth_top5=ask_depth_top5,
            bid_notional_top5=bid_notional_top5,
            ask_notional_top5=ask_notional_top5,
            imbalance_top5=imbalance_top5,
            microprice=microprice,
        )
        self._previous_order_books[symbol] = snapshot
        return snapshot

    def _call_optional_exchange_method(self, method_name: str, *args: Any, **kwargs: Any) -> Optional[Dict[str, Any]]:
        has_capability = getattr(self.exchange, "has", {}).get(method_name)
        method = getattr(self.exchange, method_name, None)
        if not method or has_capability is False:
            return None
        try:
            symbol = args[0] if args else "global"
            ttl_by_method = {
                "fetch_funding_rate": self.response_cache_ttls_ms["funding_rate"],
                "fetch_open_interest": self.response_cache_ttls_ms["open_interest"],
            }
            ttl_ms = ttl_by_method.get(method_name, 1_000)
            cache_key = (str(symbol), method_name)
            response = self._get_cached_response(cache_key, ttl_ms=ttl_ms)
            if response is None:
                response = method(*args, **kwargs)
                if isinstance(response, dict):
                    self._set_cached_response(cache_key, response)
            return response if isinstance(response, dict) else None
        except Exception as exc:
            logger.warning(f"Optional market fetch {method_name} failed: {exc}")
            return None

    def _build_derivatives_snapshot(self, symbol: str, ticker: Dict[str, Any]) -> DerivativesSnapshot:
        funding = {}
        open_interest = {}
        if ticker.get("fundingRate") in (None, ""):
            funding = self._call_optional_exchange_method("fetch_funding_rate", symbol) or {}
        if ticker.get("openInterest") in (None, "") and ticker.get("openInterestValue") in (None, ""):
            open_interest = self._call_optional_exchange_method("fetch_open_interest", symbol) or {}
        mark_price = self._safe_float(
            ticker.get("markPrice")
            or ticker.get("mark")
            or funding.get("markPrice")
            or funding.get("mark")
            or open_interest.get("markPrice"),
            default=None,
        )
        index_price = self._safe_float(
            ticker.get("indexPrice")
            or ticker.get("index")
            or funding.get("indexPrice")
            or funding.get("index")
            or open_interest.get("indexPrice"),
            default=None,
        )
        basis_pct = None
        if mark_price not in (None, 0) and index_price not in (None, 0):
            basis_pct = ((mark_price - index_price) / index_price) * 100

        previous_snapshot = self._previous_derivatives.get(symbol, DerivativesSnapshot())
        snapshot = DerivativesSnapshot(
            previous_values={
                "mark_price": self._safe_float(previous_snapshot.mark_price, default=mark_price),
                "index_price": self._safe_float(previous_snapshot.index_price, default=index_price),
                "basis_pct": self._safe_float(previous_snapshot.basis_pct, default=basis_pct),
                "funding_rate": self._safe_float(previous_snapshot.funding_rate, default=None),
                "next_funding_rate": self._safe_float(previous_snapshot.next_funding_rate, default=None),
                "open_interest": self._safe_float(previous_snapshot.open_interest, default=None),
                "open_interest_value": self._safe_float(previous_snapshot.open_interest_value, default=None),
            },
            mark_price=mark_price,
            index_price=index_price,
            basis_pct=basis_pct,
            funding_rate=self._safe_float(
                funding.get("fundingRate") or funding.get("funding_rate"),
                default=None,
            ),
            next_funding_rate=self._safe_float(
                funding.get("nextFundingRate") or funding.get("nextFundingRateRate"),
                default=None,
            ),
            open_interest=self._safe_float(
                open_interest.get("openInterestAmount")
                or open_interest.get("openInterest")
                or open_interest.get("amount"),
                default=None,
            ),
            open_interest_value=self._safe_float(
                open_interest.get("openInterestValue")
                or open_interest.get("value"),
                default=None,
            ),
        )
        self._previous_derivatives[symbol] = snapshot
        return snapshot

    def _build_regime_snapshot(
        self,
        timeframes: Dict[str, TimeframeSnapshot],
        order_book: OrderBookSnapshot,
        price_change_24h_pct: Optional[float],
        derivatives: DerivativesSnapshot,
        trigger_state: Optional[Dict[str, Any]] = None,
    ) -> MarketRegimeSnapshot:
        trigger_state = trigger_state or {}
        trends = [snapshot.trend for snapshot in timeframes.values() if snapshot.last_close > 0]
        trend_alignment = "mixed"
        if trends and all(trend == "UP" for trend in trends):
            trend_alignment = "bullish"
        elif trends and all(trend == "DOWN" for trend in trends):
            trend_alignment = "bearish"

        atr_pcts = [snapshot.atr_pct for snapshot in timeframes.values() if snapshot.atr_pct > 0]
        avg_atr_pct = sum(atr_pcts) / len(atr_pcts) if atr_pcts else 0.0
        if avg_atr_pct >= 2.0:
            volatility_regime = "high"
        elif avg_atr_pct <= 0.5 and atr_pcts:
            volatility_regime = "low"
        else:
            volatility_regime = "normal"
        if price_change_24h_pct is not None and abs(price_change_24h_pct) >= 3.0:
            volatility_regime = "eventful" if volatility_regime == "normal" else volatility_regime

        momentum_regime = "mixed"
        tf_15m = timeframes.get("15m")
        tf_1h = timeframes.get("1h")
        tf_4h = timeframes.get("4h")
        if tf_15m and tf_1h and tf_4h:
            if tf_15m.change_3bar_pct > 0 and tf_1h.change_3bar_pct > 0 and tf_4h.change_3bar_pct > 0:
                momentum_regime = "accelerating_up"
            elif tf_15m.change_3bar_pct < 0 and tf_1h.change_3bar_pct < 0 and tf_4h.change_3bar_pct < 0:
                momentum_regime = "accelerating_down"
            elif abs(tf_15m.change_3bar_pct) < 0.2 and abs(tf_1h.change_3bar_pct) < 0.5:
                momentum_regime = "choppy"

        liquidity_regime = "balanced"
        if order_book.imbalance_top5 >= 0.6:
            liquidity_regime = "bid_dominant"
        elif order_book.imbalance_top5 <= 0.4:
            liquidity_regime = "ask_dominant"

        opportunity_trigger_reasons = []
        risk_trigger_reasons = []
        fast_move_threshold_pct = max(0.5, (tf_15m.atr_pct * 0.8) if tf_15m else 0.5)
        fast_move_off_threshold_pct = max(0.3, fast_move_threshold_pct * 0.6)
        fast_move_active = False
        if tf_15m:
            fast_move_active = self._apply_hysteresis(
                current_value=abs(tf_15m.change_1bar_pct),
                on_threshold=fast_move_threshold_pct,
                off_threshold=fast_move_off_threshold_pct,
                was_active=bool(trigger_state.get("fast_price_move_15m_active")),
            )
        if fast_move_active:
            opportunity_trigger_reasons.append("fast_price_move_15m")
        volume_surge_on_threshold = 1.8
        volume_surge_off_threshold = 1.35
        volume_surge_active = False
        if tf_15m:
            volume_surge_active = self._apply_hysteresis(
                current_value=tf_15m.volume_ratio,
                on_threshold=volume_surge_on_threshold,
                off_threshold=volume_surge_off_threshold,
                was_active=bool(trigger_state.get("volume_surge_15m_active")),
            )
        if volume_surge_active:
            opportunity_trigger_reasons.append("volume_surge_15m")
        range_break_active = self._breakout_trigger_active(
            tf_snapshot=tf_1h,
            was_active=bool(trigger_state.get("range_break_1h_active")),
        )
        if range_break_active:
            opportunity_trigger_reasons.append("range_break_1h")
        if derivatives.funding_rate is not None and abs(derivatives.funding_rate) >= 0.0005:
            risk_trigger_reasons.append("elevated_funding")
        if order_book.spread_bps is not None and order_book.spread_bps >= 10:
            risk_trigger_reasons.append("wide_spread")

        return MarketRegimeSnapshot(
            trend_alignment=trend_alignment,
            volatility_regime=volatility_regime,
            momentum_regime=momentum_regime,
            liquidity_regime=liquidity_regime,
            opportunity_trigger_reasons=opportunity_trigger_reasons,
            risk_trigger_reasons=risk_trigger_reasons,
        )

    def build_next_trigger_state(self, snapshot: MarketSnapshot) -> Dict[str, Any]:
        return {
            "fast_price_move_15m_active": "fast_price_move_15m" in snapshot.opportunity_trigger_reasons,
            "volume_surge_15m_active": "volume_surge_15m" in snapshot.opportunity_trigger_reasons,
            "range_break_1h_active": "range_break_1h" in snapshot.opportunity_trigger_reasons,
        }

    def get_market_snapshot(
        self,
        symbol: str,
        trigger_state: Optional[Dict[str, Any]] = None,
        timeframes_to_fetch: Optional[Dict[str, int]] = None,
        existing_snapshot: Optional[MarketSnapshot] = None,
    ) -> MarketSnapshot:
        try:
            resolved_timeframes = timeframes_to_fetch or {"5m": 80, "15m": 80, "1h": 80, "4h": 80}
            raw_frames = {
                timeframe: self._fetch_ohlcv_cached(symbol, timeframe, full_limit=limit)
                for timeframe, limit in resolved_timeframes.items()
            }
            closed_frames = {
                timeframe: self._closed_candles(df)
                for timeframe, df in raw_frames.items()
            }
            fetched_timeframe_snapshots = {
                timeframe: self._build_timeframe_snapshot(timeframe, df_closed)
                for timeframe, df_closed in closed_frames.items()
            }
            timeframe_snapshots = {
                timeframe: snapshot
                for timeframe, snapshot in ((existing_snapshot.timeframes or {}).items() if existing_snapshot else [])
            }
            timeframe_snapshots.update(fetched_timeframe_snapshots)

            ticker = None
            if self.public_feed is not None:
                ticker = self.public_feed.get_ticker(symbol)
            if ticker is None:
                cache_key = (symbol, "ticker")
                ticker = self._get_cached_response(
                    cache_key,
                    ttl_ms=self.response_cache_ttls_ms["ticker"],
                )
                if ticker is None:
                    ticker = self.exchange.fetch_ticker(symbol) or {}
                    self._set_cached_response(cache_key, ticker)
            current_price = self._safe_float(
                ticker.get("last")
                or ticker.get("close")
                or (timeframe_snapshots.get("15m") or TimeframeSnapshot(timeframe="15m")).last_close
            )
            derivatives = self._build_derivatives_snapshot(symbol, ticker)
            order_book = self._build_order_book_snapshot(symbol)

            high_24h = self._safe_float(ticker.get("high"), default=None)
            low_24h = self._safe_float(ticker.get("low"), default=None)
            price_change_24h_pct = self._safe_float(
                ticker.get("percentage") or ticker.get("change"),
                default=None,
            )
            base_volume_24h = self._safe_float(
                ticker.get("baseVolume") or ticker.get("base_volume"),
                default=None,
            )
            quote_volume_24h = self._safe_float(
                ticker.get("quoteVolume") or ticker.get("quote_volume"),
                default=None,
            )
            distance_from_24h_high_pct = (
                self._pct_change(high_24h, current_price) if high_24h not in (None, 0) else None
            )
            distance_from_24h_low_pct = (
                self._pct_change(current_price, low_24h) if low_24h not in (None, 0) else None
            )

            regime = self._build_regime_snapshot(
                timeframe_snapshots,
                order_book,
                price_change_24h_pct,
                derivatives,
                trigger_state=trigger_state,
            )
            opportunity_trigger_reasons = list(regime.opportunity_trigger_reasons)
            risk_trigger_reasons = list(regime.risk_trigger_reasons)
            trigger_reasons = opportunity_trigger_reasons + risk_trigger_reasons
            is_opportunity_trigger = bool(opportunity_trigger_reasons)
            is_risk_trigger = bool(risk_trigger_reasons)

            tf_15m = timeframe_snapshots.get("15m", TimeframeSnapshot(timeframe="15m"))
            tf_1h = timeframe_snapshots.get("1h", TimeframeSnapshot(timeframe="1h"))
            return MarketSnapshot(
                symbol=symbol,
                current_price=current_price,
                mark_price=derivatives.mark_price,
                index_price=derivatives.index_price,
                atr_15m=tf_15m.atr,
                atr_1h=tf_1h.atr,
                trend_1h=tf_1h.trend,
                recent_volume_surge=tf_15m.volume_surge,
                is_trigger_event=is_opportunity_trigger,
                price_change_24h_pct=price_change_24h_pct,
                high_24h=high_24h,
                low_24h=low_24h,
                base_volume_24h=base_volume_24h,
                quote_volume_24h=quote_volume_24h,
                distance_from_24h_high_pct=distance_from_24h_high_pct,
                distance_from_24h_low_pct=distance_from_24h_low_pct,
                timeframes=timeframe_snapshots,
                order_book=order_book,
                derivatives=derivatives,
                regime=regime,
                is_opportunity_trigger=is_opportunity_trigger,
                is_risk_trigger=is_risk_trigger,
                opportunity_trigger_reasons=opportunity_trigger_reasons,
                risk_trigger_reasons=risk_trigger_reasons,
                trigger_reasons=trigger_reasons,
            )
        except Exception as e:
            logger.error(f"Failed to fetch market data for {symbol}: {e}")
            raise
