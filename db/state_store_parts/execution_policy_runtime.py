import json
from datetime import datetime
from typing import Any, Dict, List

from loguru import logger


class StateStoreExecutionPolicyRuntimeMixin:
    def _build_execution_policy_context_key(
        self,
        symbol: str,
        loop_mode: str,
        action_type: str,
        urgency_bucket: str,
        liquidity_bucket: str,
        risk_mode: str,
    ) -> str:
        return "|".join(
            [
                str(symbol or "*").strip() or "*",
                str(loop_mode or "").strip() or "unknown",
                str(action_type or "").strip() or "unknown",
                str(urgency_bucket or "").strip() or "unknown",
                str(liquidity_bucket or "").strip() or "unknown",
                str(risk_mode or "").strip() or "unknown",
            ]
        )

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
                        urgency_bucket,
                        liquidity_bucket,
                        decision,
                        order_action,
                        order_type,
                        execution_profile,
                        reduce_only,
                        trigger_mode,
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
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_log_id,
                            entry.get("symbol"),
                            entry.get("loop_mode"),
                            entry.get("risk_mode"),
                            entry.get("action_type"),
                            entry.get("urgency_bucket"),
                            entry.get("liquidity_bucket"),
                            entry.get("decision"),
                            entry.get("order_action"),
                            entry.get("order_type"),
                            entry.get("execution_profile"),
                            int(bool(entry.get("reduce_only"))),
                            int(bool(entry.get("trigger_mode"))),
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

    def upsert_execution_policy_profile(self, record: Dict[str, Any]):
        symbol = str(record.get("symbol") or "*").strip() or "*"
        loop_mode = str(record.get("loop_mode") or "unknown").strip() or "unknown"
        action_type = str(record.get("action_type") or "unknown").strip() or "unknown"
        urgency_bucket = str(record.get("urgency_bucket") or "unknown").strip() or "unknown"
        liquidity_bucket = str(record.get("liquidity_bucket") or "unknown").strip() or "unknown"
        risk_mode = str(record.get("risk_mode") or "unknown").strip() or "unknown"
        context_key = self._build_execution_policy_context_key(
            symbol=symbol,
            loop_mode=loop_mode,
            action_type=action_type,
            urgency_bucket=urgency_bucket,
            liquidity_bucket=liquidity_bucket,
            risk_mode=risk_mode,
        )
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO execution_policy_profiles (
                        context_key,
                        symbol,
                        loop_mode,
                        risk_mode,
                        action_type,
                        urgency_bucket,
                        liquidity_bucket,
                        recommended_order_type,
                        recommended_execution_profile,
                        metrics,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(context_key) DO UPDATE SET
                        symbol = excluded.symbol,
                        loop_mode = excluded.loop_mode,
                        risk_mode = excluded.risk_mode,
                        action_type = excluded.action_type,
                        urgency_bucket = excluded.urgency_bucket,
                        liquidity_bucket = excluded.liquidity_bucket,
                        recommended_order_type = excluded.recommended_order_type,
                        recommended_execution_profile = excluded.recommended_execution_profile,
                        metrics = excluded.metrics,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        context_key,
                        symbol,
                        loop_mode,
                        risk_mode,
                        action_type,
                        urgency_bucket,
                        liquidity_bucket,
                        record.get("recommended_order_type"),
                        record.get("recommended_execution_profile"),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to upsert execution policy profile: {e}")

    def save_execution_policy_profiles(self, entries: List[Dict[str, Any]]):
        for entry in entries or []:
            self.upsert_execution_policy_profile(entry)

    def get_execution_policy_profile(
        self,
        symbol: str,
        loop_mode: str,
        action_type: str,
        urgency_bucket: str,
        liquidity_bucket: str,
        risk_mode: str,
    ) -> Dict[str, Any]:
        context_key = self._build_execution_policy_context_key(
            symbol=symbol,
            loop_mode=loop_mode,
            action_type=action_type,
            urgency_bucket=urgency_bucket,
            liquidity_bucket=liquidity_bucket,
            risk_mode=risk_mode,
        )
        cursor = self.conn.execute(
            """
            SELECT *
            FROM execution_policy_profiles
            WHERE context_key = ?
            LIMIT 1
            """,
            (context_key,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = dict(row)
        item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
        return item

    def get_recent_execution_policy_profiles(self, limit: int = 50) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM execution_policy_profiles
            ORDER BY updated_at DESC, context_key DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows
