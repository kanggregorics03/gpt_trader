import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStoreLearningRuntimeMixin:
    def save_shadow_decisions(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO shadow_decisions (
                        audit_log_id,
                        candidate_policy_id,
                        domain,
                        symbol,
                        loop_mode,
                        guarded_attention,
                        safety_mode,
                        trade_case_id,
                        divergence_type,
                        outcome_status,
                        outcome_estimated_pnl_pct,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            entry.get("audit_log_id"),
                            entry.get("candidate_policy_id"),
                            entry.get("domain"),
                            entry.get("symbol"),
                            entry.get("loop_mode"),
                            int(bool(entry.get("guarded_attention"))),
                            int(bool(entry.get("safety_mode"))),
                            entry.get("trade_case_id"),
                            entry.get("divergence_type"),
                            entry.get("outcome_status"),
                            entry.get("outcome_estimated_pnl_pct"),
                            json.dumps(entry.get("payload") or {}),
                        )
                        for entry in entries
                        if entry.get("audit_log_id") and entry.get("candidate_policy_id")
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save shadow decisions: {e}")

    def upsert_learning_artifact(
        self,
        artifact_key: str,
        payload: Dict[str, Any],
        *,
        artifact_type: Optional[str] = None,
    ) -> None:
        key = str(artifact_key or "").strip()
        if not key:
            return
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO learning_artifacts (
                        artifact_key,
                        artifact_type,
                        payload,
                        updated_at
                    ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(artifact_key) DO UPDATE SET
                        artifact_type = excluded.artifact_type,
                        payload = excluded.payload,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        key,
                        artifact_type or key,
                        json.dumps(payload or {}),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to upsert learning artifact {key}: {e}")

    def get_learning_artifact(self, artifact_key: str) -> Dict[str, Any]:
        key = str(artifact_key or "").strip()
        if not key:
            return {}
        cursor = self.conn.execute(
            """
            SELECT artifact_key, artifact_type, payload, updated_at
            FROM learning_artifacts
            WHERE artifact_key = ?
            LIMIT 1
            """,
            (key,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = dict(row)
        item["payload"] = self._safe_json_loads(item.get("payload"), default={})
        return item

    def list_learning_artifacts(self, prefix: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        query = "SELECT artifact_key, artifact_type, payload, updated_at FROM learning_artifacts"
        params: List[Any] = []
        if prefix:
            query += " WHERE artifact_key LIKE ?"
            params.append(f"{prefix}%")
        query += " ORDER BY updated_at DESC, artifact_key DESC LIMIT ?"
        params.append(max(1, int(limit or 20)))
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_shadow_decisions_since(self, since: datetime) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM shadow_decisions
            WHERE created_at >= ?
            ORDER BY id ASC
            """,
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def update_shadow_decisions_for_trade_case(
        self,
        trade_case_id: str,
        outcome_status: Optional[str] = None,
        outcome_estimated_pnl_pct: Optional[float] = None,
    ):
        if not trade_case_id:
            return
        try:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE shadow_decisions
                    SET
                        outcome_status = COALESCE(?, outcome_status),
                        outcome_estimated_pnl_pct = COALESCE(?, outcome_estimated_pnl_pct)
                    WHERE trade_case_id = ?
                    """,
                    (
                        outcome_status,
                        outcome_estimated_pnl_pct,
                        trade_case_id,
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to update shadow decision outcomes: {e}")
