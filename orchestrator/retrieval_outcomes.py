class TradingRetrievalOutcomeMixin:
    def _save_retrieval_evaluations_for_audit(
        self,
        audit_log_id: int | None,
        intent,
        estimated_pnl_pct: float | None,
        risk_mode: str,
        symbol_journal_entries: list[dict] | None = None,
    ) -> None:
        if not audit_log_id or not hasattr(self.state_store, "save_retrieval_evaluations"):
            return
        raw_matches = list(getattr(self, "last_similar_case_matches", []) or [])
        if not raw_matches:
            return
        entry_lookup = {
            str((entry or {}).get("symbol") or ""): (entry or {})
            for entry in (symbol_journal_entries or [])
            if str((entry or {}).get("symbol") or "").strip()
        }
        decision_symbols = {
            str(getattr(order, "symbol", "") or "").strip()
            for order in (getattr(intent, "orders", []) or [])
            if str(getattr(order, "symbol", "") or "").strip()
        }
        entries = []
        for index, match in enumerate(raw_matches, start=1):
            matched_symbol = str(match.get("matched_symbol") or match.get("case_symbol") or "").strip()
            symbol_entry = entry_lookup.get(matched_symbol) or entry_lookup.get(str(match.get("case_symbol") or "").strip()) or {}
            entries.append(
                {
                    "context_type": str(match.get("context_type") or "similar_past_cases"),
                    "rank_index": int(match.get("rank_index") or index),
                    "trade_case_id": match.get("trade_case_id"),
                    "case_symbol": match.get("case_symbol"),
                    "matched_symbol": matched_symbol,
                    "decision": getattr(intent, "decision", None),
                    "decision_symbol_aligned": bool(matched_symbol and matched_symbol in decision_symbols),
                    "estimated_pnl_pct": estimated_pnl_pct,
                    "similarity": match.get("similarity"),
                    "final_score": match.get("final_score"),
                    "retrieval_source": match.get("retrieval_source"),
                    "matched_band_count": match.get("matched_band_count") or 0,
                    "risk_mode": risk_mode,
                    "outcome_trade_case_id": symbol_entry.get("trade_case_id"),
                    "outcome_status": symbol_entry.get("case_status"),
                    "outcome_realized_pnl": symbol_entry.get("net_realized_pnl"),
                    "outcome_estimated_pnl_pct": symbol_entry.get("estimated_pnl_pct"),
                    "outcome_holding_minutes": symbol_entry.get("holding_minutes"),
                    "outcome_observed_mfe_pct": symbol_entry.get("observed_mfe_pct"),
                    "outcome_observed_mae_pct": symbol_entry.get("observed_mae_pct"),
                    "outcome_slippage_bps": symbol_entry.get("slippage_bps"),
                    "outcome_updated_at": symbol_entry.get("case_closed_at") or symbol_entry.get("case_opened_at"),
                    "payload": match.get("payload") or {},
                }
            )
        self.state_store.save_retrieval_evaluations(audit_log_id, entries)

    def _backfill_retrieval_outcomes_for_symbol_entries(
        self,
        symbol_journal_entries: list[dict] | None,
    ) -> None:
        if not hasattr(self.state_store, "update_retrieval_evaluations_for_trade_case"):
            return
        for entry in symbol_journal_entries or []:
            trade_case_id = str(entry.get("trade_case_id") or "").strip()
            symbol = str(entry.get("symbol") or "").strip()
            if not trade_case_id or not symbol:
                continue
            self.state_store.update_retrieval_evaluations_for_trade_case(
                trade_case_id=trade_case_id,
                case_symbol=symbol,
                entry_audit_log_id=entry.get("audit_log_id"),
                outcome_status=entry.get("case_status"),
                outcome_realized_pnl=entry.get("net_realized_pnl"),
                outcome_estimated_pnl_pct=entry.get("estimated_pnl_pct"),
                outcome_holding_minutes=entry.get("holding_minutes"),
                outcome_observed_mfe_pct=entry.get("observed_mfe_pct"),
                outcome_observed_mae_pct=entry.get("observed_mae_pct"),
                outcome_slippage_bps=entry.get("slippage_bps"),
                outcome_updated_at=entry.get("case_closed_at") or entry.get("case_opened_at"),
            )

    def _backfill_shadow_policy_outcomes_for_symbol_entries(
        self,
        symbol_journal_entries: list[dict] | None,
    ) -> None:
        if not hasattr(self.state_store, "update_shadow_decisions_for_trade_case"):
            return
        for entry in symbol_journal_entries or []:
            trade_case_id = str(entry.get("trade_case_id") or "").strip()
            if not trade_case_id:
                continue
            self.state_store.update_shadow_decisions_for_trade_case(
                trade_case_id=trade_case_id,
                outcome_status=entry.get("case_status"),
                outcome_estimated_pnl_pct=entry.get("estimated_pnl_pct"),
            )
