from typing import Any, Dict, List


class StateStoreTransitionQueryMixin:
    def get_recent_order_state_transitions(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                client_order_id,
                exchange_order_id,
                from_state,
                to_state,
                event_type,
                payload
            FROM order_state_transitions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_order_state_transitions_for_audit(self, audit_log_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                client_order_id,
                exchange_order_id,
                from_state,
                to_state,
                event_type,
                payload
            FROM order_state_transitions
            WHERE audit_log_id = ?
            ORDER BY id ASC
            """,
            (audit_log_id,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_recent_position_state_transitions(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                from_state,
                to_state,
                event_type,
                payload
            FROM position_state_transitions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_position_state_transitions_for_audit(self, audit_log_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                from_state,
                to_state,
                event_type,
                payload
            FROM position_state_transitions
            WHERE audit_log_id = ?
            ORDER BY id ASC
            """,
            (audit_log_id,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows
