import ccxt
import pandas as pd
from typing import Dict, Any, List
from core.models import MarketSnapshot
from loguru import logger

class MarketCollector:
    def __init__(self, exchange: ccxt.bybit):
        self.exchange = exchange

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

    def get_market_snapshot(self, symbol: str) -> MarketSnapshot:
        try:
            # Fetch 15m candles
            # Limit to 20 to compute ATR(14) with a bit of buffer
            ohlcv_15m = self.exchange.fetch_ohlcv(symbol, timeframe='15m', limit=20)
            df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            # Drop the last, unclosed candle usually, unless we want live ATR
            # design doc says "마감된 캔들만 사용" -> We should drop the last one if it's currently forming
            # For simplicity let's assume [-2] is the last completed candle for ATR, or slice df_15m[:-1]
            df_15m_closed = df_15m.iloc[:-1].copy() if len(df_15m) > 1 else df_15m
            atr_15m = self.calculate_atr(df_15m_closed, 14)

            # Fetch 1h candles
            ohlcv_1h = self.exchange.fetch_ohlcv(symbol, timeframe='1h', limit=20)
            df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df_1h_closed = df_1h.iloc[:-1].copy() if len(df_1h) > 1 else df_1h
            atr_1h = self.calculate_atr(df_1h_closed, 14)

            # Determine Trend from 1h (Simple logic: compare close to SMA(14) or just change)
            if len(df_1h_closed) >= 14:
                sma_1h = df_1h_closed['close'].rolling(14).mean().iloc[-1]
                last_close = df_1h_closed['close'].iloc[-1]
                # Allow a 0.5% buffer for FLAT macro trend
                if last_close > sma_1h * 1.005:
                    trend = "UP"
                elif last_close < sma_1h * 0.995:
                    trend = "DOWN"
                else:
                    trend = "FLAT"
            else:
                trend = "FLAT"

            # Recent volume surge (Is last 15m volume > 2x average volume?)
            vol_mean = df_15m_closed['volume'].mean()
            last_vol = df_15m_closed['volume'].iloc[-1]
            surge = last_vol > (vol_mean * 2) if vol_mean > 0 else False

            current_price = self.exchange.fetch_ticker(symbol)['last']
            
            # Trigger Event Detection: e.g. Price jumped > 0.5% in 5 mins (we approximate using current vs last close)
            price_change_pct = abs(current_price - float(df_15m_closed['close'].iloc[-1])) / float(df_15m_closed['close'].iloc[-1]) * 100
            is_trigger = price_change_pct > 0.5 or surge

            return MarketSnapshot(
                symbol=symbol,
                current_price=float(current_price),
                atr_15m=atr_15m,
                atr_1h=atr_1h,
                trend_1h=trend,
                recent_volume_surge=surge,
                is_trigger_event=is_trigger
            )
            
        except Exception as e:
            logger.error(f"Failed to fetch market data for {symbol}: {e}")
            raise
