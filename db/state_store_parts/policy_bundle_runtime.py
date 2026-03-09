import json
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStorePolicyBundleRuntimeMixin:
    def upsert_policy_bundle(self, record: Dict[str, Any]):
        bundle_key = str(record.get("bundle_key") or "").strip()
        if not bundle_key:
            return
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO policy_bundles (
                        bundle_key,
                        regime_key,
                        status,
                        alpha_policy_key,
                        position_policy_key,
                        safety_policy_key,
                        execution_policy_key,
                        metrics,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(bundle_key) DO UPDATE SET
                        regime_key = excluded.regime_key,
                        status = excluded.status,
                        alpha_policy_key = excluded.alpha_policy_key,
                        position_policy_key = excluded.position_policy_key,
                        safety_policy_key = excluded.safety_policy_key,
                        execution_policy_key = excluded.execution_policy_key,
                        metrics = excluded.metrics,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        bundle_key,
                        record.get("regime_key"),
                        record.get("status") or "candidate",
                        record.get("alpha_policy_key"),
                        record.get("position_policy_key"),
                        record.get("safety_policy_key"),
                        record.get("execution_policy_key"),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to upsert policy bundle: {e}")

    def get_policy_bundle(self, bundle_key: str) -> Dict[str, Any]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM policy_bundles
            WHERE bundle_key = ?
            LIMIT 1
            """,
            (bundle_key,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = dict(row)
        item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
        return item

    def get_active_policy_bundle(self, regime_key: Optional[str] = None) -> Dict[str, Any]:
        regime_text = str(regime_key or "").strip()
        if regime_text:
            cursor = self.conn.execute(
                """
                SELECT *
                FROM policy_bundles
                WHERE regime_key = ? AND status = 'active'
                ORDER BY updated_at DESC, bundle_key DESC
                LIMIT 1
                """,
                (regime_text,),
            )
            row = cursor.fetchone()
            if row:
                item = dict(row)
                item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
                return item
        cursor = self.conn.execute(
            """
            SELECT *
            FROM policy_bundles
            WHERE (regime_key IS NULL OR regime_key = '') AND status = 'active'
            ORDER BY updated_at DESC, bundle_key DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = dict(row)
        item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
        return item

    def get_policy_bundles(
        self,
        regime_key: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM policy_bundles"
        clauses = []
        params: List[Any] = []
        regime_text = str(regime_key or "").strip()
        if regime_key is not None:
            if regime_text:
                clauses.append("COALESCE(regime_key, '') = ?")
                params.append(regime_text)
            else:
                clauses.append("(regime_key IS NULL OR regime_key = '')")
        clean_statuses = [str(status).strip() for status in (statuses or []) if str(status).strip()]
        if clean_statuses:
            placeholders = ", ".join("?" for _ in clean_statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(clean_statuses)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, bundle_key DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def save_bundle_evaluation(self, record: Dict[str, Any]) -> Optional[int]:
        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    INSERT INTO bundle_evaluations (
                        bundle_key,
                        evaluation_scope,
                        sample_count,
                        score,
                        metrics
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("bundle_key"),
                        record.get("evaluation_scope"),
                        record.get("sample_count", 0),
                        record.get("score", 0.0),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
                return int(cursor.lastrowid)
        except Exception as e:
            logger.error(f"Failed to save bundle evaluation: {e}")
            return None

    def get_bundle_evaluations(self, bundle_key: str) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM bundle_evaluations
            WHERE bundle_key = ?
            ORDER BY created_at DESC, id DESC
            """,
            (bundle_key,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def save_bundle_promotion(self, record: Dict[str, Any]):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO bundle_promotions (
                        regime_key,
                        previous_bundle_key,
                        new_bundle_key,
                        reason,
                        metrics
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("regime_key"),
                        record.get("previous_bundle_key"),
                        record.get("new_bundle_key"),
                        record.get("reason"),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
                self.conn.execute(
                    """
                    UPDATE policy_bundles
                    SET status = CASE WHEN bundle_key = ? THEN 'active' ELSE status END,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE bundle_key = ?
                    """,
                    (
                        record.get("new_bundle_key"),
                        record.get("new_bundle_key"),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to save bundle promotion: {e}")

    def get_recent_bundle_promotions(self, regime_key: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        query = "SELECT * FROM bundle_promotions"
        params: List[Any] = []
        regime_text = str(regime_key or "").strip()
        if regime_key is not None:
            if regime_text:
                query += " WHERE COALESCE(regime_key, '') = ?"
                params.append(regime_text)
            else:
                query += " WHERE (regime_key IS NULL OR regime_key = '')"
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows
