import ccxt
import time
import uuid
from typing import Any, Dict, List, Optional

from loguru import logger

from core.models import AccountState, OrderIntent, TradeIntent
from exchange.private_feed import BybitPrivateAccountFeed
from utils.symbols import normalize_symbol

class ExecutionEngine:
    def __init__(self, exchange: ccxt.bybit, private_feed: Optional[BybitPrivateAccountFeed] = None):
        self.exchange = exchange
        self.private_feed = private_feed

    def resolve_orders(
        self,
        intent: TradeIntent,
        account_state: Optional[AccountState] = None,
    ) -> List[OrderIntent]:
        return self._resolve_orders(intent, account_state)

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
        orders_to_execute = self.resolve_orders(intent, account_state)
        cancel_status_by_group: Dict[str, str] = {}
        cancel_filled_by_group: Dict[str, float] = {}
        has_unknown_order = False
        for order in orders_to_execute:
            if has_unknown_order and order.action != "CANCEL" and not order.reduce_only:
                deferred = self._build_deferred_replace_receipt(order, reason="unknown_order_requires_recovery")
                deferred["status"] = "deferred_unknown_order"
                receipts.append(deferred)
                continue
            if order.lineage_role == "replacement" and order.wait_for_cancel_confirmation:
                group_id = str(order.replace_group_id or "").strip()
                group_status = cancel_status_by_group.get(group_id, "")
                has_cancel_fills = cancel_filled_by_group.get(group_id, 0.0) > 0
                if not self._is_cancel_confirmed_status(group_status) or has_cancel_fills:
                    receipts.append(
                        self._build_deferred_replace_receipt(
                            order,
                            reason=("cancelled_with_fills_requires_replan" if has_cancel_fills else
                                    f"waiting_for_cancel_confirmation:{group_status or 'missing_cancel_receipt'}"),
                        )
                    )
                    continue
            receipt = self._execute_single_order(order)
            receipts.append(receipt)
            if self._derive_order_state(order, receipt, []) == "unknown":
                has_unknown_order = True
            if order.action == "CANCEL" and order.replace_group_id:
                cancel_status_by_group[str(order.replace_group_id)] = str(receipt.get("status") or "").lower()
                cancel_filled_by_group[str(order.replace_group_id)] = float(receipt.get("filled") or 0.0)

        return receipts

    def build_order_state_transitions(
        self,
        planned_orders: List[OrderIntent],
        receipts: List[Dict[str, Any]],
        fills: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        transitions: List[Dict[str, Any]] = []
        for index, order in enumerate(planned_orders):
            receipt = receipts[index] if index < len(receipts) else {}
            matched_fills = self._get_fills_for_receipt(order, receipt, fills)
            client_order_id = receipt.get("clientOrderId")
            exchange_order_id = receipt.get("id")
            from_state = "submitted" if order.action == "CANCEL" else "none"
            submitted_state = "cancel_requested" if order.action == "CANCEL" else "submitted"
            event_type = "intent_cancel" if order.action == "CANCEL" else "intent_submitted"
            transitions.append(
                {
                    "symbol": order.symbol,
                    "client_order_id": client_order_id,
                    "exchange_order_id": exchange_order_id,
                    "from_state": from_state,
                    "to_state": submitted_state,
                    "event_type": event_type,
                        "payload": {
                            "action": order.action,
                            "order_type": order.order_type,
                            "quantity": order.quantity,
                            "price": order.price,
                            "reduce_only": order.reduce_only,
                            "execution_profile": order.execution_profile,
                            "time_in_force": order.time_in_force,
                            "post_only": bool(order.post_only),
                            "target_order_id": order.target_order_id,
                            "target_client_order_id": order.target_client_order_id,
                            "replace_group_id": order.replace_group_id,
                            "lineage_role": order.lineage_role,
                            "lineage_parent_order_id": order.lineage_parent_order_id,
                            "lineage_parent_client_order_id": order.lineage_parent_client_order_id,
                            "wait_for_cancel_confirmation": bool(order.wait_for_cancel_confirmation),
                        },
                    }
                )
            if str(receipt.get("status") or "").lower() in ("deferred_waiting_cancel", "deferred_unknown_order"):
                transitions[-1]["to_state"] = "deferred"
                transitions[-1]["event_type"] = (
                    "entry_deferred" if receipt.get("status") == "deferred_unknown_order" else "replace_deferred"
                )
                transitions[-1]["payload"]["deferred_reason"] = receipt.get("error")
                continue
            final_state = self._derive_order_state(order, receipt, matched_fills)
            if final_state != submitted_state:
                transitions.append(
                    {
                        "symbol": order.symbol,
                        "client_order_id": client_order_id,
                        "exchange_order_id": exchange_order_id,
                        "from_state": submitted_state,
                        "to_state": final_state,
                        "event_type": "exchange_update",
                        "payload": {
                            "receipt_status": receipt.get("status"),
                            "filled_quantity": round(
                                sum(float(fill.get("amount") or 0.0) for fill in matched_fills),
                                8,
                            ),
                            "fill_count": len(matched_fills),
                            "receipt": receipt,
                            "replace_group_id": order.replace_group_id,
                            "lineage_role": order.lineage_role,
                            "lineage_parent_order_id": order.lineage_parent_order_id,
                            "lineage_parent_client_order_id": order.lineage_parent_client_order_id,
                        },
                    }
                )
        return transitions

    def build_position_state_transitions(
        self,
        intent: TradeIntent,
        account_state: AccountState,
        post_account_state: Optional[AccountState],
        planned_orders: List[OrderIntent],
        receipts: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if intent.decision in ("HOLD", "CANCEL", "REPLACE_ENTRY"):
            return []

        pre_positions = {position.symbol: position for position in account_state.positions}
        post_positions = {
            position.symbol: position
            for position in (post_account_state.positions if post_account_state else [])
        }
        statuses_by_symbol: Dict[str, List[str]] = {}
        for receipt in receipts:
            symbol = receipt.get("symbol")
            if not symbol:
                continue
            statuses_by_symbol.setdefault(symbol, []).append(str(receipt.get("status") or "unknown").lower())

        transitions = []
        symbols = sorted({order.symbol for order in planned_orders})
        for symbol in symbols:
            before = pre_positions.get(symbol)
            after = post_positions.get(symbol)
            before_size = abs(float(before.size)) if before else 0.0
            after_size = abs(float(after.size)) if after else 0.0
            from_state = "open" if before_size > 0 else "flat"
            statuses = statuses_by_symbol.get(symbol, [])

            if any(status == "timeout_unresolved" for status in statuses):
                to_state = "unknown"
            elif intent.decision in ("BUY", "SELL"):
                if after_size > 0:
                    to_state = "open"
                elif any(status in ("closed", "filled", "open", "partially_filled", "partiallyfilled") for status in statuses):
                    to_state = "opening"
                else:
                    to_state = from_state
            else:
                if after_size == 0:
                    to_state = "closed"
                elif before_size > 0 and after_size < before_size:
                    to_state = "reducing"
                elif any(status in ("open", "partially_filled", "partiallyfilled") for status in statuses):
                    to_state = "reducing"
                else:
                    to_state = "open"

            transitions.append(
                {
                    "symbol": symbol,
                    "from_state": from_state,
                    "to_state": to_state,
                    "event_type": f"intent_{intent.decision.lower()}",
                    "payload": {
                        "before_size": before_size,
                        "after_size": after_size,
                        "receipt_statuses": statuses,
                    },
                }
            )
        return transitions

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
            elif status in ("deferred_waiting_cancel", "deferred_unknown_order"):
                continue
            elif status in ("rejected", "failed"):
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

        matched_fills: List[Dict[str, Any]] = []
        seen_trade_ids = set()
        for symbol in sorted({receipt.get("symbol") for receipt in receipts if receipt.get("symbol")}):
            if self.private_feed is not None:
                recent_executions = self.private_feed.get_recent_executions(symbol, limit=limit_per_symbol)
                self._append_matching_fills(recent_executions, receipts, matched_fills, seen_trade_ids)
                if any(fill.get("symbol") == symbol for fill in matched_fills):
                    continue

            if not self.exchange.has.get("fetchMyTrades"):
                continue

            try:
                trades = self.exchange.fetch_my_trades(symbol, limit=limit_per_symbol)
            except Exception as exc:
                logger.warning(f"Failed to fetch fills for {symbol}: {exc}")
                continue

            normalized_trades = [self.normalize_fill(trade) for trade in trades]
            self._append_matching_fills(normalized_trades, receipts, matched_fills, seen_trade_ids)

        return matched_fills

    def _resolve_orders(
        self,
        intent: TradeIntent,
        account_state: Optional[AccountState],
    ) -> List[OrderIntent]:
        if intent.decision == "CANCEL":
            return intent.orders
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
        if self.private_feed is not None:
            try:
                if self.private_feed.get_health_status() == "healthy":
                    for order in self.private_feed.get_recent_orders(symbol):
                        if self._matches_client_order_id(order, client_order_id):
                            return order
            except Exception as exc:
                logger.warning("WS order recovery unavailable for {}: {}", symbol, type(exc).__name__)
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

    def _is_cancel_confirmed_status(self, status: str) -> bool:
        return str(status or "").lower() in ("canceled", "cancelled")

    def _build_deferred_replace_receipt(self, order: OrderIntent, reason: str) -> Dict[str, Any]:
        return {
            "symbol": order.symbol,
            "clientOrderId": None,
            "id": None,
            "status": "deferred_waiting_cancel",
            "action": order.action,
            "type": order.order_type,
            "amount": order.quantity,
            "price": order.price,
            "reduceOnly": order.reduce_only,
            "timeInForce": order.time_in_force,
            "postOnly": bool(order.post_only),
            "executionProfile": order.execution_profile,
            "replaceGroupId": order.replace_group_id,
            "lineageRole": order.lineage_role,
            "lineageParentOrderId": order.lineage_parent_order_id,
            "lineageParentClientOrderId": order.lineage_parent_client_order_id,
            "error": reason,
        }

    def _execute_single_order(self, order: OrderIntent) -> Dict[str, Any]:
        if order.action == "CANCEL":
            cancel_target_id = order.target_order_id or order.target_client_order_id
            params = {}
            if order.target_client_order_id:
                params["orderLinkId"] = str(order.target_client_order_id)
                params["clientOrderId"] = str(order.target_client_order_id)
            logger.info(
                f"Cancelling order for {order.symbol}. target={cancel_target_id or 'unknown'}"
            )
            try:
                receipt = self.exchange.cancel_order(
                    cancel_target_id,
                    order.symbol,
                    params=params,
                )
                normalized = self.normalize_receipt(
                    receipt,
                    fallback_client_order_id=order.target_client_order_id,
                )
                if not normalized.get("status"):
                    normalized["status"] = "canceled"
                return self._enrich_receipt_with_lineage(normalized, order)
            except ccxt.NetworkError:
                logger.error(
                    f"Timeout cancelling {order.symbol} target={cancel_target_id or 'unknown'}."
                )
                return self._enrich_receipt_with_lineage(
                    {
                        "symbol": order.symbol,
                        "clientOrderId": order.target_client_order_id,
                        "id": order.target_order_id,
                        "status": "timeout_unresolved",
                        "action": "CANCEL",
                        "error": "cancel_timeout",
                    },
                    order,
                )
            except Exception as e:
                logger.error(f"Failed cancelling order {order}: {e}")
                return self._enrich_receipt_with_lineage(
                    {
                        "symbol": order.symbol,
                        "clientOrderId": order.target_client_order_id,
                        "id": order.target_order_id,
                        "status": "failed",
                        "action": "CANCEL",
                        "error": str(e),
                    },
                    order,
                )

        client_order_id = f"gpt_{uuid.uuid4().hex[:12]}"
        side = 'buy' if order.action == 'BUY' else 'sell'
        order_type = 'market' if order.order_type == 'MARKET' else 'limit'

        params = {
            'clientOrderId': client_order_id,
            'reduceOnly': order.reduce_only
        }
        if order.time_in_force:
            params['timeInForce'] = str(order.time_in_force)
        if bool(order.post_only):
            params['postOnly'] = True
        if order.execution_profile:
            params['executionProfile'] = str(order.execution_profile)
        if order.take_profit:
            params['takeProfit'] = str(order.take_profit)
        if order.stop_loss:
            params['stopLoss'] = str(order.stop_loss)

        logger.info(f"Executing {side} {order.quantity} {order.symbol} ({order_type}). orderId: {client_order_id}")
        try:
            receipt = self.exchange.create_order(
                symbol=order.symbol,
                type=order_type,
                side=side,
                amount=order.quantity,
                price=order.price,
                params=params
            )
            normalized = self.normalize_receipt(receipt, fallback_client_order_id=client_order_id)
            time.sleep(0.5)
            return self._enrich_receipt_with_lineage(normalized, order)
        except ccxt.NetworkError:
            logger.error(f"Timeout executing {client_order_id}. IMPORTANT: We must NOT blindly retry without checking!")
            recovered_order = self._recover_timed_out_order(order.symbol, client_order_id)
            recovered_receipt = (
                self.normalize_receipt(recovered_order, fallback_client_order_id=client_order_id)
                if recovered_order else {}
            )
            return self._enrich_receipt_with_lineage(
                {
                    **recovered_receipt,
                    "symbol": order.symbol,
                    "clientOrderId": client_order_id,
                    "status": "recovered" if recovered_order else "timeout_unresolved",
                    "recovered_order": recovered_order,
                },
                order,
            )
        except Exception as e:
            logger.error(f"Failed executing order {order}: {e}")
            return self._enrich_receipt_with_lineage(
                {
                    "symbol": order.symbol,
                    "clientOrderId": client_order_id,
                    "status": "failed",
                    "side": side,
                    "type": order_type,
                    "amount": order.quantity,
                    "price": order.price,
                    "reduceOnly": order.reduce_only,
                    "timeInForce": order.time_in_force,
                    "postOnly": bool(order.post_only),
                    "executionProfile": order.execution_profile,
                    "error": str(e),
                },
                order,
            )

    def _enrich_receipt_with_lineage(self, receipt: Dict[str, Any], order: OrderIntent) -> Dict[str, Any]:
        enriched = dict(receipt or {})
        enriched["replaceGroupId"] = order.replace_group_id
        enriched["lineageRole"] = order.lineage_role
        enriched["lineageParentOrderId"] = order.lineage_parent_order_id
        enriched["lineageParentClientOrderId"] = order.lineage_parent_client_order_id
        return enriched

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
            "symbol": normalize_symbol(receipt.get("symbol") or info.get("symbol")),
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
            "timeInForce": receipt.get("timeInForce") or info.get("timeInForce"),
            "postOnly": bool(
                receipt.get("postOnly")
                or info.get("postOnly")
                or (receipt.get("params") or {}).get("postOnly", False)
            ),
            "executionProfile": (
                (receipt.get("params") or {}).get("executionProfile")
                or info.get("executionProfile")
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
            "symbol": normalize_symbol(trade.get("symbol") or info.get("symbol")),
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
        fill_symbol = normalize_symbol(fill.get("symbol"))
        for receipt in receipts:
            receipt_order_id = receipt.get("id")
            receipt_client_order_id = receipt.get("clientOrderId")
            if fill_order_id and receipt_order_id and fill_order_id == receipt_order_id:
                return True
            if fill_client_order_id and receipt_client_order_id and fill_client_order_id == receipt_client_order_id:
                return True
            if (
                fill_symbol
                and normalize_symbol(receipt.get("symbol")) == fill_symbol
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

    def _derive_order_state(
        self,
        order: OrderIntent,
        receipt: Dict[str, Any],
        matched_fills: List[Dict[str, Any]],
    ) -> str:
        status = str(receipt.get("status") or "unknown").lower()
        if status == "recovered":
            recovered_order = receipt.get("recovered_order") or {}
            status = str(
                recovered_order.get("status")
                or (recovered_order.get("info") or {}).get("orderStatus")
                or "unknown"
            ).lower()

        filled_quantity = sum(float(fill.get("amount") or 0.0) for fill in matched_fills)
        if filled_quantity <= 0:
            filled_quantity = float(receipt.get("filled") or 0.0)

        if order.action == "CANCEL":
            if status in ("timeout_unresolved", "unknown"):
                return "unknown"
            if status in ("failed", "rejected"):
                return "failed"
            if status in ("closed", "filled"):
                return "completed"
            if status in ("canceled", "cancelled"):
                return "cancelled"
            return "cancel_requested"
        if status in ("deferred_waiting_cancel", "deferred_unknown_order"):
            return "deferred"
        if status in ("timeout_unresolved", "unknown"):
            return "unknown"
        if status in ("failed", "rejected"):
            return "failed"
        if status in ("canceled", "cancelled"):
            return "cancelled"
        if status in ("closed", "filled"):
            return "completed"
        if filled_quantity > 0:
            if abs(filled_quantity) + 1e-9 < abs(float(order.quantity)):
                return "partial_filled"
            return "completed"
        if status in ("open", "partially_filled", "partiallyfilled"):
            return "partial_filled" if float(receipt.get("filled") or 0.0) > 0 else "submitted"
        return "submitted"

    def _get_fills_for_receipt(
        self,
        order: OrderIntent,
        receipt: Dict[str, Any],
        fills: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        order_id = receipt.get("id")
        client_order_id = receipt.get("clientOrderId")
        matches = []
        for fill in fills:
            if order_id and fill.get("order_id") == order_id:
                matches.append(fill)
                continue
            if client_order_id and fill.get("client_order_id") == client_order_id:
                matches.append(fill)
                continue
            if normalize_symbol(fill.get("symbol")) == normalize_symbol(order.symbol) and not order_id and not client_order_id:
                matches.append(fill)
        return matches

    def _append_matching_fills(
        self,
        fills: List[Dict[str, Any]],
        receipts: List[Dict[str, Any]],
        matched_fills: List[Dict[str, Any]],
        seen_trade_ids: set,
    ):
        for fill in fills:
            if not self._fill_matches_receipts(fill, receipts):
                continue
            trade_id = fill.get("trade_id") or f"{fill.get('order_id')}:{fill.get('timestamp')}"
            if trade_id in seen_trade_ids:
                continue
            seen_trade_ids.add(trade_id)
            matched_fills.append(fill)
