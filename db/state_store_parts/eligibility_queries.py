import hashlib
import json
import math
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStoreEligibilityMixin:
    @staticmethod
    def is_learning_eligible_audit_record(row: Dict[str, Any]) -> bool:
        record = dict(row or {})
        risk_reason = str(record.get("risk_reason") or "").strip().lower()
        if risk_reason.startswith("error in "):
            return False

        execution_summary = record.get("execution_summary") or {}
        if hasattr(execution_summary, "model_dump"):
            execution_summary = execution_summary.model_dump(mode="json")
        if not isinstance(execution_summary, dict):
            execution_summary = {}
        if str(execution_summary.get("error") or "").strip():
            return False

        ai_usage = execution_summary.get("ai_usage") or {}
        if hasattr(ai_usage, "model_dump"):
            ai_usage = ai_usage.model_dump(mode="json")
        if not isinstance(ai_usage, dict):
            ai_usage = {}
        if bool(ai_usage.get("validation_error")) or bool(ai_usage.get("semantic_validation_error")):
            return False

        intent = record.get("intent") or {}
        if hasattr(intent, "model_dump"):
            intent = intent.model_dump(mode="json")
        if not isinstance(intent, dict):
            intent = {}

        text_markers = (
            "system error",
            "structured rationale validation failed",
            "fallback due to json validation error from ai",
            "fallback due to invalid structured rationale from ai",
            "loop error during",
        )
        text_values: List[str] = []
        for note in intent.get("risk_notes") or []:
            text_values.append(str(note or "").strip().lower())
        rationale = intent.get("rationale")
        if rationale is None:
            rationale = record.get("rationale")
        if isinstance(rationale, list):
            text_values.extend(str(item or "").strip().lower() for item in rationale)
        elif rationale:
            text_values.append(str(rationale).strip().lower())
        for text in text_values:
            if any(marker in text for marker in text_markers):
                return False
        return True

    def _is_learning_eligible_audit_id(self, audit_id: Any) -> bool:
        try:
            audit_log_id = int(audit_id or 0)
        except (TypeError, ValueError):
            return False
        if audit_log_id <= 0:
            return True

        cache = getattr(self, "_learning_eligible_audit_cache", None)
        if cache is None:
            cache = {}
            self._learning_eligible_audit_cache = cache
        if audit_log_id in cache:
            return bool(cache[audit_log_id])

        cursor = self.conn.execute(
            """
            SELECT id, risk_reason, intent, execution_summary
            FROM audit_logs
            WHERE id = ?
            LIMIT 1
            """,
            (audit_log_id,),
        )
        row = cursor.fetchone()
        if row is None:
            # Missing audit rows are treated as unknown rather than invalid.
            cache[audit_log_id] = True
            return True

        item = dict(row)
        item["intent"] = self._safe_json_loads(item.get("intent"), default={})
        item["execution_summary"] = self._safe_json_loads(item.get("execution_summary"), default={})
        eligible = self.is_learning_eligible_audit_record(item)
        cache[audit_log_id] = eligible
        return eligible

    def is_prompt_eligible_trade_case_record(self, row: Dict[str, Any]) -> bool:
        record = dict(row or {})
        for key in ("entry_audit_log_id", "exit_audit_log_id"):
            audit_id = record.get(key)
            if audit_id in (None, "", 0, "0"):
                continue
            if not self._is_learning_eligible_audit_id(audit_id):
                return False
        return True
