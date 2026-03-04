import threading
import time
from typing import Any, Dict, List, Optional

from loguru import logger

from core.models import AccountState, OpenOrderState, PositionState

try:
    from pybit.unified_trading import WebSocket as BybitWebSocket
except ImportError:  # pragma: no cover - optional runtime dependency
    BybitWebSocket = None


class BybitPrivateAccountFeed:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        trading_mode: str = "live",
        ws_factory: Optional[Any] = None,
        reconcile_ttl_ms: int = 15_000,
        execution_cache_size: int = 200,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.trading_mode = trading_mode
        self.ws_factory = ws_factory or BybitWebSocket
        self.reconcile_ttl_ms = reconcile_ttl_ms
        self.execution_cache_size = execution_cache_size
        self._ws = None
        self._started = False
        self._lock = threading.RLock()
        self._wallet_state: Dict[str, Any] = {}
        self._positions_by_symbol: Dict[str, PositionState] = {}
        self._open_orders_by_key: Dict[str, OpenOrderState] = {}
        self._recent_executions_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        self._last_reconcile_ms = 0

    @property
    def available(self) -> bool:
        return self.ws_factory is not None

    def start(self) -> bool:
        if self._started:
            return True
        if not self.available:
            logger.warning("pybit is not installed. Private WebSocket feed disabled; using REST reconcile.")
            return False

        try:
            self._ws = self.ws_factory(
                testnet=(self.trading_mode == "testnet"),
                channel_type="private",
                api_key=self.api_key,
                api_secret=self.api_secret,
            )
            self._ws.wallet_stream(callback=self._handle_wallet_message)
            self._ws.position_stream(callback=self._handle_position_message)
            self._ws.order_stream(callback=self._handle_order_message)
            self._ws.execution_stream(callback=self._handle_execution_message)
            self._started = True
            logger.info(f"Bybit private WebSocket feed started for trading mode: {self.trading_mode}")
            return True
        except Exception as exc:
            logger.warning(f"Failed to start Bybit private WebSocket feed: {exc}")
            self._ws = None
            self._started = False
            return False

    def mark_reconcile_required(self):
        with self._lock:
            self._last_reconcile_ms = 0

    def seed_from_account_state(self, account_state: AccountState):
        with self._lock:
            self._wallet_state = {
                "snapshot_timestamp": account_state.snapshot_timestamp,
                "total_balance": account_state.total_balance,
                "available_balance": account_state.available_balance,
                "margin_ratio": account_state.margin_ratio,
            }
            self._positions_by_symbol = {
                position.symbol: position.model_copy(deep=True)
                for position in account_state.positions
            }
            self._open_orders_by_key = {
                self._order_key(order): order.model_copy(deep=True)
                for order in account_state.open_orders
            }
            self._last_reconcile_ms = int(time.time() * 1000)

    def get_account_state(self, symbols: List[str]) -> Optional[AccountState]:
        with self._lock:
            if not self._wallet_state or self.needs_reconcile():
                return None

            allowed_symbols = set(symbols)
            positions = [
                position.model_copy(deep=True)
                for symbol, position in self._positions_by_symbol.items()
                if symbol in allowed_symbols
            ]
            open_orders = [
                order.model_copy(deep=True)
                for order in self._open_orders_by_key.values()
                if order.symbol in allowed_symbols
            ]
            total_unrealized_pnl = round(sum(position.unrealized_pnl for position in positions), 8)
            return AccountState(
                snapshot_timestamp=int(time.time() * 1000),
                total_balance=float(self._wallet_state.get("total_balance") or 0.0),
                available_balance=float(self._wallet_state.get("available_balance") or 0.0),
                total_unrealized_pnl=total_unrealized_pnl,
                margin_ratio=self._wallet_state.get("margin_ratio"),
                positions=positions,
                open_orders=open_orders,
                open_orders_count=len(open_orders),
            )

    def needs_reconcile(self) -> bool:
        now_ms = int(time.time() * 1000)
        return (now_ms - int(self._last_reconcile_ms or 0)) > self.reconcile_ttl_ms

    def get_recent_executions(self, symbol: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._recent_executions_by_symbol.get(symbol, [])
            return [dict(row) for row in rows[:limit]]

    def get_recent_orders(self, symbol: str) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "symbol": order.symbol,
                    "clientOrderId": order.client_order_id,
                    "status": order.status,
                    "side": order.side.lower(),
                    "type": order.order_type.lower(),
                    "amount": order.quantity,
                    "price": order.price,
                    "reduceOnly": order.reduce_only,
                    "info": {
                        "symbol": order.symbol,
                        "clientOrderId": order.client_order_id,
                        "orderLinkId": order.client_order_id,
                        "orderStatus": order.status,
                    },
                }
                for order in self._open_orders_by_key.values()
                if order.symbol == symbol
            ]

    def _handle_wallet_message(self, message: Dict[str, Any]):
        data = message.get("data") or []
        if isinstance(data, dict):
            data = [data]
        if not data:
            return

        wallet = data[0]
        coin_entries = wallet.get("coin") or []
        usdt_wallet = next(
            (item for item in coin_entries if str(item.get("coin") or "").upper() == "USDT"),
            {},
        )
        with self._lock:
            self._wallet_state = {
                "snapshot_timestamp": int(time.time() * 1000),
                "total_balance": self._safe_float(
                    wallet.get("totalEquity"),
                    usdt_wallet.get("equity"),
                    self._wallet_state.get("total_balance"),
                )
                or 0.0,
                "available_balance": self._safe_float(
                    wallet.get("totalAvailableBalance"),
                    usdt_wallet.get("availableToWithdraw"),
                    usdt_wallet.get("availableToBorrow"),
                    self._wallet_state.get("available_balance"),
                )
                or 0.0,
                "margin_ratio": self._safe_float(
                    wallet.get("accountIMRate"),
                    wallet.get("marginRatio"),
                    self._wallet_state.get("margin_ratio"),
                ),
            }

    def _handle_position_message(self, message: Dict[str, Any]):
        data = message.get("data") or []
        if isinstance(data, dict):
            data = [data]

        with self._lock:
            for item in data:
                normalized = self._normalize_position(item)
                symbol = str(item.get("symbol") or normalized.symbol)
                if normalized.size <= 0:
                    self._positions_by_symbol.pop(symbol, None)
                else:
                    self._positions_by_symbol[symbol] = normalized

    def _handle_order_message(self, message: Dict[str, Any]):
        data = message.get("data") or []
        if isinstance(data, dict):
            data = [data]

        with self._lock:
            for item in data:
                normalized = self._normalize_order(item)
                key = self._order_key(normalized)
                if normalized.status.lower() in {"filled", "cancelled", "canceled", "rejected", "deactivated"}:
                    self._open_orders_by_key.pop(key, None)
                else:
                    self._open_orders_by_key[key] = normalized

    def _handle_execution_message(self, message: Dict[str, Any]):
        data = message.get("data") or []
        if isinstance(data, dict):
            data = [data]

        with self._lock:
            for item in data:
                normalized = self._normalize_execution(item)
                symbol = normalized.get("symbol") or "UNKNOWN"
                bucket = self._recent_executions_by_symbol.setdefault(symbol, [])
                bucket.insert(0, normalized)
                del bucket[self.execution_cache_size :]

    def _normalize_position(self, item: Dict[str, Any]) -> PositionState:
        side = str(item.get("side") or "").upper()
        side = "LONG" if side in {"BUY", "LONG"} else "SHORT"
        size = abs(self._safe_float(item.get("size"), 0.0) or 0.0)
        entry_price = self._safe_float(item.get("avgPrice"), item.get("entryPrice"), 0.0) or 0.0
        unrealized_pnl = self._safe_float(item.get("unrealisedPnl"), item.get("unrealizedPnl"), 0.0) or 0.0
        initial_margin = self._safe_float(item.get("positionIM"), item.get("positionIm"), 0.0) or 0.0
        maintenance_margin = self._safe_float(item.get("positionMM"), item.get("positionMm"), 0.0) or 0.0
        notional_value = self._safe_float(item.get("positionValue"), item.get("notionalValue"), 0.0) or (
            size * entry_price
        )
        margin = initial_margin or notional_value
        pnl_pct = (unrealized_pnl / margin * 100) if margin > 0 else 0.0
        trade_mode = str(item.get("tradeMode") or "").strip()
        margin_mode = "isolated" if trade_mode == "1" else ("cross" if trade_mode == "0" else None)

        return PositionState(
            symbol=str(item.get("symbol") or "UNKNOWN"),
            side=side,
            size=size,
            entry_price=entry_price,
            mark_price=self._safe_float(item.get("markPrice"), None),
            unrealized_pnl=unrealized_pnl,
            unrealized_pnl_pct=pnl_pct,
            liquidation_price=self._safe_float(item.get("liqPrice"), item.get("liquidationPrice"), None),
            leverage=self._safe_float(item.get("leverage"), None),
            notional_value=notional_value,
            initial_margin=initial_margin,
            maintenance_margin=maintenance_margin,
            margin_mode=margin_mode,
        )

    def _normalize_order(self, item: Dict[str, Any]) -> OpenOrderState:
        return OpenOrderState(
            symbol=str(item.get("symbol") or "UNKNOWN"),
            side=str(item.get("side") or "UNKNOWN").upper(),
            order_type=str(item.get("orderType") or item.get("type") or "UNKNOWN").upper(),
            quantity=self._safe_float(item.get("qty"), item.get("cumExecQty"), 0.0) or 0.0,
            price=self._safe_float(item.get("price"), None),
            reduce_only=bool(item.get("reduceOnly", False)),
            status=str(item.get("orderStatus") or item.get("status") or "open").lower(),
            client_order_id=str(item.get("orderLinkId") or item.get("clientOrderId") or item.get("orderId") or ""),
            take_profit=self._safe_float(item.get("takeProfit"), None),
            stop_loss=self._safe_float(item.get("stopLoss"), None),
            timestamp=self._safe_int(item.get("createdTime"), item.get("updatedTime")),
        )

    def _normalize_execution(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "trade_id": item.get("execId"),
            "order_id": item.get("orderId"),
            "client_order_id": item.get("orderLinkId"),
            "symbol": item.get("symbol"),
            "side": str(item.get("side") or "").lower(),
            "price": self._safe_float(item.get("execPrice"), 0.0) or 0.0,
            "amount": self._safe_float(item.get("execQty"), 0.0) or 0.0,
            "cost": self._safe_float(item.get("execValue"), 0.0) or 0.0,
            "fee_cost": self._safe_float(item.get("execFee"), 0.0) or 0.0,
            "fee_currency": item.get("feeCurrency"),
            "realized_pnl": self._safe_float(
                item.get("execPnl"),
                item.get("closedPnl"),
                item.get("realisedPnl"),
                0.0,
            )
            or 0.0,
            "timestamp": self._safe_int(item.get("execTime")),
            "payload": dict(item),
        }

    def _order_key(self, order: OpenOrderState) -> str:
        return order.client_order_id or f"{order.symbol}:{order.side}:{order.order_type}:{order.price}:{order.quantity}"

    def _safe_float(self, *values: Any) -> Optional[float]:
        for value in values:
            if value is None or value == "":
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _safe_int(self, *values: Any) -> Optional[int]:
        for value in values:
            if value is None or value == "":
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None
