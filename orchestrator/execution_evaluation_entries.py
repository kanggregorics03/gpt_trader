class TradingExecutionEvaluationEntryMixin:
    def _build_execution_policy_evaluation_entries(
        self,
        audit_log_id: int,
        intent,
        planned_orders,
        receipts,
        fills,
        market_snapshots,
        symbol_journal_entries,
        loop_mode: str,
        risk_mode: str,
        is_trigger_mode: bool,
    ):
        if not audit_log_id or not planned_orders:
            return []
        journal_by_symbol = {
            str((entry or {}).get("symbol") or ""): (entry or {})
            for entry in (symbol_journal_entries or [])
            if (entry or {}).get("symbol")
        }
        evaluations = []
        for index, order in enumerate(planned_orders):
            receipt = receipts[index] if index < len(receipts) else {}
            matched_fills = self.execution_engine._get_fills_for_receipt(order, receipt, fills)
            requested_quantity = abs(float(order.quantity or 0.0))
            filled_quantity = round(sum(float(fill.get("amount") or 0.0) for fill in matched_fills), 8)
            if filled_quantity <= 0:
                filled_quantity = round(abs(float(receipt.get("filled") or 0.0)), 8)
            average_fill_price = self._calculate_average_fill_price(matched_fills, receipt)
            snapshot = market_snapshots.get(order.symbol) if isinstance(market_snapshots, dict) else None
            reference_price = self._resolve_execution_reference_price(order, snapshot)
            context = dict(getattr(order, "execution_context", {}) or {})
            symbol_entry = journal_by_symbol.get(order.symbol, {})
            order_action = str(getattr(order, "action", "") or "").upper()
            is_cancel = order_action == "CANCEL"
            if is_cancel:
                status = str(receipt.get("status") or "unknown").lower()
                cancel_success = status in ("canceled", "cancelled", "closed")
                fill_ratio = 1.0 if cancel_success else 0.0
                slippage_bps = None
                requested_quantity = 1.0
                filled_quantity = 1.0 if cancel_success else 0.0
                average_fill_price = None
                reference_price = None
                action_type = context.get("action_type") or "cancel"
                order_type = "CANCEL"
                execution_profile = context.get("execution_profile") or "cancel_live_order"
            else:
                fill_ratio = round(min(1.0, filled_quantity / requested_quantity), 6) if requested_quantity > 0 else None
                slippage_bps = self._estimate_order_execution_slippage_bps(
                    order=order,
                    reference_price=reference_price,
                    average_fill_price=average_fill_price,
                )
                action_type = context.get("action_type") or ("reduce" if order.reduce_only else "entry")
                order_type = order.order_type
                execution_profile = order.execution_profile
            evaluations.append(
                {
                    "symbol": order.symbol,
                    "loop_mode": loop_mode,
                    "risk_mode": risk_mode,
                    "action_type": action_type,
                    "urgency_bucket": context.get("urgency_bucket") or "unknown",
                    "liquidity_bucket": context.get("liquidity_bucket") or "unknown",
                    "decision": intent.decision,
                    "order_action": order.action,
                    "order_type": order_type,
                    "execution_profile": execution_profile,
                    "reduce_only": bool(order.reduce_only),
                    "trigger_mode": bool(is_trigger_mode),
                    "spread_bps": context.get("spread_bps"),
                    "spread_percentile_1h": context.get("spread_percentile_1h"),
                    "depth_percentile_1h": context.get("depth_percentile_1h"),
                    "reference_price": reference_price,
                    "order_price": order.price,
                    "average_fill_price": average_fill_price,
                    "requested_quantity": requested_quantity,
                    "filled_quantity": filled_quantity,
                    "fill_ratio": fill_ratio,
                    "slippage_bps": slippage_bps,
                    "latency_seconds": self._estimate_order_latency_seconds(receipt, matched_fills),
                    "status": receipt.get("status") or "unknown",
                    "outcome_trade_case_id": symbol_entry.get("trade_case_id"),
                    "outcome_estimated_pnl_pct": symbol_entry.get("estimated_pnl_pct"),
                    "outcome_realized_pnl": symbol_entry.get("net_realized_pnl"),
                    "payload": {
                        "audit_log_id": audit_log_id,
                        "time_in_force": getattr(order, "time_in_force", None),
                        "post_only": bool(getattr(order, "post_only", False)),
                        "max_slippage_bps": getattr(order, "max_slippage_bps", None),
                        "limit_offset_bps": getattr(order, "limit_offset_bps", None),
                        "execution_context": context,
                        "receipt": receipt,
                    },
                }
            )
        return evaluations
