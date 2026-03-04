from pydantic import BaseModel, Field
from typing import List, Optional, Literal

class MarketSnapshot(BaseModel):
    symbol: str
    current_price: float
    atr_15m: float
    atr_1h: float
    trend_1h: str # "UP", "DOWN", "FLAT"
    recent_volume_surge: bool
    is_trigger_event: bool = False # set to True if volatility or price breached thresholds

class PositionState(BaseModel):
    symbol: str
    side: str # "LONG", "SHORT", "NONE"
    size: float
    entry_price: float
    unrealized_pnl_pct: float

class AccountState(BaseModel):
    total_balance: float
    available_balance: float
    positions: List[PositionState]
    open_orders_count: int

class DecisionRequest(BaseModel):
    policy_summary: str
    account_state: AccountState
    market_snapshot: MarketSnapshot
    recent_journal_summaries: List[str]
    long_term_memory: List[str]
    similar_past_cases: List[str] = Field(default_factory=list)
    is_trigger_mode: bool = False

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
