from utils.logger import log


class TradingCycleOrderSubmissionMixin:
    def _plan_and_execute_orders(
        self,
        *,
        cycle_context: dict,
        intent,
        effective_pre_execution_state,
        execution_summary: dict,
        execution_guards: dict,
        loop_lock_owner: str,
    ):
        planned_orders = self.execution_engine.resolve_orders(
            intent,
            account_state=effective_pre_execution_state,
        )
        entry_orders = [] if intent.decision == "REPLACE_ENTRY" else self._extract_entry_orders(planned_orders)
        fingerprint_entries = [
            self._build_order_fingerprint(order, cycle_context["market_prices"])
            for order in entry_orders
        ]
        repeated = self._find_repeated_order_fingerprints(
            fingerprint_entries=fingerprint_entries,
            cooldown_minutes=execution_guards["decision_cooldown_minutes"],
        )
        if repeated:
            execution_summary["decision_cooldown_blocked"] = sorted(repeated)
            return (
                False,
                (
                    f"Decision cooldown active ({execution_guards['decision_cooldown_minutes']}m): repeated "
                    f"order fingerprint detected for {', '.join(sorted(repeated))}."
                ),
                planned_orders,
                [],
                [],
                [],
            )

        lock_ok, locked_entry_symbols = self._acquire_entry_mutexes(
            entry_orders,
            owner=loop_lock_owner,
            ttl_seconds=execution_guards["entry_mutex_ttl_seconds"],
        )
        if not lock_ok:
            return (
                False,
                "Entry mutex active for at least one symbol. Skipping duplicate entry execution.",
                planned_orders,
                locked_entry_symbols,
                [],
                [],
            )

        if fingerprint_entries:
            self.state_store.save_decision_fingerprints(
                [
                    {
                        **fp,
                        "audit_log_id": None,
                        "status": "planned",
                    }
                    for fp in fingerprint_entries
                ]
            )
        log.info(f"Risk Approved. Executing {len(planned_orders)} orders.")
        receipts = self.execution_engine.execute_intent(
            intent,
            account_state=effective_pre_execution_state,
        )
        execution_summary["executed_order_count"] = len(receipts)
        execution_summary["receipt_summary"] = self.execution_engine.summarize_receipts(receipts)
        fills = self.execution_engine.fetch_fills_for_receipts(receipts)
        execution_summary["fill_summary"] = {
            "fill_count": len(fills),
            "fee_cost": round(sum(float(fill.get("fee_cost") or 0.0) for fill in fills), 8),
            "realized_pnl": round(sum(float(fill.get("realized_pnl") or 0.0) for fill in fills), 8),
        }
        return True, "", planned_orders, locked_entry_symbols, receipts, fills

    def _fetch_post_execution_account_state(self, *, effective_pre_execution_state, execution_summary: dict):
        try:
            self.account_reader.invalidate_cache()
            post_account_state = self.account_reader.get_account_state(
                self.symbols,
                force_refresh=True,
            )
            execution_summary["post_account_state"] = post_account_state.model_dump()
            execution_summary["balance_delta"] = round(
                post_account_state.total_balance - effective_pre_execution_state.total_balance,
                8,
            )
            execution_summary["balance_delta_pct"] = self._calculate_balance_delta_pct(
                effective_pre_execution_state.total_balance,
                post_account_state.total_balance,
            )
            return post_account_state
        except Exception as execution_state_error:
            execution_summary["post_account_state_error"] = str(execution_state_error)
            log.warning(f"Failed to fetch post-execution account state: {execution_state_error}")
            return None
