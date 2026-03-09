class TradingCycleRiskEvaluationMixin:
    def _evaluate_cycle_risk(self, cycle_context: dict, intent, execution_guards: dict) -> dict:
        unresolved_timeout_count = self.state_store.count_recent_unknown_orders(
            minutes=execution_guards["unknown_order_block_minutes"]
        )
        is_approved, reason = self.risk_engine.evaluate_intent(
            intent,
            cycle_context["account_state"],
            market_snapshots=cycle_context["market_snapshots"],
            market_prices=cycle_context["market_prices"],
            risk_state=cycle_context["risk_state"],
            is_trigger_mode=cycle_context["is_trigger_mode"],
            unresolved_timeout_count=unresolved_timeout_count,
            active_profile_override=cycle_context["effective_risk_profile"],
        )
        evidence_quality_summary = self.risk_engine.get_evidence_quality_summary(intent)
        entry_size_cap_summary = self.risk_engine.get_effective_max_order_notional_pct_balance(
            intent=intent,
            active_profile=cycle_context["effective_risk_profile"],
            risk_state=cycle_context["risk_state"],
            market_snapshots=cycle_context["market_snapshots"],
        )
        risk_payload = self._serialize_risk_state(cycle_context["risk_state"])
        return {
            "is_approved": is_approved,
            "reason": reason,
            "unresolved_timeout_count": unresolved_timeout_count,
            "evidence_quality_summary": evidence_quality_summary,
            "entry_size_cap_summary": entry_size_cap_summary,
            "risk_payload": risk_payload,
        }

    def _build_execution_summary(self, cycle_context: dict, decision_result, risk_outcome: dict, loop_type: str):
        trigger_symbols = [
            symbol
            for symbol, snapshot in cycle_context["market_snapshots"].items()
            if snapshot.is_opportunity_trigger
        ]
        risk_trigger_symbols = [
            symbol
            for symbol, snapshot in cycle_context["market_snapshots"].items()
            if snapshot.is_risk_trigger
        ]
        execution_summary = {
            "loop_type": loop_type,
            "symbols": list(cycle_context["market_snapshots"].keys()),
            "trigger_mode": cycle_context["is_trigger_mode"],
            "trigger_symbols": trigger_symbols,
            "risk_trigger_symbols": risk_trigger_symbols,
            "risk_mode": cycle_context["risk_mode"],
            "risk_profile": cycle_context["risk_profile"],
            "effective_risk_profile": cycle_context["effective_risk_profile"],
            "cycle_role": cycle_context["effective_risk_profile"].get("cycle_role"),
            "risk_state": risk_outcome["risk_payload"],
            "decision_input_hash": decision_result.input_hash,
            "ai_usage": decision_result.usage_metadata or {},
            "regime_key": cycle_context["regime_key"],
            "policy_bundle": cycle_context["active_bundle_summary"],
            "regime_drift": cycle_context["regime_drift_summary"],
            "alpha_policy": cycle_context["alpha_policy_summary"],
            "evidence_quality": risk_outcome["evidence_quality_summary"],
            "entry_size_cap": risk_outcome["entry_size_cap_summary"],
            "context_selection_summary": self._serialize_json_payload(
                getattr(self, "last_context_selection_summary", {}) or {}
            ),
            "executed_order_count": 0,
            "unknown_order_states_before_execution": risk_outcome["unresolved_timeout_count"],
        }
        return execution_summary, trigger_symbols, risk_trigger_symbols
