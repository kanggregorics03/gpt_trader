import json
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStorePolicyRegistryRuntimeMixin:
    def save_candidate_policy_version(self, record: Dict[str, Any]) -> Optional[int]:
        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    INSERT INTO candidate_policy_versions (
                        domain,
                        policy_type,
                        regime_key,
                        policy_name,
                        source,
                        status,
                        rule_text,
                        config,
                        metrics,
                        evaluated_at,
                        promoted_at,
                        retired_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("domain"),
                        record.get("policy_type"),
                        record.get("regime_key"),
                        record.get("policy_name"),
                        record.get("source"),
                        record.get("status") or "candidate",
                        record.get("rule_text"),
                        json.dumps(record.get("config") or {}),
                        json.dumps(record.get("metrics") or {}),
                        record.get("evaluated_at"),
                        record.get("promoted_at"),
                        record.get("retired_at"),
                    ),
                )
                return int(cursor.lastrowid)
        except Exception as e:
            logger.error(f"Failed to save candidate policy version: {e}")
            return None

    def get_candidate_policy_versions(
        self,
        domain: Optional[str] = None,
        regime_key: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM candidate_policy_versions"
        clauses = []
        params: List[Any] = []
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if regime_key is not None:
            if str(regime_key).strip():
                clauses.append("COALESCE(regime_key, '') = ?")
                params.append(str(regime_key).strip())
            else:
                clauses.append("(regime_key IS NULL OR regime_key = '')")
        clean_statuses = [str(status).strip() for status in (statuses or []) if str(status).strip()]
        if clean_statuses:
            placeholders = ", ".join("?" for _ in clean_statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(clean_statuses)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["config"] = self._safe_json_loads(item.get("config"), default={})
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def update_candidate_policy_version(
        self,
        candidate_policy_id: int,
        *,
        status: Optional[str] = None,
        policy_name: Optional[str] = None,
        rule_text: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
        evaluated_at: Optional[str] = None,
        promoted_at: Optional[str] = None,
        retired_at: Optional[str] = None,
    ) -> bool:
        try:
            candidate_id = int(candidate_policy_id)
        except (TypeError, ValueError):
            return False
        updates: List[str] = []
        params: List[Any] = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if policy_name is not None:
            updates.append("policy_name = ?")
            params.append(policy_name)
        if rule_text is not None:
            updates.append("rule_text = ?")
            params.append(rule_text)
        if config is not None:
            updates.append("config = ?")
            params.append(json.dumps(config))
        if metrics is not None:
            updates.append("metrics = ?")
            params.append(json.dumps(metrics))
        if evaluated_at is not None:
            updates.append("evaluated_at = ?")
            params.append(evaluated_at)
        if promoted_at is not None:
            updates.append("promoted_at = ?")
            params.append(promoted_at)
        if retired_at is not None:
            updates.append("retired_at = ?")
            params.append(retired_at)
        if not updates:
            return False
        params.append(candidate_id)
        try:
            with self.conn:
                self.conn.execute(
                    f"""
                    UPDATE candidate_policy_versions
                    SET {", ".join(updates)}
                    WHERE id = ?
                    """,
                    params,
                )
            return True
        except Exception as e:
            logger.error(f"Failed to update candidate policy version {candidate_id}: {e}")
            return False

    def save_policy_evaluation(self, record: Dict[str, Any]) -> Optional[int]:
        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    INSERT INTO policy_evaluations (
                        candidate_policy_id,
                        evaluation_scope,
                        sample_count,
                        score,
                        metrics
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("candidate_policy_id"),
                        record.get("evaluation_scope"),
                        record.get("sample_count", 0),
                        record.get("score", 0.0),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
                with self.conn:
                    self.conn.execute(
                        """
                        UPDATE candidate_policy_versions
                        SET evaluated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (record.get("candidate_policy_id"),),
                    )
                return int(cursor.lastrowid)
        except Exception as e:
            logger.error(f"Failed to save policy evaluation: {e}")
            return None

    def get_policy_evaluations(self, candidate_policy_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM policy_evaluations
            WHERE candidate_policy_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (candidate_policy_id,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def upsert_policy_registry(self, record: Dict[str, Any]):
        policy_key = str(record.get("policy_key") or "").strip()
        if not policy_key:
            return
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO policy_registry (
                        policy_key,
                        domain,
                        policy_type,
                        regime_key,
                        active_candidate_policy_id,
                        version,
                        config,
                        metrics,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(policy_key) DO UPDATE SET
                        domain = excluded.domain,
                        policy_type = excluded.policy_type,
                        regime_key = excluded.regime_key,
                        active_candidate_policy_id = excluded.active_candidate_policy_id,
                        version = excluded.version,
                        config = excluded.config,
                        metrics = excluded.metrics,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        policy_key,
                        record.get("domain"),
                        record.get("policy_type"),
                        record.get("regime_key"),
                        record.get("active_candidate_policy_id"),
                        record.get("version", 1),
                        json.dumps(record.get("config") or {}),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to upsert policy registry: {e}")

    def get_policy_registry_entry(self, policy_key: str) -> Dict[str, Any]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM policy_registry
            WHERE policy_key = ?
            LIMIT 1
            """,
            (policy_key,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = dict(row)
        item["config"] = self._safe_json_loads(item.get("config"), default={})
        item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
        return item

    def build_policy_key(self, domain: str, regime_key: Optional[str] = None) -> str:
        domain_text = str(domain or "").strip()
        regime_text = str(regime_key or "").strip()
        if regime_text:
            return f"champion:{domain_text}:{regime_text}"
        return f"champion:{domain_text}"

    def get_policy_registry_entry_for_regime(self, domain: str, regime_key: Optional[str] = None) -> Dict[str, Any]:
        regime_text = str(regime_key or "").strip()
        if regime_text:
            scoped = self.get_policy_registry_entry(self.build_policy_key(domain, regime_text))
            if scoped:
                return scoped
        return self.get_policy_registry_entry(self.build_policy_key(domain))

    def list_policy_registry(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM policy_registry"
        params: List[Any] = []
        if domain:
            query += " WHERE domain = ?"
            params.append(domain)
        query += " ORDER BY updated_at DESC, policy_key DESC"
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["config"] = self._safe_json_loads(item.get("config"), default={})
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def save_policy_promotion(self, record: Dict[str, Any]):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO policy_promotions (
                        domain,
                        regime_key,
                        previous_candidate_policy_id,
                        new_candidate_policy_id,
                        reason,
                        metrics
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("domain"),
                        record.get("regime_key"),
                        record.get("previous_candidate_policy_id"),
                        record.get("new_candidate_policy_id"),
                        record.get("reason"),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
                self.conn.execute(
                    """
                    UPDATE candidate_policy_versions
                    SET status = 'promoted', promoted_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (record.get("new_candidate_policy_id"),),
                )
        except Exception as e:
            logger.error(f"Failed to save policy promotion: {e}")
