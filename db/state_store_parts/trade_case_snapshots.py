import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStoreTradeCaseSnapshotMixin:
    def save_trade_case_snapshots(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO trade_case_snapshots (
                        trade_case_id,
                        timestamp,
                        audit_log_id,
                        symbol,
                        current_price,
                        mark_price,
                        mid_price,
                        spread_bps,
                        volume_ratio_15m,
                        unrealized_pnl_pct,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            entry.get("trade_case_id"),
                            entry.get("timestamp"),
                            entry.get("audit_log_id"),
                            entry.get("symbol"),
                            entry.get("current_price"),
                            entry.get("mark_price"),
                            entry.get("mid_price"),
                            entry.get("spread_bps"),
                            entry.get("volume_ratio_15m"),
                            entry.get("unrealized_pnl_pct"),
                            json.dumps(entry.get("payload") or {}),
                        )
                        for entry in entries
                        if entry.get("trade_case_id") and entry.get("symbol")
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save trade case snapshots: {e}")

    def save_market_snapshot_history(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO market_snapshot_history (
                        timestamp,
                        symbol,
                        current_price,
                        mark_price,
                        mid_price,
                        spread_bps,
                        spread_percentile_1h,
                        depth_percentile_1h,
                        depth_notional_top5,
                        spread_vs_1h_avg,
                        volume_ratio_15m,
                        distance_to_invalid_level,
                        trigger_active,
                        risk_trigger_active,
                        volume_surge_trigger_active,
                        fast_move_trigger_active,
                        range_break_trigger_active,
                        trend_1h,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            entry.get("timestamp"),
                            entry.get("symbol"),
                            entry.get("current_price"),
                            entry.get("mark_price"),
                            entry.get("mid_price"),
                            entry.get("spread_bps"),
                            entry.get("spread_percentile_1h"),
                            entry.get("depth_percentile_1h"),
                            entry.get("depth_notional_top5"),
                            entry.get("spread_vs_1h_avg"),
                            entry.get("volume_ratio_15m"),
                            entry.get("distance_to_invalid_level"),
                            int(bool(entry.get("trigger_active"))),
                            int(bool(entry.get("risk_trigger_active"))),
                            int(bool(entry.get("volume_surge_trigger_active"))),
                            int(bool(entry.get("fast_move_trigger_active"))),
                            int(bool(entry.get("range_break_trigger_active"))),
                            entry.get("trend_1h"),
                            json.dumps(entry.get("payload") or {}),
                        )
                        for entry in entries
                        if entry.get("symbol")
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save market snapshot history: {e}")

    def get_market_snapshot_history_between(
        self,
        symbol: str,
        start_timestamp: datetime,
        end_timestamp: datetime,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT
                id,
                timestamp,
                symbol,
                current_price,
                mark_price,
                mid_price,
                spread_bps,
                spread_percentile_1h,
                depth_percentile_1h,
                depth_notional_top5,
                spread_vs_1h_avg,
                volume_ratio_15m,
                distance_to_invalid_level,
                trigger_active,
                risk_trigger_active,
                volume_surge_trigger_active,
                fast_move_trigger_active,
                range_break_trigger_active,
                trend_1h,
                payload
            FROM market_snapshot_history
            WHERE symbol = ?
              AND timestamp >= ?
              AND timestamp <= ?
            ORDER BY timestamp ASC, id ASC
        """
        params: List[Any] = [
            str(symbol or "").strip().upper(),
            start_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            end_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        ]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_trade_case_snapshots(self, trade_case_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        query = """
            SELECT
                id,
                trade_case_id,
                timestamp,
                audit_log_id,
                symbol,
                current_price,
                mark_price,
                mid_price,
                spread_bps,
                volume_ratio_15m,
                unrealized_pnl_pct,
                payload
            FROM trade_case_snapshots
            WHERE trade_case_id = ?
            ORDER BY timestamp ASC, id ASC
        """
        params: List[Any] = [trade_case_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_trade_case_snapshots_between(
        self,
        trade_case_id: str,
        start_timestamp: Optional[str] = None,
        end_timestamp: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT
                id,
                trade_case_id,
                timestamp,
                audit_log_id,
                symbol,
                current_price,
                mark_price,
                mid_price,
                spread_bps,
                volume_ratio_15m,
                unrealized_pnl_pct,
                payload
            FROM trade_case_snapshots
            WHERE trade_case_id = ?
        """
        params: List[Any] = [trade_case_id]
        if start_timestamp:
            query += " AND timestamp >= ?"
            params.append(start_timestamp)
        if end_timestamp:
            query += " AND timestamp <= ?"
            params.append(end_timestamp)
        query += " ORDER BY timestamp ASC, id ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows
