class TradingCycleExecutionOrchestratorMixin:
    def _execute_cycle_intent(
        self,
        *,
        cycle_context: dict,
        intent,
        risk_outcome: dict,
        execution_summary: dict,
        execution_guards: dict,
        loop_lock_owner: str,
    ) -> dict:
        is_approved = bool(risk_outcome["is_approved"])
        reason = risk_outcome["reason"]
        planned_orders = []
        receipts = []
        fills = []
        effective_pre_execution_state = cycle_context["account_state"]
        post_account_state = None

        if is_approved and intent.decision in ("BUY", "SELL", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"):
            execution_result = self._run_order_execution_flow(
                cycle_context=cycle_context,
                intent=intent,
                execution_summary=execution_summary,
                execution_guards=execution_guards,
                loop_lock_owner=loop_lock_owner,
            )
            is_approved = execution_result["is_approved"]
            reason = execution_result["reason"]
            planned_orders = execution_result["planned_orders"]
            receipts = execution_result["receipts"]
            fills = execution_result["fills"]
            effective_pre_execution_state = execution_result["effective_pre_execution_state"]
            post_account_state = execution_result["post_account_state"]
            self._alert_unresolved_execution_timeouts(receipts)

        self._alert_risk_rejection(intent, reason, is_approved)
        return {
            "is_approved": is_approved,
            "reason": reason,
            "planned_orders": planned_orders,
            "receipts": receipts,
            "fills": fills,
            "effective_pre_execution_state": effective_pre_execution_state,
            "post_account_state": post_account_state,
        }
