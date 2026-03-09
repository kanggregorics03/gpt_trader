import json
from datetime import UTC, datetime
from typing import Any, Dict, Optional


class StateStoreOrderRuntimeMixin:
    def _normalize_order_state(self, value: Any) -> str:
        state = str(value or "unknown").strip().lower()
        aliases = {
            "none": "none",
            "deferred": "deferred",
            "submitted": "submitted",
            "cancel_requested": "cancel_requested",
            "partial": "partial_filled",
            "partial_filled": "partial_filled",
            "partially_filled": "partial_filled",
            "partiallyfilled": "partial_filled",
            "completed": "completed",
            "filled": "completed",
            "closed": "completed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "failed": "failed",
            "rejected": "failed",
            "unknown": "unknown",
        }
        return aliases.get(state, "unknown")

    def _is_valid_order_state_transition(self, from_state: str, to_state: str) -> bool:
        normalized_from = self._normalize_order_state(from_state)
        normalized_to = self._normalize_order_state(to_state)
        allowed = {
            "none": {"deferred", "submitted", "unknown"},
            "deferred": {"deferred", "submitted", "failed", "unknown"},
            "cancel_requested": {"cancel_requested", "cancelled", "failed", "unknown"},
            "submitted": {"submitted", "cancel_requested", "partial_filled", "completed", "cancelled", "failed", "unknown"},
            "partial_filled": {"partial_filled", "cancel_requested", "completed", "cancelled", "failed", "unknown"},
            "unknown": {"unknown", "submitted", "cancel_requested", "partial_filled", "completed", "cancelled", "failed"},
            "cancelled": {"cancelled"},
            "failed": {"failed"},
            "completed": {"completed"},
        }
        return normalized_to in allowed.get(normalized_from, {"unknown"})

    def _upsert_order_runtime_state(
        self,
        order_key: Optional[str],
        client_order_id: Optional[str],
        exchange_order_id: Optional[str],
        symbol: Optional[str],
        replace_group_id: Optional[str],
        lineage_role: Optional[str],
        lineage_parent_order_id: Optional[str],
        lineage_parent_client_order_id: Optional[str],
        state: str,
        audit_log_id: int,
        payload: Dict[str, Any],
    ):
        if not order_key:
            return
        self.conn.execute(
            """
            INSERT INTO order_runtime_state (
                order_key,
                client_order_id,
                exchange_order_id,
                symbol,
                replace_group_id,
                lineage_role,
                lineage_parent_order_id,
                lineage_parent_client_order_id,
                state,
                updated_at,
                last_audit_log_id,
                payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
            ON CONFLICT(order_key) DO UPDATE SET
                client_order_id = excluded.client_order_id,
                exchange_order_id = excluded.exchange_order_id,
                symbol = excluded.symbol,
                replace_group_id = excluded.replace_group_id,
                lineage_role = excluded.lineage_role,
                lineage_parent_order_id = excluded.lineage_parent_order_id,
                lineage_parent_client_order_id = excluded.lineage_parent_client_order_id,
                state = excluded.state,
                updated_at = CURRENT_TIMESTAMP,
                last_audit_log_id = excluded.last_audit_log_id,
                payload = excluded.payload
            """,
            (
                order_key,
                client_order_id,
                exchange_order_id,
                symbol,
                replace_group_id,
                lineage_role,
                lineage_parent_order_id,
                lineage_parent_client_order_id,
                self._normalize_order_state(state),
                audit_log_id,
                json.dumps(payload or {}),
            ),
        )

    def _utcnow(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)
