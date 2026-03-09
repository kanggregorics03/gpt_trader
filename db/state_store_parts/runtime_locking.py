import time
from datetime import timedelta
from typing import Any, Dict, List

from loguru import logger


class StateStoreRuntimeLockingMixin:
    def try_acquire_runtime_lock(self, lock_name: str, owner: str, ttl_seconds: int = 240) -> bool:
        now_epoch = time.time()
        expires_epoch = now_epoch + max(1, int(ttl_seconds or 1))
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO runtime_locks (
                        lock_name,
                        owner,
                        acquired_at_epoch,
                        expires_at_epoch
                    ) VALUES (?, '', 0, 0)
                    """,
                    (lock_name,),
                )
                updated = self.conn.execute(
                    """
                    UPDATE runtime_locks
                    SET owner = ?, acquired_at_epoch = ?, expires_at_epoch = ?
                    WHERE lock_name = ?
                      AND (expires_at_epoch <= ? OR owner = ?)
                    """,
                    (
                        owner,
                        now_epoch,
                        expires_epoch,
                        lock_name,
                        now_epoch,
                        owner,
                    ),
                )
                return updated.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to acquire runtime lock {lock_name}: {e}")
            return False

    def release_runtime_lock(self, lock_name: str, owner: str):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE runtime_locks
                    SET owner = '', acquired_at_epoch = 0, expires_at_epoch = 0
                    WHERE lock_name = ? AND owner = ?
                    """,
                    (lock_name, owner),
                )
        except Exception as e:
            logger.error(f"Failed to release runtime lock {lock_name}: {e}")

    def acquire_entry_mutex(self, symbol: str, owner: str, ttl_seconds: int = 180) -> bool:
        now_epoch = time.time()
        expires_epoch = now_epoch + max(1, int(ttl_seconds or 1))
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO entry_mutex (
                        symbol,
                        owner,
                        acquired_at_epoch,
                        expires_at_epoch
                    ) VALUES (?, '', 0, 0)
                    """,
                    (symbol,),
                )
                updated = self.conn.execute(
                    """
                    UPDATE entry_mutex
                    SET owner = ?, acquired_at_epoch = ?, expires_at_epoch = ?
                    WHERE symbol = ?
                      AND (expires_at_epoch <= ? OR owner = ?)
                    """,
                    (
                        owner,
                        now_epoch,
                        expires_epoch,
                        symbol,
                        now_epoch,
                        owner,
                    ),
                )
                return updated.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to acquire entry mutex {symbol}: {e}")
            return False

    def release_entry_mutex(self, symbol: str, owner: str):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE entry_mutex
                    SET owner = '', acquired_at_epoch = 0, expires_at_epoch = 0
                    WHERE symbol = ? AND owner = ?
                    """,
                    (symbol, owner),
                )
        except Exception as e:
            logger.error(f"Failed to release entry mutex {symbol}: {e}")

    def save_decision_fingerprints(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO decision_fingerprints (
                        symbol,
                        action,
                        size_band,
                        fingerprint,
                        audit_log_id,
                        status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            str(entry.get("symbol") or "").upper(),
                            str(entry.get("action") or "").upper(),
                            str(entry.get("size_band") or "unknown"),
                            str(entry.get("fingerprint") or ""),
                            entry.get("audit_log_id"),
                            entry.get("status") or "planned",
                        )
                        for entry in entries
                        if entry.get("symbol") and entry.get("action") and entry.get("fingerprint")
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save decision fingerprints: {e}")

    def has_recent_decision_fingerprint(
        self,
        symbol: str,
        action: str,
        size_band: str,
        within_minutes: int = 10,
    ) -> bool:
        minutes = max(1, int(within_minutes or 1))
        since = (self._utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute(
            """
            SELECT 1
            FROM decision_fingerprints
            WHERE symbol = ?
              AND action = ?
              AND size_band = ?
              AND created_at >= ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                str(symbol or "").upper(),
                str(action or "").upper(),
                str(size_band or "unknown"),
                since,
            ),
        ).fetchone()
        return row is not None

    def count_recent_unknown_orders(self, minutes: int = 30) -> int:
        lookback_minutes = max(1, int(minutes or 1))
        since = (self._utcnow() - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM order_runtime_state
            WHERE state = 'unknown'
              AND updated_at >= ?
            """,
            (since,),
        ).fetchone()
        if row is None:
            return 0
        return int(row["count"] or 0)

    def get_unknown_order_runtime_states(self, limit: int = 50) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                order_key,
                client_order_id,
                exchange_order_id,
                symbol,
                state,
                updated_at,
                last_audit_log_id,
                payload
            FROM order_runtime_state
            WHERE state = 'unknown'
            ORDER BY updated_at DESC
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
