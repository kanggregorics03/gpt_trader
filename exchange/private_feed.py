import threading
import time
from typing import Any, Dict, List, Literal, Optional

from loguru import logger

from core.models import AccountState, OpenOrderState, PositionState
from utils.symbols import normalize_symbol, normalize_symbol_list

try:
    from pybit.unified_trading import WebSocket as BybitWebSocket
except ImportError:  # pragma: no cover - optional runtime dependency
    BybitWebSocket = None


WsHealthStatus = Literal["healthy", "stale", "degraded", "reconnecting", "down"]


class BybitPrivateAccountFeed:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        trading_mode: str = "live",
        ws_factory: Optional[Any] = None,
        reconcile_ttl_ms: int = 600_000,
        execution_cache_size: int = 200,
        ws_stale_after_seconds: int = 35,
        ws_recover_stability_seconds: int = 12,
        forced_reconcile_on_recover: bool = True,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.trading_mode = trading_mode
        self.ws_factory = ws_factory or BybitWebSocket
        self.reconcile_ttl_ms = max(1, int(reconcile_ttl_ms or 600_000))
        self.execution_cache_size = max(1, int(execution_cache_size or 200))
        self.ws_stale_after_ms = max(1, int(ws_stale_after_seconds or 35)) * 1000
        self.ws_recover_stability_ms = max(1, int(ws_recover_stability_seconds or 12)) * 1000
        self.forced_reconcile_on_recover = bool(forced_reconcile_on_recover)
        self._ws = None
        self._started = False
        self._lock = threading.RLock()
        self._wallet_state: Dict[str, Any] = {}
        self._positions_by_symbol: Dict[str, PositionState] = {}
        self._open_orders_by_key: Dict[str, OpenOrderState] = {}
        self._recent_executions_by_symbol: Dict[str, List[Dict[str, Any]]] = {}
        self._reconcile_required = False
        self._health_state: WsHealthStatus = "down"
        self._last_ws_message_at = 0
        self._last_private_event_at = 0
        self._last_successful_subscribe_at = 0
        self._last_reconcile_ok_at = 0
        self._recovery_candidate_since = 0
        self._last_health_transition_at = 0
        self._last_error: Optional[str] = None
        self._event_counts = {"total": 0, "wallet": 0, "position": 0, "order": 0, "execution": 0}
        self._handler_stats = {
            "wallet": {"count": 0, "total_ms": 0.0, "max_ms": 0.0},
            "position": {"count": 0, "total_ms": 0.0, "max_ms": 0.0},
            "order": {"count": 0, "total_ms": 0.0, "max_ms": 0.0},
            "execution": {"count": 0, "total_ms": 0.0, "max_ms": 0.0},
        }
        self._exchange_lag_stats = {
            "wallet": {"last_ms": None, "max_ms": 0.0},
            "position": {"last_ms": None, "max_ms": 0.0},
            "order": {"last_ms": None, "max_ms": 0.0},
            "execution": {"last_ms": None, "max_ms": 0.0},
        }

    def _record_transport_activity(self, now_ms: Optional[int] = None):
        with self._lock:
            self._record_transport_activity_locked(now_ms=now_ms)

    def _record_transport_activity_locked(self, now_ms: Optional[int] = None):
        now_ms = self._now_ms() if now_ms is None else int(now_ms)
        self._last_ws_message_at = now_ms

    def _attach_transport_hooks(self, ws_instance: Any):
        if ws_instance is None:
            return

        if hasattr(ws_instance, "_on_message"):
            original_on_message = ws_instance._on_message

            def wrapped_on_message(message):
                self._record_transport_activity()
                return original_on_message(message)

            ws_instance._on_message = wrapped_on_message

        if hasattr(ws_instance, "_on_pong"):
            original_on_pong = ws_instance._on_pong

            def wrapped_on_pong(*args, **kwargs):
                self._record_transport_activity()
                return original_on_pong(*args, **kwargs)

            ws_instance._on_pong = wrapped_on_pong

        if hasattr(ws_instance, "_on_open"):
            original_on_open = ws_instance._on_open

            def wrapped_on_open(*args, **kwargs):
                self._record_transport_activity()
                return original_on_open(*args, **kwargs)

            ws_instance._on_open = wrapped_on_open

    @property
    def available(self) -> bool:
        return self.ws_factory is not None

    def start(self) -> bool:
        if self._started:
            return True
        if not self.available:
            logger.warning("pybit is not installed. Private WebSocket feed disabled; using REST reconcile.")
            with self._lock:
                self._transition_health_locked("down")
            return False

        try:
            self._ws = self.ws_factory(
                testnet=(self.trading_mode == "testnet"),
                channel_type="private",
                api_key=self.api_key,
                api_secret=self.api_secret,
            )
            self._attach_transport_hooks(self._ws)
            self._ws.wallet_stream(callback=self._handle_wallet_message)
            self._ws.position_stream(callback=self._handle_position_message)
            self._ws.order_stream(callback=self._handle_order_message)
            self._ws.execution_stream(callback=self._handle_execution_message)
            now_ms = self._now_ms()
            with self._lock:
                self._started = True
                self._last_successful_subscribe_at = now_ms
                self._recovery_candidate_since = 0
                self._last_error = None
                self._transition_health_locked("reconnecting", now_ms=now_ms)
            logger.info(f"Bybit private WebSocket feed started for trading mode: {self.trading_mode}")
            return True
        except Exception as exc:
            logger.warning(f"Failed to start Bybit private WebSocket feed: {exc}")
            with self._lock:
                self._ws = None
                self._started = False
                self._last_error = str(exc)
                self._transition_health_locked("down")
            return False

    def mark_reconcile_required(self):
        with self._lock:
            self._reconcile_required = True
            if self._started:
                self._transition_health_locked("degraded")

    def seed_from_account_state(self, account_state: AccountState):
        now_ms = self._now_ms()
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
            if self._started and self._recovery_candidate_since <= 0:
                self._recovery_candidate_since = now_ms
            reconcile_ok_at = now_ms
            if self._recovery_candidate_since > 0 and reconcile_ok_at <= self._recovery_candidate_since:
                reconcile_ok_at = self._recovery_candidate_since + 1
            self._last_reconcile_ok_at = reconcile_ok_at
            self._reconcile_required = False
            self._refresh_health_locked(now_ms=now_ms)

    def get_health_status(self) -> WsHealthStatus:
        with self._lock:
            return self._refresh_health_locked()

    def should_use_websocket(self) -> bool:
        return self.get_health_status() == "healthy"

    def needs_periodic_reconcile(self) -> bool:
        with self._lock:
            if self._reconcile_required:
                return True
            if self._last_reconcile_ok_at <= 0:
                return True
            return (self._now_ms() - self._last_reconcile_ok_at) > self.reconcile_ttl_ms

    def get_diagnostics(self) -> Dict[str, Any]:
        with self._lock:
            status = self._refresh_health_locked()
            now_ms = self._now_ms()
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
                "health_status": status,
                "started": self._started,
                "reconcile_required": self._reconcile_required,
                "last_ws_message_at": self._last_ws_message_at,
                "last_ws_message_age_ms": self._age_ms(now_ms, self._last_ws_message_at),
                "last_private_event_at": self._last_private_event_at,
                "last_private_event_age_ms": self._age_ms(now_ms, self._last_private_event_at),
                "last_successful_subscribe_at": self._last_successful_subscribe_at,
                "last_reconcile_ok_at": self._last_reconcile_ok_at,
                "last_reconcile_ok_age_ms": self._age_ms(now_ms, self._last_reconcile_ok_at),
                "recovery_candidate_since": self._recovery_candidate_since,
                "last_health_transition_at": self._last_health_transition_at,
                "last_error": self._last_error,
                "ws_stale_after_ms": self.ws_stale_after_ms,
                "event_counts": dict(self._event_counts),
                "handler_stats": handler_stats,
                "exchange_lag_ms": {
                    stream: {
                        "last_ms": value["last_ms"],
                        "max_ms": round(float(value["max_ms"]), 3),
                    }
                    for stream, value in self._exchange_lag_stats.items()
                },
                "cache_sizes": {
                    "positions": len(self._positions_by_symbol),
                    "open_orders": len(self._open_orders_by_key),
                    "execution_symbols": len(self._recent_executions_by_symbol),
                },
            }

    def get_account_state(self, symbols: List[str]) -> Optional[AccountState]:
        with self._lock:
            if not self._wallet_state:
                return None
            if self._refresh_health_locked() != "healthy":
                return None

            allowed_symbols = set(normalize_symbol_list(symbols))
            total_balance = float(self._wallet_state.get("total_balance") or 0.0)
            positions = [
                position.model_copy(deep=True)
                for symbol, position in self._positions_by_symbol.items()
                if symbol in allowed_symbols
            ]
            for position in positions:
                position.account_unrealized_pnl_pct = self._calculate_account_pnl_pct(
                    unrealized_pnl=position.unrealized_pnl,
                    total_balance=total_balance,
                )
            open_orders = [
                order.model_copy(deep=True)
                for order in self._open_orders_by_key.values()
                if order.symbol in allowed_symbols
            ]
            total_unrealized_pnl = round(sum(position.unrealized_pnl for position in positions), 8)
            return AccountState(
                snapshot_timestamp=self._now_ms(),
                total_balance=total_balance,
                available_balance=float(self._wallet_state.get("available_balance") or 0.0),
                total_unrealized_pnl=total_unrealized_pnl,
                margin_ratio=self._wallet_state.get("margin_ratio"),
                positions=positions,
                open_orders=open_orders,
                open_orders_count=len(open_orders),
            )

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
        started_at = self._now_ms()
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
            self._record_private_event_locked(
                stream="wallet",
                exchange_timestamp_ms=self._message_exchange_timestamp(message, wallet),
                handler_started_at_ms=started_at,
            )
            self._wallet_state = {
                "snapshot_timestamp": self._now_ms(),
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
            self._refresh_health_locked()

    def _handle_position_message(self, message: Dict[str, Any]):
        started_at = self._now_ms()
        data = message.get("data") or []
        if isinstance(data, dict):
            data = [data]

        with self._lock:
            self._record_private_event_locked(
                stream="position",
                exchange_timestamp_ms=self._message_exchange_timestamp(message, *data),
                handler_started_at_ms=started_at,
            )
            for item in data:
                normalized = self._normalize_position(item)
                symbol = normalize_symbol(item.get("symbol") or normalized.symbol)
                if normalized.size <= 0:
                    self._positions_by_symbol.pop(symbol, None)
                else:
                    self._positions_by_symbol[symbol] = normalized
            self._refresh_health_locked()

    def _handle_order_message(self, message: Dict[str, Any]):
        started_at = self._now_ms()
        data = message.get("data") or []
        if isinstance(data, dict):
            data = [data]

        with self._lock:
            self._record_private_event_locked(
                stream="order",
                exchange_timestamp_ms=self._message_exchange_timestamp(message, *data),
                handler_started_at_ms=started_at,
            )
            for item in data:
                normalized = self._normalize_order(item)
                key = self._order_key(normalized)
                if normalized.status.lower() in {"filled", "cancelled", "canceled", "rejected", "deactivated"}:
                    self._open_orders_by_key.pop(key, None)
                else:
                    self._open_orders_by_key[key] = normalized
            self._refresh_health_locked()

    def _handle_execution_message(self, message: Dict[str, Any]):
        started_at = self._now_ms()
        data = message.get("data") or []
        if isinstance(data, dict):
            data = [data]

        with self._lock:
            self._record_private_event_locked(
                stream="execution",
                exchange_timestamp_ms=self._message_exchange_timestamp(message, *data),
                handler_started_at_ms=started_at,
            )
            for item in data:
                normalized = self._normalize_execution(item)
                symbol = normalize_symbol(normalized.get("symbol") or "UNKNOWN")
                bucket = self._recent_executions_by_symbol.setdefault(symbol, [])
                bucket.insert(0, normalized)
                del bucket[self.execution_cache_size :]
            self._refresh_health_locked()

    def _record_private_event_locked(
        self,
        *,
        stream: str,
        exchange_timestamp_ms: Optional[int],
        handler_started_at_ms: int,
    ):
        now_ms = self._now_ms()
        previous_state = self._compute_health_status_locked(now_ms=now_ms)
        self._record_transport_activity_locked(now_ms=now_ms)
        self._last_private_event_at = now_ms
        self._event_counts["total"] += 1
        self._event_counts[stream] += 1
        duration_ms = max(0.0, float(now_ms - handler_started_at_ms))
        stats = self._handler_stats[stream]
        stats["count"] += 1
        stats["total_ms"] += duration_ms
        stats["max_ms"] = max(float(stats["max_ms"]), duration_ms)
        if exchange_timestamp_ms is not None:
            lag_ms = max(0.0, float(now_ms - exchange_timestamp_ms))
            lag_stats = self._exchange_lag_stats[stream]
            lag_stats["last_ms"] = round(lag_ms, 3)
            lag_stats["max_ms"] = max(float(lag_stats["max_ms"]), lag_ms)
        if previous_state in {"stale", "reconnecting", "down"}:
            self._recovery_candidate_since = now_ms
        self._last_error = None

    def _refresh_health_locked(self, now_ms: Optional[int] = None) -> WsHealthStatus:
        now_ms = self._now_ms() if now_ms is None else int(now_ms)
        next_status = self._compute_health_status_locked(now_ms=now_ms)
        self._transition_health_locked(next_status, now_ms=now_ms)
        if next_status == "healthy":
            self._recovery_candidate_since = 0
        return self._health_state

    def _compute_health_status_locked(self, now_ms: Optional[int] = None) -> WsHealthStatus:
        now_ms = self._now_ms() if now_ms is None else int(now_ms)
        if not self._started:
            return "down"
        if self._last_successful_subscribe_at <= 0:
            return "reconnecting"
        if self._last_ws_message_at <= 0:
            if self._last_reconcile_ok_at <= 0 or not self._wallet_state:
                return "reconnecting"
            recovery_since = self._recovery_candidate_since or self._last_successful_subscribe_at or self._last_reconcile_ok_at
            if (
                self.forced_reconcile_on_recover
                and self._last_reconcile_ok_at <= recovery_since
            ):
                return "degraded"
            if (now_ms - recovery_since) < self.ws_recover_stability_ms:
                return "reconnecting"
            if (now_ms - self._last_reconcile_ok_at) > self.reconcile_ttl_ms:
                return "degraded"
            return "healthy"
        if self._reconcile_required:
            return "degraded"
        if self._last_reconcile_ok_at <= 0:
            return "degraded"
        if (now_ms - self._last_ws_message_at) > self.ws_stale_after_ms:
            if (now_ms - self._last_reconcile_ok_at) <= self.reconcile_ttl_ms:
                return "healthy"
            return "stale"
        if self._last_private_event_at <= 0 and (now_ms - self._last_reconcile_ok_at) > self.reconcile_ttl_ms:
            return "degraded"
        if self._recovery_candidate_since > 0:
            if (
                self.forced_reconcile_on_recover
                and self._last_reconcile_ok_at <= self._recovery_candidate_since
            ):
                return "degraded"
            if (now_ms - self._recovery_candidate_since) < self.ws_recover_stability_ms:
                return "reconnecting"
        return "healthy"

    def _transition_health_locked(self, next_status: WsHealthStatus, now_ms: Optional[int] = None):
        now_ms = self._now_ms() if now_ms is None else int(now_ms)
        if next_status == self._health_state:
            return
        self._health_state = next_status
        self._last_health_transition_at = now_ms

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
            symbol=normalize_symbol(item.get("symbol") or "UNKNOWN"),
            side=side,
            size=size,
            entry_price=entry_price,
            mark_price=self._safe_float(item.get("markPrice"), None),
            unrealized_pnl=unrealized_pnl,
            unrealized_pnl_pct=pnl_pct,
            account_unrealized_pnl_pct=self._calculate_account_pnl_pct(
                unrealized_pnl=unrealized_pnl,
                total_balance=self._wallet_state.get("total_balance"),
            ),
            liquidation_price=self._safe_float(item.get("liqPrice"), item.get("liquidationPrice"), None),
            leverage=self._safe_float(item.get("leverage"), None),
            notional_value=notional_value,
            initial_margin=initial_margin,
            maintenance_margin=maintenance_margin,
            margin_mode=margin_mode,
        )

    def _calculate_account_pnl_pct(self, unrealized_pnl: float, total_balance: Any) -> Optional[float]:
        balance = self._safe_float(total_balance, None)
        if balance is None or balance <= 0:
            return None
        return (float(unrealized_pnl) / balance) * 100.0

    def _normalize_order(self, item: Dict[str, Any]) -> OpenOrderState:
        return OpenOrderState(
            symbol=normalize_symbol(item.get("symbol") or "UNKNOWN"),
            order_id=str(item.get("orderId") or "") or None,
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
            "symbol": normalize_symbol(item.get("symbol")),
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

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _message_exchange_timestamp(self, message: Dict[str, Any], *items: Dict[str, Any]) -> Optional[int]:
        direct = self._safe_int(message.get("creationTime"), message.get("ts"))
        if direct is not None:
            return direct
        for item in items:
            if not isinstance(item, dict):
                continue
            timestamp = self._safe_int(
                item.get("creationTime"),
                item.get("updatedTime"),
                item.get("execTime"),
                item.get("createdTime"),
            )
            if timestamp is not None:
                return timestamp
        return None

    def _age_ms(self, now_ms: int, timestamp_ms: int) -> Optional[int]:
        if timestamp_ms <= 0:
            return None
        return max(0, int(now_ms - timestamp_ms))
