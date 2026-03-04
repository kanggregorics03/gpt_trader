import ccxt
import time
import uuid
from typing import Any, Dict, List, Optional

from loguru import logger

from core.models import AccountState, OrderIntent, TradeIntent

class ExecutionEngine:
    def __init__(self, exchange: ccxt.bybit):
        self.exchange = exchange

    def execute_intent(
        self,
        intent: TradeIntent,
        account_state: Optional[AccountState] = None,
    ) -> List[dict]:
        """
        Executes orders requested in intent. Risk engine must have already approved this.
        Returns a list of receipt dictionaries from ccxt/Bybit.
        """
        receipts = []
        orders_to_execute = self._resolve_orders(intent, account_state)
        for order in orders_to_execute:
            # 1. Idempotency Key
            client_order_id = f"gpt_{uuid.uuid4().hex[:12]}"
            
            # 2. Map actions to ccxt format
            side = 'buy' if order.action == 'BUY' else 'sell'
            order_type = 'market' if order.order_type == 'MARKET' else 'limit'
            
            params = {
                'clientOrderId': client_order_id,
                'reduceOnly': order.reduce_only
            }
            
            # Add take profit and stop loss if specified (Bybit specific logic)
            if order.take_profit:
                params['takeProfit'] = str(order.take_profit)
            if order.stop_loss:
                params['stopLoss'] = str(order.stop_loss)

            logger.info(f"Executing {side} {order.quantity} {order.symbol} ({order_type}). orderId: {client_order_id}")
            
            try:
                # 3. Create Order
                receipt = self.exchange.create_order(
                    symbol=order.symbol,
                    type=order_type,
                    side=side,
                    amount=order.quantity,
                    price=order.price,
                    params=params
                )
                normalized = self.normalize_receipt(receipt, fallback_client_order_id=client_order_id)
                receipts.append(normalized)
                # Brief sleep to avoid rate limits if multiple orders
                time.sleep(0.5)
                
            except ccxt.RequestTimeout:
                logger.error(f"Timeout executing {client_order_id}. IMPORTANT: We must NOT blindly retry without checking!")
                recovered_order = self._recover_timed_out_order(order.symbol, client_order_id)
                receipts.append({
                    "symbol": order.symbol,
                    "clientOrderId": client_order_id,
                    "status": "recovered" if recovered_order else "timeout_unresolved",
                    "recovered_order": recovered_order,
                })
            except Exception as e:
                logger.error(f"Failed executing order {order}: {e}")
        
        return receipts

    def summarize_receipts(self, receipts: List[Dict[str, Any]]) -> Dict[str, Any]:
        summary = {
            "submitted_orders": len(receipts),
            "filled_orders": 0,
            "open_orders": 0,
            "failed_orders": 0,
            "recovered_orders": 0,
            "unresolved_timeouts": 0,
            "filled_quantity": 0.0,
            "notional_value": 0.0,
            "fee_cost": 0.0,
            "symbols": [],
            "client_order_ids": [],
        }

        symbols = set()
        client_order_ids = []
        for receipt in receipts:
            status = str(receipt.get("status") or "unknown").lower()
            symbol = receipt.get("symbol")
            if symbol:
                symbols.add(symbol)
            client_order_id = receipt.get("clientOrderId")
            if client_order_id:
                client_order_ids.append(client_order_id)

            filled = float(receipt.get("filled") or 0.0)
            average = float(receipt.get("average") or 0.0)
            cost = float(receipt.get("cost") or 0.0)
            fee_cost = float(receipt.get("fee_cost") or 0.0)

            summary["filled_quantity"] += filled
            summary["notional_value"] += cost or (filled * average)
            summary["fee_cost"] += fee_cost

            if status in ("closed", "filled"):
                summary["filled_orders"] += 1
            elif status in ("open", "partially_filled", "partiallyfilled"):
                summary["open_orders"] += 1
            elif status == "recovered":
                summary["recovered_orders"] += 1
            elif status == "timeout_unresolved":
                summary["unresolved_timeouts"] += 1
            elif status in ("canceled", "rejected", "failed"):
                summary["failed_orders"] += 1

        summary["symbols"] = sorted(symbols)
        summary["client_order_ids"] = client_order_ids
        summary["filled_quantity"] = round(summary["filled_quantity"], 8)
        summary["notional_value"] = round(summary["notional_value"], 8)
        summary["fee_cost"] = round(summary["fee_cost"], 8)
        return summary

    def fetch_fills_for_receipts(self, receipts: List[Dict[str, Any]], limit_per_symbol: int = 50) -> List[Dict[str, Any]]:
        if not receipts:
            return []
        if not self.exchange.has.get("fetchMyTrades"):
            return []

        matched_fills: List[Dict[str, Any]] = []
        seen_trade_ids = set()
        for symbol in sorted({receipt.get("symbol") for receipt in receipts if receipt.get("symbol")}):
            try:
                trades = self.exchange.fetch_my_trades(symbol, limit=limit_per_symbol)
            except Exception as exc:
                logger.warning(f"Failed to fetch fills for {symbol}: {exc}")
                continue

            for trade in trades:
                normalized = self.normalize_fill(trade)
                if not self._fill_matches_receipts(normalized, receipts):
                    continue
                trade_id = normalized.get("trade_id") or f"{normalized.get('order_id')}:{normalized.get('timestamp')}"
                if trade_id in seen_trade_ids:
                    continue
                seen_trade_ids.add(trade_id)
                matched_fills.append(normalized)

        return matched_fills

    def _resolve_orders(
        self,
        intent: TradeIntent,
        account_state: Optional[AccountState],
    ) -> List[OrderIntent]:
        if intent.decision != "EXIT" or intent.orders:
            return intent.orders

        if account_state is None:
            logger.warning("EXIT intent received without account state; nothing to close.")
            return []

        close_orders = []
        for position in account_state.positions:
            if position.size <= 0:
                continue
            close_orders.append(
                OrderIntent(
                    symbol=position.symbol,
                    action="SELL" if position.side == "LONG" else "BUY",
                    order_type="MARKET",
                    quantity=position.size,
                    reduce_only=True,
                )
            )
        return close_orders

    def _recover_timed_out_order(self, symbol: str, client_order_id: str) -> Optional[Dict]:
        for fetcher in (self._fetch_open_orders, self._fetch_closed_orders):
            try:
                orders = fetcher(symbol)
            except Exception as exc:
                logger.warning(f"Order recovery fetch failed for {symbol}: {exc}")
                continue
            for order in orders:
                if self._matches_client_order_id(order, client_order_id):
                    return order
        return None

    def _fetch_open_orders(self, symbol: str) -> List[Dict]:
        return self.exchange.fetch_open_orders(symbol)

    def _fetch_closed_orders(self, symbol: str) -> List[Dict]:
        if not self.exchange.has.get("fetchClosedOrders"):
            return []
        return self.exchange.fetch_closed_orders(symbol, limit=20)

    def _matches_client_order_id(self, order: Dict, client_order_id: str) -> bool:
        if order.get("clientOrderId") == client_order_id:
            return True
        info = order.get("info") or {}
        return info.get("clientOrderId") == client_order_id or info.get("orderLinkId") == client_order_id

    def normalize_receipt(
        self,
        receipt: Dict[str, Any],
        fallback_client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        info = receipt.get("info") or {}
        fee = receipt.get("fee") or {}
        return {
            "id": receipt.get("id"),
            "clientOrderId": (
                receipt.get("clientOrderId")
                or info.get("clientOrderId")
                or info.get("orderLinkId")
                or fallback_client_order_id
            ),
            "symbol": receipt.get("symbol") or info.get("symbol"),
            "side": receipt.get("side") or info.get("side"),
            "type": receipt.get("type") or info.get("orderType"),
            "status": receipt.get("status") or info.get("orderStatus") or "unknown",
            "price": receipt.get("price"),
            "average": receipt.get("average"),
            "amount": receipt.get("amount"),
            "filled": receipt.get("filled"),
            "remaining": receipt.get("remaining"),
            "cost": receipt.get("cost"),
            "reduceOnly": bool(
                receipt.get("reduceOnly")
                or info.get("reduceOnly")
                or (receipt.get("params") or {}).get("reduceOnly", False)
            ),
            "fee_cost": fee.get("cost", 0.0) if isinstance(fee, dict) else 0.0,
            "fee_currency": fee.get("currency") if isinstance(fee, dict) else None,
            "timestamp": receipt.get("timestamp"),
        }

    def normalize_fill(self, trade: Dict[str, Any]) -> Dict[str, Any]:
        info = trade.get("info") or {}
        fee = trade.get("fee") or {}
        return {
            "trade_id": trade.get("id") or info.get("execId"),
            "order_id": trade.get("order") or info.get("orderId"),
            "client_order_id": info.get("orderLinkId") or info.get("clientOrderId"),
            "symbol": trade.get("symbol") or info.get("symbol"),
            "side": trade.get("side") or info.get("side"),
            "price": float(trade.get("price") or info.get("execPrice") or 0.0),
            "amount": float(trade.get("amount") or info.get("execQty") or 0.0),
            "cost": float(trade.get("cost") or 0.0),
            "fee_cost": float(fee.get("cost") or info.get("execFee") or 0.0),
            "fee_currency": fee.get("currency") if isinstance(fee, dict) else info.get("feeCurrency"),
            "realized_pnl": self._extract_realized_pnl(trade),
            "timestamp": trade.get("timestamp") or info.get("execTime"),
            "payload": trade,
        }

    def _fill_matches_receipts(self, fill: Dict[str, Any], receipts: List[Dict[str, Any]]) -> bool:
        fill_order_id = fill.get("order_id")
        fill_client_order_id = fill.get("client_order_id")
        fill_symbol = fill.get("symbol")
        for receipt in receipts:
            receipt_order_id = receipt.get("id")
            receipt_client_order_id = receipt.get("clientOrderId")
            if fill_order_id and receipt_order_id and fill_order_id == receipt_order_id:
                return True
            if fill_client_order_id and receipt_client_order_id and fill_client_order_id == receipt_client_order_id:
                return True
            if (
                fill_symbol
                and receipt.get("symbol") == fill_symbol
                and str(receipt.get("status")).lower() in ("closed", "filled", "open", "recovered")
            ):
                if receipt_client_order_id or receipt_order_id:
                    continue
                return True
        return False

    def _extract_realized_pnl(self, trade: Dict[str, Any]) -> float:
        info = trade.get("info") or {}
        for key in ("closedPnl", "closedPnlEv", "execPnl", "realisedPnl", "realizedPnl"):
            value = info.get(key)
            if value not in (None, ""):
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return 0.0
