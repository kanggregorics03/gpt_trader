class TradingCycleResultSummaryMixin:
    def _persist_cycle_results(
        self,
        *,
        cycle_context: dict,
        decision_outcome: dict,
        risk_outcome: dict,
        execution_outcome: dict,
        execution_summary: dict,
        trigger_symbols: list[str],
        risk_trigger_symbols: list[str],
        execution_guards: dict,
    ) -> dict:
        estimated_pnl_pct = execution_summary.get("balance_delta_pct")
        if estimated_pnl_pct is None:
            estimated_pnl_pct = self._estimate_realized_pnl_pct(
                decision_outcome["intent"],
                execution_outcome["effective_pre_execution_state"],
            )
        loss_signal = estimated_pnl_pct is not None and estimated_pnl_pct < 0
        account_payload = execution_outcome["effective_pre_execution_state"].model_dump(mode="json")
        audit_market = {
            "symbols": list(cycle_context["market_snapshots"].keys()),
            "market_snapshots": {
                symbol: snapshot.model_dump(mode="json")
                for symbol, snapshot in cycle_context["market_snapshots"].items()
            },
            "trigger_symbols": trigger_symbols,
            "risk_trigger_symbols": risk_trigger_symbols,
            "rag_context": {
                "risk_state": risk_outcome["risk_payload"],
                "account_state": account_payload,
            },
        }
        audit_id = self.state_store.save_audit(
            account=account_payload,
            market=audit_market,
            intent=decision_outcome["intent"].model_dump(mode="json"),
            risk_ok=execution_outcome["is_approved"],
            reason=execution_outcome["reason"],
            receipts=execution_outcome["receipts"],
            policy_snapshot=cycle_context["policy_summary"],
            decision_input_hash=decision_outcome["decision_result"].input_hash,
            risk_mode=cycle_context["risk_mode"],
            risk_profile=cycle_context["risk_profile"],
            decision_request=decision_outcome["request"].model_dump(mode="json"),
            ai_raw_response=decision_outcome["decision_result"].raw_response,
            execution_summary=self._serialize_json_payload(execution_summary),
            market_vectors=self.rag_engine.build_market_vectors(
                audit_market["market_snapshots"],
                account_state=account_payload,
                risk_state=risk_outcome["risk_payload"],
            ),
        )
        symbol_journal_entries = self._persist_audit_artifacts(
            audit_id=audit_id,
            cycle_context=cycle_context,
            decision_outcome=decision_outcome,
            execution_outcome=execution_outcome,
            execution_summary=execution_summary,
            trigger_symbols=trigger_symbols,
            risk_trigger_symbols=risk_trigger_symbols,
            estimated_pnl_pct=estimated_pnl_pct,
        )
        trade_case_ids, case_statuses, case_summary = self._summarize_trade_cases(symbol_journal_entries)
        self.state_store.save_journal(
            decision_outcome["intent"].decision,
            decision_outcome["intent"].rationale,
            decision_outcome["intent"].confidence,
            audit_log_id=audit_id,
            risk_reason=execution_outcome["reason"],
            estimated_pnl_pct=estimated_pnl_pct,
            loss_signal=loss_signal,
            rationale_items=decision_outcome["intent"].rationale_items,
            trade_case_ids=trade_case_ids,
            case_statuses=case_statuses,
            case_summary=case_summary,
        )
        self._alert_cycle_decision_summary(
            cycle_context=cycle_context,
            decision_outcome=decision_outcome,
            execution_outcome=execution_outcome,
            execution_summary=execution_summary,
        )
        final_account_state = (
            execution_outcome["post_account_state"]
            or execution_outcome["effective_pre_execution_state"]
            or cycle_context["account_state"]
        )
        final_unknown_order_count = self.state_store.count_recent_unknown_orders(
            minutes=execution_guards["unknown_order_block_minutes"]
        )
        final_safety_state = self.safety_policy_engine.get_emergency_state(
            account_state=final_account_state,
            market_snapshots=cycle_context["market_snapshots"],
            regime_key=cycle_context["regime_key"],
        )
        return {
            "loop_type": execution_summary["loop_type"],
            "guarded_attention_active": cycle_context["is_guarded_attention"],
            "safety_trigger_active": bool(final_safety_state.get("active")) or final_unknown_order_count > 0,
            "has_positions": self._has_live_positions(final_account_state),
            "has_open_orders": self._has_live_open_orders(final_account_state),
            "has_unknown_orders": final_unknown_order_count > 0,
            "executed_order_count": execution_summary.get("executed_order_count", 0),
            "ai_fallback_retriable": bool(
                ((execution_summary.get("ai_fallback_retry") or {}).get("retriable"))
            ),
            "position_closed": self._position_closed_during_cycle(
                execution_outcome["effective_pre_execution_state"],
                execution_outcome["post_account_state"],
            ),
            "final_account_state": final_account_state,
        }
