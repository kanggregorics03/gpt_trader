from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from utils.symbols import normalize_symbol


class TimeframeSnapshot(BaseModel):
    timeframe: str
    closed_at: Optional[int] = None
    last_close: float = 0.0
    previous_values: Dict[str, Optional[float]] = Field(default_factory=dict)
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
    previous_values: Dict[str, Optional[float]] = Field(default_factory=dict)
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
    previous_values: Dict[str, Optional[float]] = Field(default_factory=dict)
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
    fast_move_trigger_active: bool = False
    volume_surge_trigger_active: bool = False
    range_break_trigger_active: bool = False
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
    volume_ratio_15m: Optional[float] = None
    volume_surge_15m_now: bool = False
    fast_move_trigger_active: bool = False
    volume_surge_trigger_active: bool = False
    range_break_trigger_active: bool = False
    oi_change_15m: Optional[float] = None
    oi_change_1h: Optional[float] = None
    funding_rate_change_1h: Optional[float] = None
    basis_change_1h: Optional[float] = None
    spread_vs_1h_avg: Optional[float] = None
    spread_percentile_1h: Optional[float] = None
    depth_percentile_1h: Optional[float] = None
    distance_to_15m_breakout_level: Optional[float] = None
    distance_to_1h_breakout_level: Optional[float] = None
    distance_to_invalid_level: Optional[float] = None
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

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: Any) -> str:
        return normalize_symbol(value)

class PositionState(BaseModel):
    symbol: str
    side: str # "LONG", "SHORT", "NONE"
    size: float
    entry_price: float
    mark_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float
    account_unrealized_pnl_pct: Optional[float] = None
    liquidation_price: Optional[float] = None
    leverage: Optional[float] = None
    notional_value: float = 0.0
    initial_margin: float = 0.0
    maintenance_margin: float = 0.0
    margin_mode: Optional[str] = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: Any) -> str:
        return normalize_symbol(value)


class OpenOrderState(BaseModel):
    symbol: str
    order_id: Optional[str] = None
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

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: Any) -> str:
        return normalize_symbol(value)


class RiskStateSnapshot(BaseModel):
    daily_start_balance: float = 0.0
    daily_pnl_pct: float = 0.0
    drawdown_active: bool = False
    drawdown_activate_pct: Optional[float] = None
    consecutive_losses: int = 0
    last_loss_estimated_pnl_pct: Optional[float] = None
    cooldown_minutes_effective: int = 0
    cooldown_components: List[str] = Field(default_factory=list)
    cooldown_active: bool = False
    cooldown_remaining_minutes: int = 0
    failure_pattern_score: float = 0.0
    dominant_failure_reason: Optional[str] = None
    dominant_failure_count: int = 0
    reentry_guard_active: bool = False
    reentry_guard_symbol: Optional[str] = None
    reentry_guard_reason: Optional[str] = None
    reentry_guard_remaining_minutes: int = 0
    reentry_guard_last_closed_at: Optional[str] = None
    reentry_guard_last_estimated_pnl_pct: Optional[float] = None
    reentry_guard_last_exit_reason: Optional[str] = None


def coerce_risk_state_snapshot(risk_state: Any) -> RiskStateSnapshot:
    if isinstance(risk_state, RiskStateSnapshot):
        return risk_state
    if hasattr(risk_state, "model_dump"):
        return RiskStateSnapshot.model_validate(risk_state.model_dump(mode="json"))
    if isinstance(risk_state, dict):
        return RiskStateSnapshot.model_validate(risk_state)
    return RiskStateSnapshot()

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


class RecentJournalContext(BaseModel):
    journal_id: int
    audit_log_id: Optional[int] = None
    timestamp: Optional[str] = None
    decision: str
    rationale: List[str] = Field(default_factory=list)
    rationale_items: List["RationaleItem"] = Field(default_factory=list)
    confidence: float = 0.0
    risk_reason: str = ""
    estimated_pnl_pct: Optional[float] = None
    loss_signal: bool = False
    ledger_net_realized_pnl: float = 0.0
    ledger_fee_cost: float = 0.0
    risk_approved: Optional[bool] = None
    risk_mode: Optional[str] = None
    trigger_symbols: List[str] = Field(default_factory=list)
    risk_trigger_symbols: List[str] = Field(default_factory=list)
    evidence_quality: Dict[str, Any] = Field(default_factory=dict)
    entry_size_cap: Dict[str, Any] = Field(default_factory=dict)
    summary: Optional[str] = None


class DecisionRequest(BaseModel):
    policy_summary: str
    account_state: AccountState
    market_snapshot: Optional[MarketSnapshot] = None
    market_snapshots: Dict[str, MarketSnapshot] = Field(default_factory=dict)
    risk_state: RiskStateSnapshot
    recent_journal_context: List[RecentJournalContext] = Field(default_factory=list)
    long_term_memory: List[LongTermMemoryRecord | str]
    similar_past_cases: List[str] = Field(default_factory=list)
    is_trigger_mode: bool = False
    risk_mode: Literal["normal", "trigger", "cooldown", "drawdown"] = "normal"
    risk_profile: Dict[str, Any] = Field(default_factory=dict)
    loop_mode: Literal["1h", "15m", "5m"] = "1h"
    cycle_role: Optional[str] = None

