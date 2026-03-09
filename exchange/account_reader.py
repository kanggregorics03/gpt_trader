import time
from typing import Any, Dict, List, Optional

import ccxt
from loguru import logger

from core.models import AccountState, OpenOrderState, PositionState
from exchange.private_feed import BybitPrivateAccountFeed
from utils.symbols import normalize_symbol, normalize_symbol_list


class AccountReader:
    def __init__(
        self,
        exchange: ccxt.bybit,
        trading_mode: str = "live",
        cache_ttl_ms: int = 1500,
        private_feed: Optional[BybitPrivateAccountFeed] = None,
    ):
        self.exchange = exchange
        self.trading_mode = trading_mode
        self.cache_ttl_ms = cache_ttl_ms
        self.private_feed = private_feed
        self._account_state_cache: Dict[str, Any] = {
            "cache_key": None,
            "cached_at_ms": 0,
            "state": None,
        }
        logger.info(f"AccountReader connected to Bybit private trading mode: {self.trading_mode}")

    def get_account_state(self, symbols: List[str], force_refresh: bool = False) -> AccountState:
        symbols = normalize_symbol_list(symbols)
        cache_key = tuple(sorted(symbols))
        private_feed = getattr(self, "private_feed", None)
        ws_health = None
        ws_reconcile_due = False

        if private_feed is not None:
            ws_health = private_feed.get_health_status()
            ws_reconcile_due = private_feed.needs_periodic_reconcile()

        if not force_refresh:
            if private_feed is None:
                cached_state = self._get_cached_account_state(cache_key)
                if cached_state is not None:
                    return cached_state
            elif ws_health == "healthy" and not ws_reconcile_due:
                cached_state = self._get_cached_account_state(cache_key)
                if cached_state is not None:
                    return cached_state
                ws_state = private_feed.get_account_state(symbols)
                if ws_state is not None:
                    self._set_cached_account_state(cache_key, ws_state)
                    return ws_state
            else:
                logger.info(
                    f"Private WS account cache unavailable; using REST reconcile "
                    f"(health={ws_health}, reconcile_due={ws_reconcile_due})"
                )

        try:
            # 1. Fetch Balances
            balance = self.exchange.fetch_balance()
            total_balance = balance.get('USDT', {}).get('total', 0.0)
            available_balance = balance.get('USDT', {}).get('free', 0.0)
            account_info = self._get_account_info(balance)
            margin_ratio = self._safe_float(
                account_info.get("accountIMRate"),
                account_info.get("accountMMRate"),
                account_info.get("marginRatio"),
            )

            # 2. Fetch Open Positions
            positions_data = self.exchange.fetch_positions(symbols)
            active_positions = []
            total_unrealized_pnl = 0.0
            for pos in positions_data:
                size = float(pos.get('contracts', 0) or 0)
                if size > 0:
                    info = pos.get("info") or {}
                    side = "LONG" if pos.get('side') == 'long' else "SHORT"
                    entry_price = float(pos.get('entryPrice', 0))
                    unrealized_pnl = float(pos.get('unrealizedPnl', 0))
                    total_unrealized_pnl += unrealized_pnl
                    mark_price = self._safe_float(pos.get("markPrice"), info.get("markPrice"))
                    
                    # Estimate PnL % if possible, ccxt returns absolute PnL
                    # We approximate it based on margin or entry value
                    initial_margin = self._safe_float(pos.get('initialMargin'), info.get("positionIM")) or 0.0
                    maintenance_margin = self._safe_float(
                        pos.get("maintenanceMargin"),
                        info.get("positionMM"),
                    ) or 0.0
                    notional_value = self._safe_float(
                        pos.get("notional"),
                        pos.get("contracts") and pos.get("contractSize") and size * float(pos.get("contractSize") or 0) * entry_price,
                        info.get("positionValue"),
                    ) or (size * entry_price)
                    margin = initial_margin or notional_value
                    pnl_pct = (unrealized_pnl / margin * 100) if margin > 0 else 0.0

                    active_positions.append(PositionState(
                        symbol=normalize_symbol(pos['symbol']),
                        side=side,
                        size=size,
                        entry_price=entry_price,
                        mark_price=mark_price,
                        unrealized_pnl=unrealized_pnl,
                        unrealized_pnl_pct=pnl_pct,
                        liquidation_price=self._safe_float(pos.get("liquidationPrice"), info.get("liqPrice")),
                        leverage=self._safe_float(pos.get("leverage"), info.get("leverage")),
                        notional_value=notional_value,
                        initial_margin=initial_margin,
                        maintenance_margin=maintenance_margin,
                        margin_mode=self._normalize_margin_mode(pos.get("marginMode"), info),
                    ))

            # 3. Fetch Open Orders
            open_orders = self._fetch_open_orders(symbols)

            state = AccountState(
                snapshot_timestamp=int(time.time() * 1000),
                total_balance=total_balance,
                available_balance=available_balance,
                total_unrealized_pnl=round(total_unrealized_pnl, 8),
                margin_ratio=margin_ratio,
                positions=active_positions,
                open_orders=open_orders,
                open_orders_count=len(open_orders),
            )
            if private_feed is not None:
                private_feed.seed_from_account_state(state)
            self._set_cached_account_state(cache_key, state)
            return state

        except Exception as e:
            logger.error(f"Failed to fetch account state: {e}")
            raise

    def invalidate_cache(self):
        self._account_state_cache = {
            "cache_key": None,
            "cached_at_ms": 0,
            "state": None,
        }
        private_feed = getattr(self, "private_feed", None)
        if private_feed is not None:
            private_feed.mark_reconcile_required()

    def get_runtime_diagnostics(self) -> Dict[str, Any]:
        cache_entry = getattr(self, "_account_state_cache", None) or {}
        now_ms = int(time.time() * 1000)
        cached_at_ms = int(cache_entry.get("cached_at_ms") or 0)
        private_feed = getattr(self, "private_feed", None)
        return {
            "cache": {
                "cache_ttl_ms": int(getattr(self, "cache_ttl_ms", 0) or 0),
                "cached_at_ms": cached_at_ms or None,
                "cache_age_ms": (now_ms - cached_at_ms) if cached_at_ms > 0 else None,
                "cached_symbol_count": len(cache_entry.get("cache_key") or ()),
                "has_cached_state": cache_entry.get("state") is not None,
            },
            "private_feed": private_feed.get_diagnostics() if private_feed is not None else None,
        }

    def _get_cached_account_state(self, cache_key: tuple[str, ...]) -> Optional[AccountState]:
        ttl_ms = int(getattr(self, "cache_ttl_ms", 0) or 0)
        if ttl_ms <= 0:
            return None

        cache_entry = getattr(self, "_account_state_cache", None) or {}
        if cache_entry.get("cache_key") != cache_key:
            return None

        cached_state = cache_entry.get("state")
        cached_at_ms = int(cache_entry.get("cached_at_ms") or 0)
        if cached_state is None:
            return None
        if (int(time.time() * 1000) - cached_at_ms) > ttl_ms:
            self.invalidate_cache()
            return None
        return cached_state.model_copy(deep=True)

    def _set_cached_account_state(self, cache_key: tuple[str, ...], state: AccountState):
        self._account_state_cache = {
            "cache_key": cache_key,
            "cached_at_ms": int(time.time() * 1000),
            "state": state.model_copy(deep=True),
        }

    def _fetch_open_orders(self, symbols: List[str]) -> List[OpenOrderState]:
        allowed_symbols = set(normalize_symbol_list(symbols))
        try:
            raw_orders = self.exchange.fetch_open_orders()
        except Exception as exc:
            logger.warning(f"Bulk open-order fetch failed, falling back to per-symbol fetch: {exc}")
            raw_orders = []
            for symbol in symbols:
                raw_orders.extend(self.exchange.fetch_open_orders(symbol))

        normalized_orders = []
        for order in raw_orders:
            normalized = self._normalize_open_order(order)
            if normalized.symbol in allowed_symbols:
                normalized_orders.append(normalized)
        return normalized_orders

    def _get_account_info(self, balance: Dict[str, Any]) -> Dict[str, Any]:
        info = balance.get("info") or {}
        result = info.get("result")
        if isinstance(result, dict):
            account_list = result.get("list") or []
            if account_list and isinstance(account_list[0], dict):
                return account_list[0]
        return {}

    def _normalize_open_order(self, order: Dict[str, Any]) -> OpenOrderState:
        info = order.get("info") or {}
        return OpenOrderState(
            symbol=normalize_symbol(order.get("symbol") or info.get("symbol") or "UNKNOWN"),
            order_id=order.get("id") or info.get("orderId"),
            side=str(order.get("side") or info.get("side") or "UNKNOWN").upper(),
            order_type=str(order.get("type") or info.get("orderType") or "UNKNOWN").upper(),
            quantity=float(order.get("amount") or info.get("qty") or 0.0),
            price=self._safe_float(order.get("price"), info.get("price")),
            reduce_only=bool(order.get("reduceOnly", info.get("reduceOnly", False))),
            status=str(order.get("status") or info.get("orderStatus") or "open").lower(),
            client_order_id=(
                order.get("clientOrderId")
                or info.get("clientOrderId")
                or info.get("orderLinkId")
            ),
            take_profit=self._safe_float(info.get("takeProfit")),
            stop_loss=self._safe_float(info.get("stopLoss")),
            timestamp=order.get("timestamp"),
        )

    def _normalize_margin_mode(self, margin_mode: Optional[str], info: Dict[str, Any]) -> Optional[str]:
        if margin_mode:
            return str(margin_mode).lower()
        trade_mode = str(info.get("tradeMode") or "").strip()
        if trade_mode == "1":
            return "isolated"
        if trade_mode == "0":
            return "cross"
        return None

    def _safe_float(self, *values: Any) -> Optional[float]:
        for value in values:
            if value is None or value == "":
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None
