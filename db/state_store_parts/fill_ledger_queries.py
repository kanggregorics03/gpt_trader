from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


class StateStoreFillLedgerQueryMixin:
    def get_execution_fills(self, audit_log_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                id,
                fill_timestamp,
                symbol,
                order_id,
                client_order_id,
                trade_id,
                side,
                price,
                amount,
                cost,
                fee_cost,
                fee_currency,
                realized_pnl,
                payload
            FROM audit_fills
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

    def get_recent_fills(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                fill_timestamp,
                symbol,
                side,
                price,
                amount,
                fee_cost,
                fee_currency,
                realized_pnl
            FROM audit_fills
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_recent_position_ledger(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                decision,
                fill_count,
                total_amount,
                gross_realized_pnl,
                fee_cost,
                net_realized_pnl,
                balance_delta,
                balance_delta_pct
            FROM position_ledger
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_position_ledger_for_audit(self, audit_log_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                decision,
                fill_count,
                total_amount,
                gross_realized_pnl,
                fee_cost,
                net_realized_pnl,
                balance_delta,
                balance_delta_pct,
                payload
            FROM position_ledger
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

    def get_fill_stats(self, hours: int = 24, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._utcnow()
        since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS fill_count,
                COALESCE(SUM(fee_cost), 0) AS fee_cost,
                COALESCE(SUM(realized_pnl), 0) AS realized_pnl
            FROM audit_fills
            WHERE created_at >= ?
            """,
            (since,),
        ).fetchone()
        stats = dict(row or {})
        return {
            "fill_count": int(stats.get("fill_count") or 0),
            "fee_cost": round(float(stats.get("fee_cost") or 0.0), 8),
            "realized_pnl": round(float(stats.get("realized_pnl") or 0.0), 8),
        }

    def get_position_ledger_stats(self, hours: int = 24, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._utcnow()
        since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS row_count,
                COALESCE(SUM(net_realized_pnl), 0) AS net_realized_pnl,
                COALESCE(SUM(gross_realized_pnl), 0) AS gross_realized_pnl,
                COALESCE(SUM(fee_cost), 0) AS fee_cost
            FROM position_ledger
            WHERE created_at >= ?
            """,
            (since,),
        ).fetchone()
        stats = dict(row or {})
        return {
            "row_count": int(stats.get("row_count") or 0),
            "net_realized_pnl": round(float(stats.get("net_realized_pnl") or 0.0), 8),
            "gross_realized_pnl": round(float(stats.get("gross_realized_pnl") or 0.0), 8),
            "fee_cost": round(float(stats.get("fee_cost") or 0.0), 8),
        }

    def get_symbol_performance_report(self, hours: int = 24, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or self._utcnow()
        since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.execute(
            """
            SELECT
                symbol,
                COUNT(*) AS row_count,
                COALESCE(SUM(fill_count), 0) AS fill_count,
                COALESCE(SUM(total_amount), 0) AS total_amount,
                COALESCE(SUM(gross_realized_pnl), 0) AS gross_realized_pnl,
                COALESCE(SUM(fee_cost), 0) AS fee_cost,
                COALESCE(SUM(net_realized_pnl), 0) AS net_realized_pnl,
                AVG(CASE WHEN net_realized_pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate
            FROM position_ledger
            WHERE created_at >= ?
            GROUP BY symbol
            ORDER BY net_realized_pnl DESC, symbol ASC
            """,
            (since,),
        )
        report = []
        for row in cursor.fetchall():
            item = dict(row)
            item["fill_count"] = int(item.get("fill_count") or 0)
            item["row_count"] = int(item.get("row_count") or 0)
            item["total_amount"] = round(float(item.get("total_amount") or 0.0), 8)
            item["gross_realized_pnl"] = round(float(item.get("gross_realized_pnl") or 0.0), 8)
            item["fee_cost"] = round(float(item.get("fee_cost") or 0.0), 8)
            item["net_realized_pnl"] = round(float(item.get("net_realized_pnl") or 0.0), 8)
            item["win_rate"] = round(float(item.get("win_rate") or 0.0) * 100, 2)
            report.append(item)
        return report

    def get_daily_ledger_report(self, days: int = 7, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or self._utcnow()
        since = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.execute(
            """
            SELECT
                substr(created_at, 1, 10) AS day,
                COUNT(*) AS row_count,
                COALESCE(SUM(net_realized_pnl), 0) AS net_realized_pnl,
                COALESCE(SUM(gross_realized_pnl), 0) AS gross_realized_pnl,
                COALESCE(SUM(fee_cost), 0) AS fee_cost
            FROM position_ledger
            WHERE created_at >= ?
            GROUP BY substr(created_at, 1, 10)
            ORDER BY day DESC
            """,
            (since,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["row_count"] = int(item.get("row_count") or 0)
            item["net_realized_pnl"] = round(float(item.get("net_realized_pnl") or 0.0), 8)
            item["gross_realized_pnl"] = round(float(item.get("gross_realized_pnl") or 0.0), 8)
            item["fee_cost"] = round(float(item.get("fee_cost") or 0.0), 8)
            rows.append(item)
        return rows