class OrderIntent(BaseModel):
    symbol: str = Field(description="Trading pair symbol (e.g., BTCUSDT)")
    action: Literal["BUY", "SELL", "CANCEL"] = Field(description="Order action")
    order_type: Literal["MARKET", "LIMIT"] = Field(description="Type of order")
    quantity: float = Field(default=0.0, description="Order quantity")
    price: Optional[float] = Field(None, description="Limit price if LIMIT order, null for MARKET")
    reduce_only: bool = Field(False, description="Whether this order is only to reduce position")
    take_profit: Optional[float] = Field(None, description="Take profit price")
    stop_loss: Optional[float] = Field(None, description="Stop loss price")
    target_order_id: Optional[str] = Field(
        None,
        description="Exchange order identifier to cancel when action is CANCEL.",
    )
    target_client_order_id: Optional[str] = Field(
        None,
        description="Client order identifier to cancel when action is CANCEL.",
    )
    replace_group_id: Optional[str] = Field(
        None,
        description="Stable lineage group id tying a cancel request to its replacement order.",
    )
    lineage_role: Optional[Literal["cancel", "replacement"]] = Field(
        None,
        description="Whether this order is the cancel leg or replacement leg of a replace-entry flow.",
    )
    lineage_parent_order_id: Optional[str] = Field(
        None,
        description="Original exchange order id this order is linked to for replacement lineage.",
    )
    lineage_parent_client_order_id: Optional[str] = Field(
        None,
        description="Original client order id this order is linked to for replacement lineage.",
    )
    wait_for_cancel_confirmation: bool = Field(
        False,
        description="Whether this order must not be submitted until its linked cancel request is confirmed.",
    )
    execution_profile: Optional[str] = Field(
        None,
        description="Execution profile chosen by the execution policy layer, e.g. market_urgent or limit_passive.",
    )
    urgency_score: Optional[float] = Field(
        None,
        description="Normalized urgency score from 0.0 to 1.0 used for execution policy selection.",
    )
    max_slippage_bps: Optional[float] = Field(
        None,
        description="Maximum adverse slippage budget in basis points for the execution policy.",
    )
    time_in_force: Optional[str] = Field(
        None,
        description="Exchange time-in-force hint for limit orders.",
    )
    post_only: bool = Field(False, description="Whether a limit order should avoid taking liquidity.")
    limit_offset_bps: Optional[float] = Field(
        None,
        description="Offset in basis points from reference price used to construct a limit order.",
    )
    execution_context: Dict[str, Any] = Field(
        default_factory=dict,
        description="Compact execution-policy context persisted for later evaluation.",
    )

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: Any) -> str:
        return normalize_symbol(value)


class EvidenceItem(BaseModel):
    signal: str = Field(description="Observed signal name, e.g. rsi_14 or volume_ratio")
    symbol: Optional[str] = Field(default=None, description="Optional symbol when market_context contains multiple symbols")
    timeframe: str = Field(description="Timeframe associated with the evidence, e.g. 15m, 1h, account, risk")
    operator: Literal["gte", "lte", "gt", "lt", "eq", "cross_up", "cross_down", "delta_abs_gte", "note"] = Field(
        description="Comparison operator connecting observed_value and threshold"
    )
    observed_value: float | str | bool = Field(
        description="Measured scalar value from the request payload. Prefer numeric values, but categorical/bool values are allowed for eq/note evidence."
    )
    threshold: Optional[float | str | bool] = Field(
        default=None,
        description="Comparison threshold when applicable. Numeric operators require numeric thresholds; eq/note may use categorical or bool thresholds.",
    )
    units: Optional[str] = Field(default=None, description="Optional units, e.g. pct, bps, usdt")
    source: str = Field(description="Dot path to the source field in DecisionRequest, e.g. market_snapshots.BTCUSDT.timeframes.15m.rsi_14")
    confidence_contribution: Optional[float] = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="Optional signed relative contribution of this evidence to the final confidence",
    )

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_optional_symbol(cls, value: Any) -> Optional[str]:
        if value in (None, ""):
            return None
        normalized = normalize_symbol(value)
        return normalized or None


class RationaleItem(BaseModel):
    summary: str = Field(description="Short human-readable summary of this rationale item")
    why_it_matters: str = Field(description="Why this evidence matters for the trade decision")
    trade_implication: str = Field(description="How the evidence changes the proposed trade action")
    failure_mode: Optional[str] = Field(
        default=None,
        description="What would invalidate or weaken this rationale item",
    )
    evidence: List[EvidenceItem] = Field(
        default_factory=list,
        description="Machine-checkable factual evidence backing this rationale item",
    )


class TradeIntent(BaseModel):
    decision: Literal["HOLD", "BUY", "SELL", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"] = Field(description="Overall strategy decision")
    orders: List[OrderIntent] = Field(default_factory=list, description="List of proposed orders")
    rationale: List[str] = Field(description="Brief reasoning for the decision")
    rationale_items: List[RationaleItem] = Field(
        default_factory=list,
        description="Structured rationale with machine-checkable evidence and human interpretation",
    )
    risk_notes: List[str] = Field(description="Any identified market or strategy risks")
    confidence: float = Field(description="AI confidence score 0.0 to 1.0", ge=0.0, le=1.0)
    thesis_id: Optional[str] = Field(
        None,
        description="Stable identifier for the thesis that opened or managed the position.",
    )
    thesis_summary: Optional[str] = Field(
        None,
        description="Compact summary of the thesis behind the current trade lifecycle.",
    )
    invalidation_reason: Optional[str] = Field(
        None,
        description="Primary condition that invalidates the current thesis.",
    )
    management_template_id: Optional[str] = Field(
        None,
        description="Identifier for the management template assigned to this trade lifecycle.",
    )
    exit_reason: Optional[str] = Field(
        None,
        description="Structured exit reason when the intent is risk reducing or closing.",
    )
