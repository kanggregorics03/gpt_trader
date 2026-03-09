import json
from typing import Any, Dict, Optional

from loguru import logger


class StateStoreDecisionStateMixin:
    def get_symbol_trigger_state(self, symbol: str) -> Dict[str, Any]:
        cursor = self.conn.execute(
            """
            SELECT payload
            FROM trigger_state
            WHERE symbol = ?
            """,
            (symbol,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        return self._safe_json_loads(row["payload"], default={})

    def save_symbol_trigger_state(self, symbol: str, state: Dict[str, Any]):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO trigger_state (symbol, updated_at, payload)
                    VALUES (?, CURRENT_TIMESTAMP, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        updated_at = CURRENT_TIMESTAMP,
                        payload = excluded.payload
                    """,
                    (
                        symbol,
                        json.dumps(state or {}),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to save trigger state for {symbol}: {e}")

    def get_cached_decision(self, prompt_hash: str) -> Optional[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                prompt_hash,
                model_name,
                input_hash,
                raw_response,
                intent,
                created_at,
                last_used_at,
                hit_count
            FROM decision_cache
            WHERE prompt_hash = ?
            """,
            (prompt_hash,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        with self.conn:
            self.conn.execute(
                """
                UPDATE decision_cache
                SET
                    last_used_at = CURRENT_TIMESTAMP,
                    hit_count = hit_count + 1
                WHERE prompt_hash = ?
                """,
                (prompt_hash,),
            )

        item = dict(row)
        item["intent"] = self._safe_json_loads(item.get("intent"), default={})
        item["hit_count"] = int(item.get("hit_count") or 0) + 1
        return item

    def save_cached_decision(
        self,
        prompt_hash: str,
        model_name: str,
        input_hash: str,
        raw_response: str,
        intent: Dict[str, Any],
    ):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO decision_cache (
                        prompt_hash,
                        model_name,
                        input_hash,
                        raw_response,
                        intent,
                        created_at,
                        last_used_at,
                        hit_count
                    ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
                    ON CONFLICT(prompt_hash) DO UPDATE SET
                        model_name = excluded.model_name,
                        input_hash = excluded.input_hash,
                        raw_response = excluded.raw_response,
                        intent = excluded.intent,
                        last_used_at = CURRENT_TIMESTAMP
                    """,
                    (
                        prompt_hash,
                        model_name,
                        input_hash,
                        raw_response,
                        json.dumps(intent),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to save cached decision for {prompt_hash}: {e}")
