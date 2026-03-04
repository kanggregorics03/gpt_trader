from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class TimeframeSnapshot(BaseModel):
    timeframe: str
    last_close: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0
    realized_volatility_pct: float = 0.0
    sma_14: float = 0.0
    ema_14: float = 0.0
    rsi_14: float = 50.0
    trend: str = "FLAT"
    change_1bar_pct: float = 0.0
    change_3bar_pct: float = 0.0
    change_6bar_pct: float = 0.0
    range_20_high: float = 0.0
    range_20_low: float = 0.0
    distance_to_range_high_pct: float = 0.0
    distance_to_range_low_pct: float = 0.0
    range_position_pct: float = 50.0
    breakout_up: bool = False
    breakout_down: bool = False
    volume_last: float = 0.0
    volume_mean_20: float = 0.0
    volume_ratio: float = 0.0
    volume_surge: bool = False
    candle_body_pct: float = 0.0
    upper_wick_pct: float = 0.0
    lower_wick_pct: float = 0.0
    close_vs_vwap_pct: float = 0.0


class OrderBookSnapshot(BaseModel):
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    spread_bps: Optional[float] = None
    mid_price: Optional[float] = None
    bid_depth_top5: float = 0.0
    ask_depth_top5: float = 0.0
    bid_notional_top5: float = 0.0
    ask_notional_top5: float = 0.0
    imbalance_top5: float = 0.5
    microprice: Optional[float] = None


class DerivativesSnapshot(BaseModel):
    mark_price: Optional[float] = None
    index_price: Optional[float] = None
    basis_pct: Optional[float] = None
    funding_rate: Optional[float] = None
    next_funding_rate: Optional[float] = None
    open_interest: Optional[float] = None
    open_interest_value: Optional[float] = None


class MarketRegimeSnapshot(BaseModel):
    trend_alignment: str = "mixed"
    volatility_regime: str = "normal"
    momentum_regime: str = "mixed"
    liquidity_regime: str = "balanced"
    opportunity_trigger_reasons: List[str] = Field(default_factory=list)
    risk_trigger_reasons: List[str] = Field(default_factory=list)


class MarketSnapshot(BaseModel):
    symbol: str
    current_price: float
    mark_price: Optional[float] = None
    index_price: Optional[float] = None
    atr_15m: float
    atr_1h: float
    trend_1h: str
    recent_volume_surge: bool
    is_trigger_event: bool = False
    price_change_24h_pct: Optional[float] = None
    high_24h: Optional[float] = None
    low_24h: Optional[float] = None
    base_volume_24h: Optional[float] = None
    quote_volume_24h: Optional[float] = None
    distance_from_24h_high_pct: Optional[float] = None
    distance_from_24h_low_pct: Optional[float] = None
    timeframes: Dict[str, TimeframeSnapshot] = Field(default_factory=dict)
    order_book: OrderBookSnapshot = Field(default_factory=OrderBookSnapshot)
    derivatives: DerivativesSnapshot = Field(default_factory=DerivativesSnapshot)
    regime: MarketRegimeSnapshot = Field(default_factory=MarketRegimeSnapshot)
    is_opportunity_trigger: bool = False
    is_risk_trigger: bool = False
    opportunity_trigger_reasons: List[str] = Field(default_factory=list)
    risk_trigger_reasons: List[str] = Field(default_factory=list)
    trigger_reasons: List[str] = Field(default_factory=list)

class PositionState(BaseModel):
    symbol: str
    side: str # "LONG", "SHORT", "NONE"
    size: float
    entry_price: float
    mark_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float
    liquidation_price: Optional[float] = None
    leverage: Optional[float] = None
    notional_value: float = 0.0
    initial_margin: float = 0.0
    maintenance_margin: float = 0.0
    margin_mode: Optional[str] = None


class OpenOrderState(BaseModel):
    symbol: str
    side: str = "UNKNOWN"
    order_type: str = "UNKNOWN"
    quantity: float = 0.0
    price: Optional[float] = None
    reduce_only: bool = False
    status: str = "open"
    client_order_id: Optional[str] = None
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    timestamp: Optional[int] = None


class RiskStateSnapshot(BaseModel):
    daily_start_balance: float = 0.0
    daily_pnl_pct: float = 0.0
    drawdown_active: bool = False
    drawdown_activate_pct: Optional[float] = None
    consecutive_losses: int = 0
    cooldown_active: bool = False
    cooldown_remaining_minutes: int = 0

class AccountState(BaseModel):
    snapshot_timestamp: Optional[int] = None
    total_balance: float
    available_balance: float
    total_unrealized_pnl: float = 0.0
    margin_ratio: Optional[float] = None
    positions: List[PositionState] = Field(default_factory=list)
    open_orders: List[OpenOrderState] = Field(default_factory=list)
    open_orders_count: int = 0


class MemoryCondition(BaseModel):
    regime: List[str] = Field(default_factory=list)
    volatility: List[str] = Field(default_factory=list)
    trend: List[str] = Field(default_factory=list)


class LongTermMemoryRecord(BaseModel):
    rule: str
    category: str = "lesson"
    status: str = "rule"
    support: int = 0
    regime: str = "general"
    apply_when: MemoryCondition = Field(default_factory=MemoryCondition)
    ignore_when: List[str] = Field(default_factory=list)
    evidence_journal_ids: List[int] = Field(default_factory=list)
    metrics: Dict[str, Any] = Field(default_factory=dict)

class DecisionRequest(BaseModel):
    policy_summary: str
    account_state: AccountState
    market_snapshot: Optional[MarketSnapshot] = None
    market_snapshots: Dict[str, MarketSnapshot] = Field(default_factory=dict)
    risk_state: RiskStateSnapshot
    recent_journal_summaries: List[str]
    long_term_memory: List[LongTermMemoryRecord | str]
    similar_past_cases: List[str] = Field(default_factory=list)
    is_trigger_mode: bool = False
    risk_mode: Literal["normal", "trigger", "cooldown", "drawdown"] = "normal"
    risk_profile: Dict[str, Any] = Field(default_factory=dict)

class OrderIntent(BaseModel):
    symbol: str = Field(description="Trading pair symbol (e.g., BTCUSDT)")
    action: Literal["BUY", "SELL"] = Field(description="Order action")
    order_type: Literal["MARKET", "LIMIT"] = Field(description="Type of order")
    quantity: float = Field(description="Order quantity")
    price: Optional[float] = Field(None, description="Limit price if LIMIT order, null for MARKET")
    reduce_only: bool = Field(False, description="Whether this order is only to reduce position")
    take_profit: Optional[float] = Field(None, description="Take profit price")
    stop_loss: Optional[float] = Field(None, description="Stop loss price")

class TradeIntent(BaseModel):
    decision: Literal["HOLD", "BUY", "SELL", "REDUCE", "EXIT"] = Field(description="Overall strategy decision")
    orders: List[OrderIntent] = Field(default_factory=list, description="List of proposed orders")
    rationale: List[str] = Field(description="Brief reasoning for the decision")
    risk_notes: List[str] = Field(description="Any identified market or strategy risks")
    confidence: float = Field(description="AI confidence score 0.0 to 1.0", ge=0.0, le=1.0)
