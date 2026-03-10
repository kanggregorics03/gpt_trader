import json
from datetime import datetime
from typing import Any, Dict, List

from loguru import logger


class StateStoreExecutionEvaluationRuntimeMixin:
    def save_execution_policy_evaluations(self, audit_log_id: int, entries: List[Dict[str, Any]]):
        if not audit_log_id or not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO execution_policy_evaluations (
                        audit_log_id,
                        symbol,
                        loop_mode,
                        risk_mode,
                        action_type,
                        entry_style,
                        urgency_bucket,
                        liquidity_bucket,
                        decision,
                        order_action,
                        order_type,
                        execution_profile,
                        reduce_only,
                        guarded_attention,
                        safety_mode,
                        spread_bps,
                        spread_percentile_1h,
                        depth_percentile_1h,
                        reference_price,
                        order_price,
                        average_fill_price,
                        requested_quantity,
                        filled_quantity,
                        fill_ratio,
                        slippage_bps,
                        latency_seconds,
                        status,
                        outcome_trade_case_id,
                        outcome_estimated_pnl_pct,
                        outcome_realized_pnl,
                        outcome_label,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_log_id,
                            entry.get("symbol"),
                            entry.get("loop_mode"),
                            entry.get("risk_mode"),
                            entry.get("action_type"),
                            entry.get("entry_style"),
                            entry.get("urgency_bucket"),
                            entry.get("liquidity_bucket"),
                            entry.get("decision"),
                            entry.get("order_action"),
                            entry.get("order_type"),
                            entry.get("execution_profile"),
                            int(bool(entry.get("reduce_only"))),
                            int(bool(entry.get("guarded_attention"))),
                            int(bool(entry.get("safety_mode"))),
                            entry.get("spread_bps"),
                            entry.get("spread_percentile_1h"),
                            entry.get("depth_percentile_1h"),
                            entry.get("reference_price"),
                            entry.get("order_price"),
                            entry.get("average_fill_price"),
                            entry.get("requested_quantity"),
                            entry.get("filled_quantity"),
                            entry.get("fill_ratio"),
                            entry.get("slippage_bps"),
                            entry.get("latency_seconds"),
                            entry.get("status"),
                            entry.get("outcome_trade_case_id"),
                            entry.get("outcome_estimated_pnl_pct"),
                            entry.get("outcome_realized_pnl"),
                            entry.get("outcome_label"),
                            json.dumps(entry.get("payload") or {}),
                        )
                        for entry in entries
                        if entry.get("symbol")
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save execution policy evaluations: {e}")

    def get_execution_policy_evaluations_since(self, since: datetime) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM execution_policy_evaluations
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
