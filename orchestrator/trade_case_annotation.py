from datetime import datetime


class TradingTradeCaseAnnotationMixin:
    def _estimate_symbol_slippage(
        self,
        symbol: str,
        symbol_orders: list,
        fills: list[dict],
        market_snapshots,
    ) -> tuple[float | None, float | None, float | None]:
        symbol_fills = [fill for fill in (fills or []) if str(fill.get("symbol") or "") == symbol]
        if not symbol_fills:
            return None, None, None

        total_amount = sum(abs(float(fill.get("amount") or 0.0)) for fill in symbol_fills)
        if total_amount <= 0:
            return None, None, None
        avg_fill_price = (
            sum(float(fill.get("price") or 0.0) * abs(float(fill.get("amount") or 0.0)) for fill in symbol_fills)
            / total_amount
        )

        planned_prices = [
            float(getattr(order, "price", 0.0) or 0.0)
            for order in (symbol_orders or [])
            if float(getattr(order, "price", 0.0) or 0.0) > 0
        ]
        snapshot = market_snapshots.get(symbol) if isinstance(market_snapshots, dict) else None
        reference_price = None
        if planned_prices:
            reference_price = sum(planned_prices) / len(planned_prices)
        elif snapshot is not None:
            for candidate in (
                getattr(snapshot.order_book, "mid_price", None),
                getattr(snapshot.order_book, "microprice", None),
                getattr(snapshot, "current_price", None),
                getattr(snapshot, "mark_price", None),
            ):
                try:
                    candidate_value = float(candidate)
                except (TypeError, ValueError):
                    candidate_value = 0.0
                if candidate_value > 0:
                    reference_price = candidate_value
                    break
        if not reference_price or reference_price <= 0:
            return avg_fill_price, None, None

        action = str(getattr(symbol_orders[0], "action", "") or "").upper() if symbol_orders else ""
        if action == "BUY":
            adverse_delta = max(0.0, avg_fill_price - reference_price)
        elif action == "SELL":
            adverse_delta = max(0.0, reference_price - avg_fill_price)
        else:
            adverse_delta = abs(avg_fill_price - reference_price)
        slippage_cost = round(adverse_delta * total_amount, 8)
        slippage_bps = round((adverse_delta / reference_price) * 10000.0, 4) if reference_price > 0 else None
        return avg_fill_price, slippage_cost, slippage_bps

    def _get_symbol_exit_price(self, symbol: str, avg_fill_price: float | None, market_snapshots) -> float | None:
        if avg_fill_price and avg_fill_price > 0:
            return avg_fill_price
        snapshot = market_snapshots.get(symbol) if isinstance(market_snapshots, dict) else None
        if snapshot is None:
            return None
        for candidate in (
            getattr(snapshot, "current_price", None),
            getattr(snapshot, "mark_price", None),
            getattr(snapshot.order_book, "mid_price", None),
        ):
            try:
                candidate_value = float(candidate)
            except (TypeError, ValueError):
                candidate_value = 0.0
            if candidate_value > 0:
                return candidate_value
        return None

    def _extract_symbol_failure_mode(self, symbol: str, rationale_items) -> str:
        for item in rationale_items or []:
            evidence_rows = (item.evidence or []) if hasattr(item, "evidence") else (item.get("evidence") or [])
            evidence_symbols = {
                (e.symbol if hasattr(e, "symbol") else e.get("symbol"))
                for e in evidence_rows
            }
            if evidence_symbols and symbol not in evidence_symbols and None not in evidence_symbols:
                continue
            failure_mode = (
                item.failure_mode if hasattr(item, "failure_mode") else item.get("failure_mode")
            )
            if failure_mode:
                return str(failure_mode)
        return ""

    def _derive_thesis_failure_reason(
        self,
        symbol: str,
        entry: dict,
        symbol_snapshot,
        rationale_items,
        risk_reason: str,
        observed_pnl_pct: float | None,
        realized_pnl: float | None,
    ) -> str | None:
        risk_reason_text = str(risk_reason or "").lower()
        if "kill switch" in risk_reason_text:
            return "risk_kill_switch"
        if "cooldown" in risk_reason_text or "drawdown" in risk_reason_text:
            return "risk_profile_pressure"
        if symbol_snapshot is not None and getattr(symbol_snapshot, "risk_trigger_reasons", None):
            return "execution_quality_degraded:" + ",".join(symbol_snapshot.risk_trigger_reasons[:3])
        failure_mode = self._extract_symbol_failure_mode(symbol, rationale_items)
        negative_outcome = (
            (realized_pnl is not None and float(realized_pnl) < 0)
            or (observed_pnl_pct is not None and float(observed_pnl_pct) < 0)
            or bool(entry.get("loss_signal"))
        )
        if negative_outcome and failure_mode:
            return failure_mode
        if negative_outcome:
            return "adverse_price_follow_through"
        return None

    def _annotate_trade_case_entries(
        self,
        audit_log_id: int,
        symbol_journal_entries: list[dict],
        position_state_transitions: list[dict],
        intent,
        before_state,
        after_state,
        market_snapshots,
        fills: list[dict],
        risk_mode: str,
        loop_mode: str = "",
        is_guarded_attention: bool = False,
        is_safety_mode: bool = False,
        risk_state=None,
        observed_at: str | None = None,
    ) -> list[dict]:
        observed_at = observed_at or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        transitions_by_symbol = {
            str((entry or {}).get("symbol")): (entry or {})
            for entry in (position_state_transitions or [])
            if (entry or {}).get("symbol")
        }
        symbol_orders = {}
        for order in (intent.orders or []):
            symbol_orders.setdefault(order.symbol, []).append(order)

        annotated_entries = []
        for entry in symbol_journal_entries or []:
            symbol = str(entry.get("symbol") or "")
            if not symbol:
                continue
            transition = transitions_by_symbol.get(symbol, {})
            from_state = str(transition.get("from_state") or "")
            to_state = str(transition.get("to_state") or "")
            active_case = self.state_store.get_active_trade_case(symbol)
            trade_case_id = active_case.get("trade_case_id")
            case_opened_at = active_case.get("opened_at")
            case_closed_at = None
            holding_minutes = None
            case_status = None
            symbol_snapshot = market_snapshots.get(symbol) if isinstance(market_snapshots, dict) else None
            current_case = self.state_store.get_trade_case(trade_case_id) if trade_case_id else {}
            observed_pnl_pct = self._get_symbol_observed_pnl_pct(symbol, before_state, after_state)
            avg_fill_price, slippage_cost, slippage_bps = self._estimate_symbol_slippage(
                symbol=symbol,
                symbol_orders=symbol_orders.get(symbol, []),
                fills=fills,
                market_snapshots=market_snapshots,
            )
            realized_pnl = entry.get("net_realized_pnl")
            fee_cost = entry.get("fee_cost")
            thesis_failure_reason = self._derive_thesis_failure_reason(
                symbol=symbol,
                entry=entry,
                symbol_snapshot=symbol_snapshot,
                rationale_items=intent.rationale_items,
                risk_reason=entry.get("risk_reason") or "",
                observed_pnl_pct=observed_pnl_pct,
                realized_pnl=realized_pnl,
            )
            thesis_id = (
                str(getattr(intent, "thesis_id", None) or "").strip()
                or active_case.get("thesis_id")
                or current_case.get("thesis_id")
            )
            management_template_id = (
                str(getattr(intent, "management_template_id", None) or "").strip()
                or active_case.get("management_template_id")
                or current_case.get("management_template_id")
            )
            exit_reason = str(getattr(intent, "exit_reason", None) or "").strip() or None

            if from_state == "flat" and to_state in ("opening", "open"):
                trade_case_id = self._make_trade_case_id(symbol, audit_log_id, observed_at)
                case_opened_at = observed_at
                case_status = "opened"
                self.state_store.upsert_active_trade_case(
                    symbol=symbol,
                    trade_case_id=trade_case_id,
                    opened_at=case_opened_at,
                    entry_decision=entry.get("decision"),
                    thesis_id=thesis_id,
                    management_template_id=management_template_id,
                    last_decision=entry.get("decision"),
                    last_audit_log_id=audit_log_id,
                    payload={
                        "opened_via": transition.get("event_type"),
                        "from_state": from_state,
                        "to_state": to_state,
                        "thesis_summary": getattr(intent, "thesis_summary", None),
                        "invalidation_reason": getattr(intent, "invalidation_reason", None),
                    },
                )
            elif from_state == "open" and to_state == "closed":
                closed_case = self.state_store.close_active_trade_case(
                    symbol=symbol,
                    closed_at=observed_at,
                    last_decision=entry.get("decision"),
                    last_audit_log_id=audit_log_id,
                )
                trade_case_id = closed_case.get("trade_case_id") or trade_case_id
                case_opened_at = closed_case.get("opened_at") or case_opened_at
                case_closed_at = observed_at
                holding_minutes = self._calculate_holding_minutes(case_opened_at, case_closed_at)
                case_status = "closed"
            elif to_state == "reducing":
                if not trade_case_id:
                    trade_case_id = self._make_trade_case_id(symbol, audit_log_id, observed_at)
                    case_opened_at = observed_at
                self.state_store.upsert_active_trade_case(
                    symbol=symbol,
                    trade_case_id=trade_case_id,
                    opened_at=case_opened_at,
                    entry_decision=(active_case.get("entry_decision") or entry.get("decision")),
                    thesis_id=thesis_id,
                    management_template_id=management_template_id,
                    last_decision=entry.get("decision"),
                    last_audit_log_id=audit_log_id,
                    payload={
                        **(active_case.get("payload") or {}),
                        "from_state": from_state,
                        "to_state": to_state,
                        "synthetic_opened_at": case_opened_at is None,
                    },
                )
                case_status = "reduced"
            elif to_state == "open" and from_state == "open":
                if trade_case_id:
                    self.state_store.upsert_active_trade_case(
                        symbol=symbol,
                        trade_case_id=trade_case_id,
                        opened_at=case_opened_at,
                        entry_decision=active_case.get("entry_decision"),
                        thesis_id=thesis_id,
                        management_template_id=management_template_id,
                        last_decision=entry.get("decision"),
                        last_audit_log_id=audit_log_id,
                        payload=active_case.get("payload") or {},
                    )
                case_status = "active" if trade_case_id else None
            elif to_state == "unknown":
                case_status = "unknown"
            elif trade_case_id:
                case_status = "active"

            if trade_case_id and case_opened_at and holding_minutes is None and case_closed_at is None:
                holding_minutes = self._calculate_holding_minutes(case_opened_at, observed_at)

            entry_audit_log_id = current_case.get("entry_audit_log_id") or (
                audit_log_id if case_status == "opened" else active_case.get("last_audit_log_id")
            )
            exit_audit_log_id = (
                audit_log_id if case_status == "closed" else current_case.get("exit_audit_log_id")
            )
            observed_mfe_pct, observed_mae_pct = self._recompute_trade_case_excursions(
                trade_case_id,
                symbol=symbol,
                trade_case=current_case,
                entry_audit_log_id=entry_audit_log_id,
                exit_audit_log_id=exit_audit_log_id,
                fallback_opened_at=case_opened_at,
                fallback_closed_at=case_closed_at,
                current_observed_pnl_pct=observed_pnl_pct,
            )

            entry["trade_case_id"] = trade_case_id
            entry["case_status"] = case_status
            entry["case_opened_at"] = case_opened_at
            entry["case_closed_at"] = case_closed_at
            entry["holding_minutes"] = holding_minutes
            entry["observed_pnl_pct"] = observed_pnl_pct
            entry["observed_mfe_pct"] = observed_mfe_pct
            entry["observed_mae_pct"] = observed_mae_pct
            entry["slippage_cost"] = slippage_cost
            entry["slippage_bps"] = slippage_bps
            entry["entry_price"] = current_case.get("entry_price") or self._get_symbol_entry_price(symbol, before_state, after_state)
            entry["exit_price"] = self._get_symbol_exit_price(symbol, avg_fill_price, market_snapshots)
            entry["entry_side"] = current_case.get("entry_side") or self._get_symbol_entry_side(
                symbol,
                before_state,
                after_state,
                symbol_orders.get(symbol, []),
            )
            entry["thesis_failure_reason"] = thesis_failure_reason
            feature_vector = self._build_trade_case_feature_vector(
                symbol=symbol,
                market_snapshots=market_snapshots,
                account_state=before_state,
                risk_state=risk_state,
            )
            self.state_store.upsert_trade_case(
                {
                    "trade_case_id": trade_case_id,
                    "symbol": symbol,
                    "status": "closed" if case_status == "closed" else ("open" if case_status == "opened" else (case_status or "active")),
                    "loop_mode": loop_mode,
                    "risk_mode": risk_mode,
                    "guarded_attention": bool(is_guarded_attention),
                    "safety_mode": bool(is_safety_mode),
                    "opened_at": case_opened_at,
                    "closed_at": case_closed_at,
                    "entry_audit_log_id": entry_audit_log_id,
                    "exit_audit_log_id": exit_audit_log_id,
                    "entry_decision": current_case.get("entry_decision") or entry.get("decision"),
                    "exit_decision": entry.get("decision") if case_status == "closed" else current_case.get("exit_decision"),
                    "entry_side": entry.get("entry_side"),
                    "entry_price": entry.get("entry_price"),
                    "exit_price": entry.get("exit_price"),
                    "estimated_pnl_pct": observed_pnl_pct,
                    "realized_pnl": realized_pnl,
                    "fee_cost": fee_cost,
                    "holding_minutes": holding_minutes,
                    "observed_mfe_pct": observed_mfe_pct,
                    "observed_mae_pct": observed_mae_pct,
                    "slippage_cost": slippage_cost,
                    "slippage_bps": slippage_bps,
                    "thesis_failure_reason": thesis_failure_reason,
                    "thesis_id": thesis_id,
                    "management_template_id": management_template_id,
                    "exit_reason": (
                        exit_reason
                        if case_status == "closed"
                        else current_case.get("exit_reason")
                    ),
                    "evidence_signals": entry.get("evidence_signals") or [],
                    "trigger_reasons": (entry.get("payload") or {}).get("trigger_reasons") or [],
                    "risk_trigger_reasons": (entry.get("payload") or {}).get("risk_trigger_reasons") or [],
                    "feature_vector": feature_vector or current_case.get("feature_vector") or [],
                    "payload": {
                        "case_status": case_status,
                        "from_state": from_state,
                        "to_state": to_state,
                        "risk_reason": entry.get("risk_reason"),
                        "thesis_summary": getattr(intent, "thesis_summary", None),
                        "invalidation_reason": getattr(intent, "invalidation_reason", None),
                        "management_template_id": management_template_id,
                        "exit_reason": exit_reason,
                        "regime_tags": [
                            f"trend:{getattr(symbol_snapshot.regime, 'trend_alignment', '')}"
                            if symbol_snapshot is not None else "",
                            f"volatility:{getattr(symbol_snapshot.regime, 'volatility_regime', '')}"
                            if symbol_snapshot is not None else "",
                        ],
                        "entry_evidence_signals": entry.get("evidence_signals") or [],
                        "feature_snapshot": {
                            "spread_percentile_1h": getattr(symbol_snapshot, "spread_percentile_1h", None)
                            if symbol_snapshot is not None else None,
                            "oi_change_15m": getattr(symbol_snapshot, "oi_change_15m", None)
                            if symbol_snapshot is not None else None,
                            "oi_change_1h": getattr(symbol_snapshot, "oi_change_1h", None)
                            if symbol_snapshot is not None else None,
                            "volume_ratio_15m": getattr(symbol_snapshot, "volume_ratio_15m", None)
                            if symbol_snapshot is not None else None,
                            "distance_to_invalid_level": getattr(symbol_snapshot, "distance_to_invalid_level", None)
                            if symbol_snapshot is not None else None,
                            "funding_rate_change_1h": getattr(symbol_snapshot, "funding_rate_change_1h", None)
                            if symbol_snapshot is not None else None,
                            "basis_change_1h": getattr(symbol_snapshot, "basis_change_1h", None)
                            if symbol_snapshot is not None else None,
                        },
                    },
                }
            )
            annotated_entries.append(entry)
        return annotated_entries
