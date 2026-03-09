import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStoreRetrievalJournalMixin:
    def save_retrieval_evaluations(self, audit_log_id: int, entries: List[Dict[str, Any]]):
        if not audit_log_id or not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO retrieval_evaluations (
                        audit_log_id,
                        context_type,
                        rank_index,
                        trade_case_id,
                        case_symbol,
                        matched_symbol,
                        decision,
                        decision_symbol_aligned,
                        estimated_pnl_pct,
                        similarity,
                        final_score,
                        retrieval_source,
                        matched_band_count,
                        risk_mode,
                        outcome_trade_case_id,
                        outcome_status,
                        outcome_realized_pnl,
                        outcome_estimated_pnl_pct,
                        outcome_holding_minutes,
                        outcome_observed_mfe_pct,
                        outcome_observed_mae_pct,
                        outcome_slippage_bps,
                        outcome_updated_at,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_log_id,
                            entry.get("context_type"),
                            entry.get("rank_index"),
                            entry.get("trade_case_id"),
                            entry.get("case_symbol"),
                            entry.get("matched_symbol"),
                            entry.get("decision"),
                            int(bool(entry.get("decision_symbol_aligned"))),
                            entry.get("estimated_pnl_pct"),
                            entry.get("similarity"),
                            entry.get("final_score"),
                            entry.get("retrieval_source"),
                            int(entry.get("matched_band_count") or 0),
                            entry.get("risk_mode"),
                            entry.get("outcome_trade_case_id"),
                            entry.get("outcome_status"),
                            entry.get("outcome_realized_pnl"),
                            entry.get("outcome_estimated_pnl_pct"),
                            entry.get("outcome_holding_minutes"),
                            entry.get("outcome_observed_mfe_pct"),
                            entry.get("outcome_observed_mae_pct"),
                            entry.get("outcome_slippage_bps"),
                            entry.get("outcome_updated_at"),
                            json.dumps(entry.get("payload") or {}),
                        )
                        for entry in entries
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save retrieval evaluations: {e}")

    def update_retrieval_evaluations_for_trade_case(
        self,
        trade_case_id: str,
        case_symbol: str,
        entry_audit_log_id: Optional[int] = None,
        outcome_status: Optional[str] = None,
        outcome_realized_pnl: Optional[float] = None,
        outcome_estimated_pnl_pct: Optional[float] = None,
        outcome_holding_minutes: Optional[float] = None,
        outcome_observed_mfe_pct: Optional[float] = None,
        outcome_observed_mae_pct: Optional[float] = None,
        outcome_slippage_bps: Optional[float] = None,
        outcome_updated_at: Optional[str] = None,
    ) -> None:
        if not trade_case_id or not case_symbol:
            return
        try:
            with self.conn:
                params: List[Any] = [
                    trade_case_id,
                    outcome_status,
                    outcome_realized_pnl,
                    outcome_estimated_pnl_pct,
                    outcome_holding_minutes,
                    outcome_observed_mfe_pct,
                    outcome_observed_mae_pct,
                    outcome_slippage_bps,
                    outcome_updated_at,
                    trade_case_id,
                ]
                query = """
                    UPDATE retrieval_evaluations
                    SET
                        outcome_trade_case_id = ?,
                        outcome_status = COALESCE(?, outcome_status),
                        outcome_realized_pnl = COALESCE(?, outcome_realized_pnl),
                        outcome_estimated_pnl_pct = COALESCE(?, outcome_estimated_pnl_pct),
                        outcome_holding_minutes = COALESCE(?, outcome_holding_minutes),
                        outcome_observed_mfe_pct = COALESCE(?, outcome_observed_mfe_pct),
                        outcome_observed_mae_pct = COALESCE(?, outcome_observed_mae_pct),
                        outcome_slippage_bps = COALESCE(?, outcome_slippage_bps),
                        outcome_updated_at = COALESCE(?, outcome_updated_at)
                    WHERE outcome_trade_case_id = ?
                """
                if entry_audit_log_id is not None:
                    query += " OR (audit_log_id = ? AND case_symbol = ?)"
                    params.extend([entry_audit_log_id, case_symbol])
                self.conn.execute(query, params)
        except Exception as e:
            logger.error(f"Failed to update retrieval evaluations for trade case: {e}")

    def get_retrieval_evaluations_since(
        self,
        since: datetime,
        context_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT
                id,
                audit_log_id,
                created_at,
                context_type,
                rank_index,
                trade_case_id,
                case_symbol,
                matched_symbol,
                decision,
                decision_symbol_aligned,
                estimated_pnl_pct,
                similarity,
                final_score,
                retrieval_source,
                matched_band_count,
                risk_mode,
                outcome_trade_case_id,
                outcome_status,
                outcome_realized_pnl,
                outcome_estimated_pnl_pct,
                outcome_holding_minutes,
                outcome_observed_mfe_pct,
                outcome_observed_mae_pct,
                outcome_slippage_bps,
                outcome_updated_at,
                payload
            FROM retrieval_evaluations
            WHERE created_at >= ?
            ORDER BY created_at ASC, id ASC
        """
        params: List[Any] = [since.strftime("%Y-%m-%d %H:%M:%S")]
        if context_type:
            query = query.replace("WHERE created_at >= ?", "WHERE created_at >= ? AND context_type = ?")
            params.append(context_type)
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

    def get_replay_runs_since(
        self,
        since: datetime,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT
                id,
                audit_log_id,
                created_at,
                original_decision,
                replayed_decision,
                decision_changed,
                order_count_changed,
                side_changed,
                order_type_changed,
                execution_profile_changed,
                size_delta,
                confidence_delta,
                simulated_fill_ratio,
                simulated_slippage_bps,
                simulated_latency_seconds,
                rationale_keywords_added,
                rationale_keywords_removed,
                evidence_signals_added,
                evidence_signals_removed,
                evidence_signal_count_delta,
                payload
            FROM replay_runs
            WHERE created_at >= ?
            ORDER BY created_at ASC, id ASC
        """
        params: List[Any] = [since.strftime("%Y-%m-%d %H:%M:%S")]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["rationale_keywords_added"] = self._safe_json_loads(item.get("rationale_keywords_added"), default=[])
            item["rationale_keywords_removed"] = self._safe_json_loads(item.get("rationale_keywords_removed"), default=[])
            item["evidence_signals_added"] = self._safe_json_loads(item.get("evidence_signals_added"), default=[])
            item["evidence_signals_removed"] = self._safe_json_loads(item.get("evidence_signals_removed"), default=[])
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows
