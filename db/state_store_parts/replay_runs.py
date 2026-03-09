import json
from typing import Any, Dict

from loguru import logger


class StateStoreReplayRunMixin:
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
        except Exception as e:
            logger.error(f"Failed to save replay run: {e}")
