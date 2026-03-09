class TradingJournalContextSupportMixin:
    def _extract_symbols_from_rationale_items(self, rationale_items):
        symbols = set()
        for item in rationale_items or []:
            evidence_rows = (item.evidence or []) if hasattr(item, "evidence") else (item.get("evidence") or [])
            for evidence in evidence_rows:
                symbol = evidence.symbol if hasattr(evidence, "symbol") else evidence.get("symbol")
                if symbol:
                    symbols.add(str(symbol))
        return symbols

    def _compact_recent_journal_row(self, row, max_rationale_items: int, max_evidence_per_item: int):
        rationale = row.get("rationale") or []
        compact_rationale = rationale[:2] if isinstance(rationale, list) else [str(rationale)]
        compact_items = []
        for item in (row.get("rationale_items") or [])[:max_rationale_items]:
            evidence_rows = (item.evidence or []) if hasattr(item, "evidence") else (item.get("evidence") or [])
            compact_evidence = []
            for evidence in evidence_rows[:max_evidence_per_item]:
                compact_evidence.append(
                    {
                        "signal": evidence.signal if hasattr(evidence, "signal") else evidence.get("signal"),
                        "symbol": evidence.symbol if hasattr(evidence, "symbol") else evidence.get("symbol"),
                        "timeframe": evidence.timeframe if hasattr(evidence, "timeframe") else evidence.get("timeframe"),
                        "operator": evidence.operator if hasattr(evidence, "operator") else evidence.get("operator"),
                        "observed_value": (
                            evidence.observed_value if hasattr(evidence, "observed_value") else evidence.get("observed_value")
                        ),
                        "threshold": evidence.threshold if hasattr(evidence, "threshold") else evidence.get("threshold"),
                        "units": evidence.units if hasattr(evidence, "units") else evidence.get("units"),
                        "source": evidence.source if hasattr(evidence, "source") else evidence.get("source"),
                    }
                )
            compact_items.append(
                {
                    "summary": item.summary if hasattr(item, "summary") else item.get("summary"),
                    "why_it_matters": item.why_it_matters if hasattr(item, "why_it_matters") else item.get("why_it_matters"),
                    "trade_implication": item.trade_implication if hasattr(item, "trade_implication") else item.get("trade_implication"),
                    "failure_mode": item.failure_mode if hasattr(item, "failure_mode") else item.get("failure_mode"),
                    "evidence": compact_evidence,
                }
            )
        return {
            "journal_id": row.get("journal_id") or row.get("id"),
            "audit_log_id": row.get("audit_log_id"),
            "timestamp": row.get("timestamp"),
            "decision": row.get("decision") or "HOLD",
            "rationale": compact_rationale,
            "rationale_items": compact_items,
            "confidence": row.get("confidence") or 0.0,
            "risk_reason": row.get("risk_reason") or "",
            "estimated_pnl_pct": row.get("estimated_pnl_pct"),
            "loss_signal": bool(row.get("loss_signal")),
            "ledger_net_realized_pnl": row.get("ledger_net_realized_pnl") or 0.0,
            "ledger_fee_cost": row.get("ledger_fee_cost") or 0.0,
            "risk_approved": row.get("risk_approved"),
            "risk_mode": row.get("risk_mode"),
            "trigger_symbols": row.get("trigger_symbols") or [],
            "risk_trigger_symbols": row.get("risk_trigger_symbols") or [],
            "evidence_quality": row.get("evidence_quality") or {},
            "entry_size_cap": row.get("entry_size_cap") or {},
            "trade_case_ids": row.get("trade_case_ids") or [],
            "case_statuses": row.get("case_statuses") or {},
            "case_summary": row.get("case_summary") or "",
            "similarity_score": row.get("similarity_score"),
            "matched_features": row.get("matched_features") or [],
            "summary": row.get("summary"),
        }

    def _is_system_fallback_context_row(self, row) -> bool:
        execution_summary = row.get("execution_summary") or {}
        if str(execution_summary.get("error") or "").strip():
            return True
        ai_usage = execution_summary.get("ai_usage") or {}
        if bool(ai_usage.get("validation_error")) or bool(ai_usage.get("semantic_validation_error")):
            return True

        text_markers = (
            "system error",
            "structured rationale validation failed",
            "fallback due to json validation error from ai",
            "fallback due to invalid structured rationale from ai",
            "loop error during",
        )
        text_values: list[str] = []
        for value in (row.get("rationale") or []):
            text_values.append(str(value or "").strip().lower())
        for value in (row.get("risk_notes") or []):
            text_values.append(str(value or "").strip().lower())
        summary = row.get("summary")
        if summary:
            text_values.append(str(summary).strip().lower())
        risk_reason = row.get("risk_reason")
        if risk_reason:
            text_values.append(str(risk_reason).strip().lower())
        return any(any(marker in text for marker in text_markers) for text in text_values if text)

    def _classify_recent_journal_bucket(self, row) -> str:
        if self._is_system_fallback_context_row(row):
            return "system_fallback"

        decision = str(row.get("decision") or "HOLD").upper()
        has_trigger = bool(row.get("trigger_symbols") or [])
        has_risk_trigger = bool(row.get("risk_trigger_symbols") or [])
        has_loss_signal = bool(row.get("loss_signal"))
        risk_reason = str(row.get("risk_reason") or "").strip().lower()
        is_non_default_risk_reason = risk_reason not in ("", "approved", "safe action.")

        if decision == "HOLD":
            if has_trigger and not has_risk_trigger:
                return "missed_opportunity_hold"
            if not has_trigger and not has_risk_trigger and not has_loss_signal and not is_non_default_risk_reason:
                return "good_hold"
            return "hold"
        if decision in ("BUY", "SELL"):
            return "entry_action"
        if decision in ("REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY") or has_loss_signal or has_risk_trigger:
            return "risk_outcome"
        return "other_action"

    def _journal_decision_diversity_key(self, row) -> str:
        decision = str(row.get("decision") or "HOLD").upper()
        if decision == "HOLD":
            bucket = str(row.get("context_bucket") or "")
            if bucket in {"good_hold", "missed_opportunity_hold"}:
                return bucket
            return "hold"
        if decision in ("BUY", "SELL"):
            return "entry_action"
        if decision in ("REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"):
            return "risk_outcome"
        return "other_action"

    def _classify_memory_bucket(self, row, metrics: dict | None = None) -> str:
        metrics = metrics or {}
        category_text = str(row.get("category") or metrics.get("category") or "lesson").strip().lower()
        hold_quality = str(row.get("hold_quality") or metrics.get("hold_quality") or "").strip().lower()
        lifecycle_status = str(row.get("lifecycle_status") or metrics.get("lifecycle_status") or "").strip().lower()
        retired = bool(row.get("retired") or metrics.get("retired") or lifecycle_status == "retired")
        support = int(row.get("support") or metrics.get("support") or 0)

        if retired:
            return "retired_counterpoint"
        if hold_quality == "good_hold":
            return "good_hold"
        if hold_quality in {"missed_opportunity_hold", "probe_only_opportunity_hold"}:
            return "missed_opportunity_hold"
        if category_text == "risk_control":
            return "risk_control"
        if category_text in {"opportunity", "setup", "entry"}:
            return "opportunity"
        if category_text in {"execution_quality", "execution", "microstructure"}:
            return "execution_quality"
        if support <= 2:
            return "emerging"
        return "general"
