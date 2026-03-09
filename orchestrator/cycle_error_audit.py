from utils.logger import log


class TradingCycleErrorAuditMixin:
    def _build_cycle_error_result(self, loop_type: str, error: Exception) -> dict:
        log.error(f"Error in {loop_type} cycle: {error}")
        self.state_store.save_audit(
            account={},
            market={},
            intent={
                "decision": "HOLD",
                "orders": [],
                "rationale": [f"Loop error during {loop_type}."],
                "risk_notes": ["System Error"],
                "confidence": 0.0,
            },
            risk_ok=False,
            reason=f"Error in {loop_type} cycle: {error}",
            receipts=[],
            execution_summary={"loop_type": loop_type, "error": str(error)},
        )
        self.alert_manager.send(
            "Trading Loop Error",
            f"{loop_type} cycle failed: {error}",
            level="error",
        )
        return {
            "loop_type": loop_type,
            "trigger_active": self.active_loop_mode == "5m",
            "has_positions": True,
            "has_open_orders": True,
            "has_unknown_orders": True,
            "executed_order_count": 0,
            "position_closed": False,
            "final_account_state": None,
            "error": str(error),
        }
