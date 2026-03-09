import json
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStoreActiveTradeCaseMixin:
    def get_active_trade_case(self, symbol: str) -> Dict[str, Any]:
        if not symbol:
            return {}
        cursor = self.conn.execute(
            """
            SELECT
                symbol,
                trade_case_id,
                opened_at,
                entry_decision,
                thesis_id,
                management_template_id,
                last_decision,
                last_audit_log_id,
                payload
            FROM active_trade_cases
            WHERE symbol = ?
            """,
            (symbol,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = dict(row)
        item["payload"] = self._safe_json_loads(item.get("payload"), default={})
        return item

    def get_all_active_trade_cases(self) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                symbol,
                trade_case_id,
                opened_at,
                entry_decision,
                thesis_id,
                management_template_id,
                last_decision,
                last_audit_log_id,
                payload
            FROM active_trade_cases
            ORDER BY opened_at ASC, symbol ASC
            """
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def upsert_active_trade_case(
        self,
        symbol: str,
        trade_case_id: str,
        opened_at: Optional[str],
        entry_decision: Optional[str] = None,
        thesis_id: Optional[str] = None,
        management_template_id: Optional[str] = None,
        last_decision: Optional[str] = None,
        last_audit_log_id: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ):
        if not symbol or not trade_case_id:
            return
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO active_trade_cases (
                        symbol,
                        trade_case_id,
                        opened_at,
                        entry_decision,
                        thesis_id,
                        management_template_id,
                        last_decision,
                        last_audit_log_id,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        trade_case_id = excluded.trade_case_id,
                        opened_at = excluded.opened_at,
                        entry_decision = excluded.entry_decision,
                        thesis_id = excluded.thesis_id,
                        management_template_id = excluded.management_template_id,
                        last_decision = excluded.last_decision,
                        last_audit_log_id = excluded.last_audit_log_id,
                        payload = excluded.payload
                    """,
                    (
                        symbol,
                        trade_case_id,
                        opened_at,
                        entry_decision,
                        thesis_id,
                        management_template_id,
                        last_decision,
                        last_audit_log_id,
                        json.dumps(payload or {}),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to upsert active trade case: {e}")

    def close_active_trade_case(
        self,
        symbol: str,
        closed_at: Optional[str] = None,
        last_decision: Optional[str] = None,
        last_audit_log_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        existing = self.get_active_trade_case(symbol)
        if not existing:
            return {}
        payload = dict(existing.get("payload") or {})
        if closed_at:
            payload["closed_at"] = closed_at
        if last_decision:
            payload["last_decision"] = last_decision
        if last_audit_log_id is not None:
            payload["last_audit_log_id"] = last_audit_log_id
        existing["payload"] = payload
        try:
            with self.conn:
                self.conn.execute(
                    "DELETE FROM active_trade_cases WHERE symbol = ?",
                    (symbol,),
                )
        except Exception as e:
            logger.error(f"Failed to close active trade case: {e}")
        return existing
