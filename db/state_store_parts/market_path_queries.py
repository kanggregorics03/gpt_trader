from datetime import datetime
from typing import Any, Dict, List, Optional


class StateStoreMarketPathQueryMixin:
    def get_market_path_between(
        self,
        symbol: str,
        start_timestamp: datetime,
        end_timestamp: datetime,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        history_rows = self.get_market_snapshot_history_between(
            symbol,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            limit=limit,
        )
        merged_rows: Dict[str, Dict[str, Any]] = {}
        for item in history_rows:
            timestamp_key = str(item.get("timestamp") or "")
            merged_rows[timestamp_key] = {
                "id": item.get("id"),
                "timestamp": item.get("timestamp"),
                "symbol": item.get("symbol"),
                "current_price": item.get("current_price"),
                "mark_price": item.get("mark_price"),
                "mid_price": item.get("mid_price"),
                "spread_bps": item.get("spread_bps"),
                "spread_percentile_1h": item.get("spread_percentile_1h"),
                "depth_percentile_1h": item.get("depth_percentile_1h"),
                "depth_notional_top5": item.get("depth_notional_top5"),
                "spread_vs_1h_avg": item.get("spread_vs_1h_avg"),
                "volume_ratio_15m": item.get("volume_ratio_15m"),
                "distance_to_invalid_level": item.get("distance_to_invalid_level"),
                "trigger_active": bool(item.get("trigger_active")),
                "risk_trigger_active": bool(item.get("risk_trigger_active")),
                "volume_surge_trigger_active": bool(item.get("volume_surge_trigger_active")),
                "fast_move_trigger_active": bool(item.get("fast_move_trigger_active")),
                "range_break_trigger_active": bool(item.get("range_break_trigger_active")),
                "trend_1h": item.get("trend_1h"),
                "snapshot": item.get("payload") or {},
                "_source": "market_snapshot_history",
            }
        query = """
            SELECT
                id,
                timestamp,
                market_state
            FROM audit_logs
            WHERE timestamp >= ?
              AND timestamp <= ?
              AND market_state IS NOT NULL
            ORDER BY timestamp ASC, id ASC
        """
        params: List[Any] = [
            start_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            end_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        ]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        target_symbol = str(symbol or "").strip().upper()
        for row in cursor.fetchall():
            item = dict(row)
            market_state = self._safe_json_loads(item.get("market_state"), default={})
            snapshot = ((market_state.get("market_snapshots") or {}).get(target_symbol) or {})
            if not snapshot:
                continue
            order_book = snapshot.get("order_book") or {}
            timestamp_key = str(item.get("timestamp") or "")
            if timestamp_key in merged_rows:
                continue
            merged_rows[timestamp_key] = {
                "id": item.get("id"),
                "timestamp": item.get("timestamp"),
                "symbol": target_symbol,
                "current_price": snapshot.get("current_price"),
                "mark_price": snapshot.get("mark_price"),
                "mid_price": order_book.get("mid_price"),
                "spread_bps": order_book.get("spread_bps"),
                "spread_percentile_1h": snapshot.get("spread_percentile_1h"),
                "depth_percentile_1h": snapshot.get("depth_percentile_1h"),
                "depth_notional_top5": snapshot.get("depth_notional_top5"),
                "spread_vs_1h_avg": snapshot.get("spread_vs_1h_avg"),
                "volume_ratio_15m": snapshot.get("volume_ratio_15m"),
                "distance_to_invalid_level": snapshot.get("distance_to_invalid_level"),
                "trigger_active": bool(snapshot.get("is_opportunity_trigger")),
                "risk_trigger_active": bool(snapshot.get("is_risk_trigger")),
                "volume_surge_trigger_active": bool(snapshot.get("volume_surge_trigger_active")),
                "fast_move_trigger_active": bool(snapshot.get("fast_move_trigger_active")),
                "range_break_trigger_active": bool(snapshot.get("range_break_trigger_active")),
                "trend_1h": snapshot.get("trend_1h"),
                "snapshot": snapshot,
                "_source": "audit_market_state",
            }
        rows = [item for _, item in sorted(merged_rows.items(), key=lambda pair: pair[0])]
        if limit is not None:
            return rows[:limit]
        return rows
