class TradingCyclePreflightMixin:
    def _run_order_execution_flow(
        self,
        *,
        cycle_context: dict,
        intent,
        execution_summary: dict,
        execution_guards: dict,
        loop_lock_owner: str,
    ) -> dict:
        is_approved = True
        reason = ""
        planned_orders = []
        receipts = []
        fills = []
        effective_pre_execution_state = cycle_context["account_state"]
        post_account_state = None
        locked_entry_symbols = []
        try:
            self.account_reader.invalidate_cache()
            effective_pre_execution_state = self.account_reader.get_account_state(
                self.symbols,
                force_refresh=True,
            )
            reapproval = self._reapprove_before_execution(
                cycle_context=cycle_context,
                intent=intent,
                effective_pre_execution_state=effective_pre_execution_state,
                execution_summary=execution_summary,
                execution_guards=execution_guards,
            )
            is_approved = reapproval["is_approved"]
            reason = reapproval["reason"]
            if is_approved:
                (
                    is_approved,
                    reason,
                    planned_orders,
                    locked_entry_symbols,
                    receipts,
                    fills,
                ) = self._plan_and_execute_orders(
                    cycle_context=cycle_context,
                    intent=intent,
                    effective_pre_execution_state=effective_pre_execution_state,
                    execution_summary=execution_summary,
                    execution_guards=execution_guards,
                    loop_lock_owner=loop_lock_owner,
                )
        finally:
            self._release_entry_mutexes(locked_entry_symbols, owner=loop_lock_owner)

        if is_approved and receipts:
            post_account_state = self._fetch_post_execution_account_state(
                effective_pre_execution_state=effective_pre_execution_state,
                execution_summary=execution_summary,
            )
        return {
            "is_approved": is_approved,
            "reason": reason,
            "planned_orders": planned_orders,
            "receipts": receipts,
            "fills": fills,
            "effective_pre_execution_state": effective_pre_execution_state,
            "post_account_state": post_account_state,
        }

    def _reapprove_before_execution(
        self,
        *,
        cycle_context: dict,
        intent,
        effective_pre_execution_state,
        execution_summary: dict,
        execution_guards: dict,
    ) -> dict:
        unresolved_after_reconcile = self.state_store.count_recent_unknown_orders(
            minutes=execution_guards["unknown_order_block_minutes"]
        )
        reapproved, rereason = self.risk_engine.evaluate_intent(
            intent,
            effective_pre_execution_state,
            market_snapshots=cycle_context["market_snapshots"],
            market_prices=cycle_context["market_prices"],
            risk_state=cycle_context["risk_state"],
            is_trigger_mode=cycle_context["is_trigger_mode"],
            unresolved_timeout_count=unresolved_after_reconcile,
            active_profile_override=cycle_context["effective_risk_profile"],
        )
        execution_summary["pre_trade_reconciliation"] = {
            "reapproved": bool(reapproved),
            "reason": rereason,
            "unknown_order_states": unresolved_after_reconcile,
        }
        if not reapproved:
            return {
                "is_approved": False,
                "reason": f"Pre-trade reconciliation rejected: {rereason}",
            }
        return {"is_approved": True, "reason": rereason}

    def _find_repeated_order_fingerprints(
        self,
        *,
        fingerprint_entries: list[dict],
        cooldown_minutes: int,
    ) -> list[str]:
        repeated = []
        for fp in fingerprint_entries:
            if self.state_store.has_recent_decision_fingerprint(
                symbol=fp["symbol"],
                action=fp["action"],
                size_band=fp["size_band"],
                within_minutes=cooldown_minutes,
            ):
                repeated.append(f"{fp['symbol']}:{fp['action']}:{fp['size_band']}")
        return repeated
