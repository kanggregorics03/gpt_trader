from utils.logger import log


class TradingCycleAlertsMixin:
    def _alert_unresolved_execution_timeouts(self, receipts: list[dict]):
        unresolved = [receipt for receipt in receipts if receipt.get("status") == "timeout_unresolved"]
        if unresolved:
            unresolved_symbols = sorted(
                {receipt.get("symbol") for receipt in unresolved if receipt.get("symbol")}
            )
            self.alert_manager.send(
                "Execution Timeout",
                (
                    f"{len(unresolved)} unresolved timed out order(s)"
                    f" for {', '.join(unresolved_symbols) if unresolved_symbols else 'portfolio'}."
                ),
                level="error",
            )

    def _alert_risk_rejection(self, intent, reason: str, is_approved: bool):
        if is_approved:
            return
        log.warning(f"Risk Rejected: {reason}")
        if (
            not self._is_daily_kill_switch_reason(reason)
            and (
                "Kill switch" in reason
                or "Cooldown" in reason
                or "unresolved order lifecycle" in reason.lower()
                or "decision cooldown active" in reason.lower()
            )
        ):
            self.alert_manager.send(
                "Risk Engine Blocked Trade",
                f"Portfolio {intent.decision} rejected: {reason}",
                level="warning",
            )
