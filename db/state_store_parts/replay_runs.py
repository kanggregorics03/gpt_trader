import json
from typing import Any, Dict

from loguru import logger


class StateStoreReplayRunMixin:
    def _extract_replay_hold_quality_annotation(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        counterfactual = payload.get("hold_opportunity_counterfactual") or {}
        hold_quality = str(counterfactual.get("hold_quality") or "").strip().lower()
        hold_quality_detail = str(counterfactual.get("hold_quality_detail") or hold_quality).strip().lower()
        if not hold_quality:
            return {}
        if hold_quality in {"no_trigger_hold", "risk_filtered_hold", "execution_filtered_hold", "structure_filtered_hold"}:
            hold_quality = "good_hold"
        if hold_quality not in {"good_hold", "missed_opportunity_hold", "probe_only_opportunity_hold"}:
            return {}
        trigger_context = counterfactual.get("trigger_context") or {}
        execution_context = counterfactual.get("execution_context") or {}
        trend_context = counterfactual.get("trend_context") or {}
        if hold_quality == "good_hold":
            summary = "Replay validated this HOLD."
            trade_implication = "Keep the conservative HOLD unless new trigger context emerges."
            failure_mode = "Do not reuse this HOLD if the trigger or execution context materially changes."
        else:
            summary = "Replay found this HOLD missed a tradeable opportunity."
            trade_implication = "Allow a smaller probe entry when the same trigger and execution context repeat."
            failure_mode = "Do not escalate if spread/depth or trend alignment is worse than the replay context."
        return {
            "summary": summary,
            "why_it_matters": (
                "Replay counterfactual reviewed this HOLD against the stored market path "
                "instead of relying on a no-trigger heuristic."
            ),
            "trade_implication": trade_implication,
            "failure_mode": failure_mode,
            "hold_quality": hold_quality,
            "hold_quality_detail": hold_quality_detail,
            "source": "hold_opportunity_counterfactual",
            "hold_opportunity_cost": float(counterfactual.get("hold_opportunity_cost") or 0.0),
            "missed_breakout_count": int(counterfactual.get("missed_breakout_count") or 0),
            "baseline_beats_hold": bool(counterfactual.get("baseline_beats_hold")),
            "simple_rule_baseline_beats_hold": bool(counterfactual.get("simple_rule_baseline_beats_hold")),
            "trigger_context": str(trigger_context.get("signature") or ""),
            "execution_context": str(execution_context.get("signature") or ""),
            "trend_context": str(trend_context.get("signature") or ""),
        }

    def _upsert_journal_hold_quality_annotation(self, audit_log_id: int, payload: Dict[str, Any]) -> None:
        annotation = self._extract_replay_hold_quality_annotation(payload)
        if not annotation or not audit_log_id:
            return
        cursor = self.conn.execute(
            """
            SELECT id, decision, rationale_json
            FROM journal
            WHERE audit_log_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (audit_log_id,),
        )
        row = cursor.fetchone()
        if not row or str(row["decision"] or "").upper() != "HOLD":
            return
        rationale_items_raw = row["rationale_json"]
        if isinstance(rationale_items_raw, str) and rationale_items_raw.strip():
            try:
                rationale_items = json.loads(rationale_items_raw)
            except json.JSONDecodeError:
                rationale_items = []
        elif isinstance(rationale_items_raw, list):
            rationale_items = rationale_items_raw
        else:
            rationale_items = []
        if not isinstance(rationale_items, list):
            rationale_items = []

        updated_items = []
        replaced = False
        for item in rationale_items:
            if not isinstance(item, dict):
                updated_items.append(item)
                continue
            if str(item.get("source") or "").strip().lower() == "hold_opportunity_counterfactual":
                updated_items.append({**item, **annotation})
                replaced = True
            else:
                updated_items.append(item)
        if not replaced:
            updated_items.insert(0, annotation)
        with self.conn:
            self.conn.execute(
                """
                UPDATE journal
                SET rationale_json = ?
                WHERE id = ?
                """,
                (json.dumps(updated_items), int(row["id"])),
            )

    def save_replay_run(self, audit_log_id: int, payload: Dict[str, Any]):
        comparison = payload.get("comparison") or {}
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO replay_runs (
                        audit_log_id,
                        original_decision,
                        replayed_decision,
                        decision_changed,
                        order_count_changed,
                        side_changed,
                        order_type_changed,
                        execution_profile_changed,
                        size_delta,
                        confidence_delta,
                        simulated_fill_ratio,
                        simulated_slippage_bps,
                        simulated_latency_seconds,
                        rationale_keywords_added,
                        rationale_keywords_removed,
                        evidence_signals_added,
                        evidence_signals_removed,
                        evidence_signal_count_delta,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        audit_log_id,
                        comparison.get("original_decision"),
                        comparison.get("replayed_decision"),
                        int(bool(comparison.get("decision_changed"))),
                        int(bool(comparison.get("order_count_changed"))),
                        int(bool(comparison.get("side_changed"))),
                        int(bool(comparison.get("order_type_changed"))),
                        int(bool(comparison.get("execution_profile_changed"))),
                        float(comparison.get("size_delta") or 0.0),
                        float(comparison.get("confidence_delta") or 0.0),
                        payload.get("simulated_fill_ratio"),
                        payload.get("simulated_slippage_bps"),
                        payload.get("simulated_latency_seconds"),
                        json.dumps(comparison.get("rationale_keywords_added") or []),
                        json.dumps(comparison.get("rationale_keywords_removed") or []),
                        json.dumps(comparison.get("evidence_signals_added") or []),
                        json.dumps(comparison.get("evidence_signals_removed") or []),
                        int(comparison.get("evidence_signal_count_delta") or 0),
                        json.dumps(payload),
                    ),
                )
            self._upsert_journal_hold_quality_annotation(audit_log_id, payload)
        except Exception as e:
            logger.error(f"Failed to save replay run: {e}")
