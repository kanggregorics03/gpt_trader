import json
from typing import Any, Dict, List

from loguru import logger


class StateStoreRuntimeTransitionMixin:
    def save_order_state_transitions(self, audit_log_id: int, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                for entry in entries:
                    client_order_id = entry.get("client_order_id")
                    exchange_order_id = entry.get("exchange_order_id")
                    order_key = client_order_id or exchange_order_id
                    from_state = self._normalize_order_state(entry.get("from_state"))
                    to_state = self._normalize_order_state(entry.get("to_state"))
                    if order_key:
                        runtime_row = self.conn.execute(
                            """
                            SELECT state
                            FROM order_runtime_state
                            WHERE order_key = ?
                            LIMIT 1
                            """,
                            (order_key,),
                        ).fetchone()
                        if runtime_row and runtime_row["state"]:
                            from_state = self._normalize_order_state(runtime_row["state"])
                    if not self._is_valid_order_state_transition(from_state, to_state):
                        logger.warning(
                            "Invalid order state transition detected (audit_id={}): {} -> {}. Coercing to unknown.",
                            audit_log_id,
                            from_state,
                            to_state,
                        )
                        to_state = "unknown"

                    payload = entry.get("payload", {})
                    self.conn.execute(
                        """
                        INSERT INTO order_state_transitions (
                            audit_log_id,
                            symbol,
                            client_order_id,
                            exchange_order_id,
                            replace_group_id,
                            lineage_role,
                            lineage_parent_order_id,
                            lineage_parent_client_order_id,
                            from_state,
                            to_state,
                            event_type,
                            payload
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            audit_log_id,
                            entry.get("symbol"),
                            client_order_id,
                            exchange_order_id,
                            entry.get("replace_group_id") or payload.get("replace_group_id"),
                            entry.get("lineage_role") or payload.get("lineage_role"),
                            entry.get("lineage_parent_order_id") or payload.get("lineage_parent_order_id"),
                            entry.get("lineage_parent_client_order_id") or payload.get("lineage_parent_client_order_id"),
                            from_state,
                            to_state,
                            entry.get("event_type"),
                            json.dumps(payload),
                        ),
                    )
                    self._upsert_order_runtime_state(
                        order_key=order_key,
                        client_order_id=client_order_id,
                        exchange_order_id=exchange_order_id,
                        symbol=entry.get("symbol"),
                        replace_group_id=entry.get("replace_group_id") or payload.get("replace_group_id"),
                        lineage_role=entry.get("lineage_role") or payload.get("lineage_role"),
                        lineage_parent_order_id=entry.get("lineage_parent_order_id") or payload.get("lineage_parent_order_id"),
                        lineage_parent_client_order_id=entry.get("lineage_parent_client_order_id") or payload.get("lineage_parent_client_order_id"),
                        state=to_state,
                        audit_log_id=audit_log_id,
                        payload=payload,
                    )
        except Exception as e:
            logger.error(f"Failed to save order state transitions: {e}")

    def save_position_state_transitions(self, audit_log_id: int, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO position_state_transitions (
                        audit_log_id,
                        symbol,
                        from_state,
                        to_state,
                        event_type,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_log_id,
                            entry.get("symbol"),
                            entry.get("from_state"),
                            entry.get("to_state"),
                            entry.get("event_type"),
                            json.dumps(entry.get("payload", {})),
                        )
                        for entry in entries
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save position state transitions: {e}")
