from datetime import datetime, timedelta
from typing import Any, Dict, Optional


class StateStoreMonitoringSnapshotQueryMixin:
    def get_monitoring_snapshot(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._utcnow()
        since = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        fill_stats = self.get_fill_stats(hours=24, now=now)

        cursor = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total_audits,
                SUM(CASE WHEN risk_approved = 0 THEN 1 ELSE 0 END) AS rejected_audits,
                SUM(CASE WHEN risk_reason LIKE 'Error in %' THEN 1 ELSE 0 END) AS loop_errors
            FROM audit_logs
            WHERE timestamp >= ?
            """,
            (since,),
        )
        stats = dict(cursor.fetchone() or {})

        unresolved_timeouts = self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM audit_logs
            WHERE timestamp >= ?
              AND receipts LIKE '%timeout_unresolved%'
            """,
            (since,),
        ).fetchone()

        latest_audit_row = self.conn.execute(
            """
            SELECT timestamp, intent, risk_approved, risk_reason, execution_summary
            FROM audit_logs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        latest_memory_row = self.conn.execute(
            """
            SELECT timestamp, rule, metrics
            FROM long_term_memory
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return {
            "total_audits_24h": int(stats.get("total_audits") or 0),
            "rejected_24h": int(stats.get("rejected_audits") or 0),
            "loop_errors_24h": int(stats.get("loop_errors") or 0),
            "timeouts_unresolved_24h": int((unresolved_timeouts or {})["count"] or 0),
            "unknown_order_states_30m": self.count_recent_unknown_orders(minutes=30),
            "fee_cost_24h": fill_stats["fee_cost"],
            "fill_count_24h": fill_stats["fill_count"],
            "realized_pnl_24h": fill_stats["realized_pnl"],
            "ledger_net_pnl_24h": self.get_position_ledger_stats(hours=24, now=now)["net_realized_pnl"],
            "latest_audit": dict(latest_audit_row) if latest_audit_row else None,
            "latest_memory": self._decode_metrics_row(latest_memory_row),
            "recent_logs": self.get_recent_audits(limit=20),
            "semantic_validation_summary": self._build_semantic_validation_summary(now=now),
            "hold_quality_summary_7d": self._build_hold_quality_summary(now=now, days=7),
            "replay_learning_summary_7d": self._build_replay_learning_summary(now=now, days=7),
            "label_quality_summary_7d": self._build_label_quality_summary(now=now, days=7),
            "context_bias_summary": self._build_context_bias_summary(now=now),
            "learning_liveness": self._build_learning_liveness_summary(now=now),
            "recent_memory": self.get_recent_long_term_memory(limit=5),
            "recent_symbol_journal": self.get_recent_symbol_journal(limit=10),
            "recent_fills": self.get_recent_fills(limit=10),
            "recent_ledger": self.get_recent_position_ledger(limit=10),
            "recent_replays": self.get_recent_replay_runs(limit=10),
            "recent_order_states": self.get_recent_order_state_transitions(limit=10),
            "recent_position_states": self.get_recent_position_state_transitions(limit=10),
        }
