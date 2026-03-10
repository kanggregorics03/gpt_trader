import os
import time

from orchestrator.cycle_decision import TradingCycleDecisionMixin
from orchestrator.cycle_persistence import TradingCyclePersistenceMixin
from utils.logger import log


class TradingCycleExecutionMixin(
    TradingCycleDecisionMixin,
    TradingCyclePersistenceMixin,
):
    def run_cycle(self, loop_type: str, snapshot_bundle=None, loop_mode: str | None = None):
        log.info(f"--- Starting {loop_type} Cycle ---")
        execution_guards = self._build_execution_guards()
        self.last_context_selection_summary = self._build_empty_context_selection_summary()
        loop_mode = loop_mode or self.active_loop_mode
        loop_lock_name = "trading_orchestrator_loop"
        loop_lock_owner = f"{loop_type}:{os.getpid()}:{int(time.time() * 1000)}"
        if not self.state_store.try_acquire_runtime_lock(
            loop_lock_name,
            owner=loop_lock_owner,
            ttl_seconds=execution_guards["loop_lock_ttl_seconds"],
        ):
            log.warning(f"Skipping {loop_type}: loop lock is already held by another worker.")
            return {
                "loop_type": loop_type,
                "guarded_attention_active": self.active_loop_mode == "5m",
                "safety_trigger_active": self.active_loop_mode == "5m",
                "has_positions": False,
                "has_open_orders": False,
                "has_unknown_orders": False,
                "executed_order_count": 0,
                "position_closed": False,
                "final_account_state": None,
                "locked": True,
                "lock_skipped": True,
            }
        try:
            cycle_context = self._build_cycle_context(
                snapshot_bundle=snapshot_bundle,
                loop_mode=loop_mode,
                execution_guards=execution_guards,
            )
            decision_outcome = self._resolve_cycle_decision(cycle_context)
            risk_outcome = self._evaluate_cycle_risk(
                cycle_context,
                decision_outcome["intent"],
                execution_guards,
            )
            execution_summary, trigger_symbols, risk_trigger_symbols = self._build_execution_summary(
                cycle_context,
                decision_outcome["decision_result"],
                risk_outcome,
                loop_type,
            )
            execution_summary["execution_policy"] = decision_outcome["execution_policy_summary"]
            execution_outcome = self._execute_cycle_intent(
                cycle_context=cycle_context,
                intent=decision_outcome["intent"],
                risk_outcome=risk_outcome,
                execution_summary=execution_summary,
                execution_guards=execution_guards,
                loop_lock_owner=loop_lock_owner,
            )
            return self._persist_cycle_results(
                cycle_context=cycle_context,
                decision_outcome=decision_outcome,
                risk_outcome=risk_outcome,
                execution_outcome=execution_outcome,
                execution_summary=execution_summary,
                trigger_symbols=trigger_symbols,
                risk_trigger_symbols=risk_trigger_symbols,
                execution_guards=execution_guards,
            )
        except Exception as error:
            return self._build_cycle_error_result(loop_type, error)
        finally:
            self.state_store.release_runtime_lock(loop_lock_name, owner=loop_lock_owner)
