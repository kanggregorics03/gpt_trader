from utils.logger import log


class TradingCycleAlertsMixin:
    def _coerce_rationale_item_summary(self, item) -> str:
        if hasattr(item, "summary"):
            return " ".join(str(getattr(item, "summary", "") or "").split())
        if isinstance(item, dict):
            return " ".join(str(item.get("summary") or "").split())
        return ""

    def _build_cycle_order_summary(self, execution_summary: dict, intent) -> str:
        execution_policy = dict((execution_summary or {}).get("execution_policy") or {})
        policy_orders = execution_policy.get("orders") or []
        if policy_orders:
            buckets = {}
            for order in policy_orders:
                action = str((order or {}).get("action") or "").strip().upper() or "UNKNOWN"
                order_type = str((order or {}).get("order_type") or "").strip().upper() or "UNKNOWN"
                entry_style = str((order or {}).get("entry_style") or "").strip().lower()
                action_type = str((order or {}).get("action_type") or "").strip().lower()
                semantic = entry_style or action_type or "managed"
                key = (action, order_type, semantic)
                buckets[key] = buckets.get(key, 0) + 1
            return "; ".join(
                f"{action} {order_type} {semantic} x{count}"
                for (action, order_type, semantic), count in buckets.items()
            )
        intent_orders = getattr(intent, "orders", []) or []
        if not intent_orders:
            return "none"
        buckets = {}
        for order in intent_orders:
            action = str(getattr(order, "action", "") or "").strip().upper() or "UNKNOWN"
            order_type = str(getattr(order, "order_type", "") or "").strip().upper() or "UNKNOWN"
            entry_style = str(getattr(order, "entry_style", "") or "").strip().lower()
            semantic = entry_style or ("reduce" if bool(getattr(order, "reduce_only", False)) else "managed")
            key = (action, order_type, semantic)
            buckets[key] = buckets.get(key, 0) + 1
        return "; ".join(
            f"{action} {order_type} {semantic} x{count}"
            for (action, order_type, semantic), count in buckets.items()
        )

    def _alert_cycle_decision_summary(
        self,
        *,
        cycle_context: dict,
        decision_outcome: dict,
        execution_outcome: dict,
        execution_summary: dict,
    ):
        intent = decision_outcome["intent"]
        decision_reason = ""
        rationale_lines = [
            " ".join(str(item or "").split())
            for item in (getattr(intent, "rationale", None) or [])
            if str(item or "").strip()
        ]
        if rationale_lines:
            decision_reason = " | ".join(rationale_lines[:2])
        if not decision_reason:
            decision_reason = str(execution_outcome.get("reason") or "").strip()
        decision_reason = " ".join(decision_reason.split())[:220]
        detail_summaries = [
            self._coerce_rationale_item_summary(item)
            for item in (getattr(intent, "rationale_items", None) or [])
            if self._coerce_rationale_item_summary(item)
        ]
        risk_notes = [
            " ".join(str(item or "").split())
            for item in (getattr(intent, "risk_notes", None) or [])
            if str(item or "").strip()
        ]
        approved = "yes" if bool(execution_outcome.get("is_approved")) else "no"
        model_name = str(getattr(decision_outcome.get("decision_result"), "model_name", "") or "").strip() or "unknown"
        lines = [
            (
                f"loop={cycle_context.get('loop_mode')} "
                f"risk={cycle_context.get('risk_mode')} "
                f"role={cycle_context.get('effective_risk_profile', {}).get('cycle_role') or ''} "
                f"decision={intent.decision} "
                f"approved={approved} "
                f"model={model_name}"
            ).strip(),
            f"why={decision_reason or 'n/a'}",
        ]
        if detail_summaries:
            lines.append(f"detail={' | '.join(detail_summaries[:2])[:220]}")
        if risk_notes:
            lines.append(f"risk={' | '.join(risk_notes[:2])[:180]}")
        lines.append(f"orders={self._build_cycle_order_summary(execution_summary, intent)}")
        message = "\n".join(lines)
        self.alert_manager.send(
            "Cycle Decision",
            message,
            level="info",
        )

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
