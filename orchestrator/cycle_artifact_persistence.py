from datetime import UTC, datetime


class TradingCycleArtifactPersistenceMixin:
    def _persist_audit_artifacts(
        self,
        *,
        audit_id,
        cycle_context: dict,
        decision_outcome: dict,
        execution_outcome: dict,
        execution_summary: dict,
        trigger_symbols: list[str],
        risk_trigger_symbols: list[str],
        estimated_pnl_pct,
    ) -> list[dict]:
        if audit_id is None:
            return []

        ledger_entries = []
        position_state_transitions = []
        if execution_outcome["fills"]:
            self.state_store.save_execution_fills(audit_id, execution_outcome["fills"])
            ledger_entries = self._build_position_ledger_entries(
                decision_outcome["intent"],
                execution_outcome["fills"],
                execution_summary,
            )
            self.state_store.save_position_ledger_entries(audit_id, ledger_entries)
        if execution_outcome["planned_orders"]:
            order_state_transitions = self.execution_engine.build_order_state_transitions(
                execution_outcome["planned_orders"],
                execution_outcome["receipts"],
                execution_outcome["fills"],
            )
            position_state_transitions = self.execution_engine.build_position_state_transitions(
                decision_outcome["intent"],
                execution_outcome["effective_pre_execution_state"],
                execution_outcome["post_account_state"],
                execution_outcome["planned_orders"],
                execution_outcome["receipts"],
            )
            self.state_store.save_order_state_transitions(audit_id, order_state_transitions)
            self.state_store.save_position_state_transitions(audit_id, position_state_transitions)

        symbol_journal_entries = self._build_symbol_journal_entries(
            audit_log_id=audit_id,
            intent=decision_outcome["intent"],
            market_snapshots=cycle_context["market_snapshots"],
            trigger_symbols=trigger_symbols,
            risk_trigger_symbols=risk_trigger_symbols,
            risk_reason=execution_outcome["reason"],
            risk_mode=cycle_context["risk_mode"],
            risk_approved=execution_outcome["is_approved"],
            estimated_pnl_pct=estimated_pnl_pct,
            ledger_entries=ledger_entries,
        )
        observed_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        symbol_journal_entries = self._annotate_trade_case_entries(
            audit_log_id=audit_id,
            symbol_journal_entries=symbol_journal_entries,
            position_state_transitions=position_state_transitions,
            intent=decision_outcome["intent"],
            before_state=execution_outcome["effective_pre_execution_state"],
            after_state=execution_outcome["post_account_state"],
            market_snapshots=cycle_context["market_snapshots"],
            fills=execution_outcome["fills"],
            risk_mode=cycle_context["risk_mode"],
            risk_state=cycle_context["risk_state"],
            observed_at=observed_at,
        )
        self._save_retrieval_evaluations_for_audit(
            audit_log_id=audit_id,
            intent=decision_outcome["intent"],
            estimated_pnl_pct=estimated_pnl_pct,
            risk_mode=cycle_context["risk_mode"],
            symbol_journal_entries=symbol_journal_entries,
        )
        self._backfill_retrieval_outcomes_for_symbol_entries(symbol_journal_entries)
        self._backfill_shadow_policy_outcomes_for_symbol_entries(symbol_journal_entries)
        execution_policy_evaluations = self._build_execution_policy_evaluation_entries(
            audit_log_id=audit_id,
            intent=decision_outcome["intent"],
            planned_orders=execution_outcome["planned_orders"],
            receipts=execution_outcome["receipts"],
            fills=execution_outcome["fills"],
            market_snapshots=cycle_context["market_snapshots"],
            symbol_journal_entries=symbol_journal_entries,
            loop_mode=cycle_context["loop_mode"],
            risk_mode=cycle_context["risk_mode"],
            is_trigger_mode=cycle_context["is_trigger_mode"],
        )
        if execution_policy_evaluations and hasattr(self.state_store, "save_execution_policy_evaluations"):
            self.state_store.save_execution_policy_evaluations(
                audit_id,
                execution_policy_evaluations,
            )
        self._record_shadow_policy_decisions(
            audit_log_id=audit_id,
            loop_mode=cycle_context["loop_mode"],
            live_intent=decision_outcome["intent"],
            account_state=(
                execution_outcome["post_account_state"]
                or execution_outcome["effective_pre_execution_state"]
            ),
            market_snapshots=cycle_context["market_snapshots"],
            risk_mode=cycle_context["risk_mode"],
            symbol_journal_entries=symbol_journal_entries,
            regime_key=cycle_context["regime_key"],
        )
        self.state_store.save_symbol_journal_entries(symbol_journal_entries)
        trade_case_snapshot_entries = self._build_trade_case_snapshot_entries(
            audit_log_id=audit_id,
            observed_at=observed_at,
            account_state=(
                execution_outcome["post_account_state"]
                or execution_outcome["effective_pre_execution_state"]
            ),
            market_snapshots=cycle_context["market_snapshots"],
            symbol_journal_entries=symbol_journal_entries,
        )
        self.state_store.save_trade_case_snapshots(trade_case_snapshot_entries)
        return symbol_journal_entries
