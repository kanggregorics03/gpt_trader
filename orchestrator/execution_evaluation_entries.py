class TradingExecutionEvaluationEntryMixin:
    def _resolve_execution_evaluation_entry_style(self, order, action_type: str) -> str | None:
        order_action = str(getattr(order, "action", "") or "").upper()
        if bool(getattr(order, "reduce_only", False)):
            return None
        if order_action not in {"BUY", "SELL"}:
            return None
        style = str(getattr(order, "entry_style", "") or "").strip().lower()
        if not style and str(action_type or "").lower() in {"entry", "replace_entry"}:
            style = "immediate" if str(getattr(order, "order_type", "") or "").upper() == "MARKET" else ""
        return style or None

    def _classify_execution_policy_outcome_label(
        self,
        *,
        action_type: str,
        order,
        receipt,
        filled_quantity: float,
        symbol_entry,
        outcome_estimated_pnl_pct,
        outcome_realized_pnl,
    ) -> str | None:
        action_type = str(action_type or "").strip().lower()
        order_action = str(getattr(order, "action", "") or "").upper()
        status = str((receipt or {}).get("status") or "").strip().lower()
        symbol_entry = symbol_entry or {}
        summary = str(symbol_entry.get("summary") or "").strip().lower()
        entry_payload = symbol_entry.get("payload") or {}
        exit_reason = str(entry_payload.get("exit_reason") or "").strip().lower()

        if order_action == "CANCEL":
            if "stale" in summary:
                return "stale_entry_cancelled"
            return None

        if action_type in {"exit", "reduce"} or bool(getattr(order, "reduce_only", False)):
            return "risk_exit"

        is_entry = order_action in {"BUY", "SELL"} and not bool(getattr(order, "reduce_only", False))
        if not is_entry:
            return None

        terminal_without_fill = filled_quantity <= 0 and status in {
            "canceled",
            "cancelled",
            "closed",
            "rejected",
            "failed",
            "expired",
        }
        if terminal_without_fill:
            if "stale" in summary:
                return "stale_entry_cancelled"
            return "missed_due_to_no_fill"

        if exit_reason or outcome_realized_pnl is not None or outcome_estimated_pnl_pct is not None:
            pnl_proxy = outcome_estimated_pnl_pct
            if pnl_proxy is None and outcome_realized_pnl is not None:
                pnl_proxy = float(outcome_realized_pnl)
            if pnl_proxy is not None and float(pnl_proxy) < 0:
                return "edge_failed"
        return None

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
        is_guarded_attention: bool,
        is_safety_mode: bool,
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
            entry_style = self._resolve_execution_evaluation_entry_style(order, action_type)
            outcome_estimated_pnl_pct = symbol_entry.get("estimated_pnl_pct")
            outcome_realized_pnl = symbol_entry.get("net_realized_pnl")
            outcome_label = self._classify_execution_policy_outcome_label(
                action_type=action_type,
                order=order,
                receipt=receipt,
                filled_quantity=filled_quantity,
                symbol_entry=symbol_entry,
                outcome_estimated_pnl_pct=outcome_estimated_pnl_pct,
                outcome_realized_pnl=outcome_realized_pnl,
            )
            evaluations.append(
                {
                    "symbol": order.symbol,
                    "loop_mode": loop_mode,
                    "risk_mode": risk_mode,
                    "action_type": action_type,
                    "entry_style": entry_style,
                    "urgency_bucket": context.get("urgency_bucket") or "unknown",
                    "liquidity_bucket": context.get("liquidity_bucket") or "unknown",
                    "decision": intent.decision,
                    "order_action": order.action,
                    "order_type": order_type,
                    "execution_profile": execution_profile,
                    "reduce_only": bool(order.reduce_only),
                    "guarded_attention": bool(is_guarded_attention),
                    "safety_mode": bool(is_safety_mode),
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
                    "outcome_estimated_pnl_pct": outcome_estimated_pnl_pct,
                    "outcome_realized_pnl": outcome_realized_pnl,
                    "outcome_label": outcome_label,
                    "payload": {
                        "audit_log_id": audit_log_id,
                        "time_in_force": getattr(order, "time_in_force", None),
                        "post_only": bool(getattr(order, "post_only", False)),
                        "max_slippage_bps": getattr(order, "max_slippage_bps", None),
                        "limit_offset_bps": getattr(order, "limit_offset_bps", None),
                        "execution_context": context,
                        "entry_style": entry_style,
                        "outcome_label": outcome_label,
                        "case_status": symbol_entry.get("case_status"),
                        "exit_reason": (symbol_entry.get("payload") or {}).get("exit_reason"),
                        "receipt": receipt,
                    },
                }
            )
        return evaluations
