from datetime import datetime
from typing import Any, Dict, List, Optional


class StateStoreAuditLogQueryMixin:
    def get_recent_audits(self, limit: int = 20, since: Optional[datetime] = None) -> List[Dict[str, Any]]:
        if since is not None:
            since_value = since.strftime("%Y-%m-%d %H:%M:%S")
            cursor = self.conn.execute(
                """
                SELECT
                    id,
                    timestamp,
                    intent,
                    risk_approved,
                    risk_reason,
                    decision_input_hash,
                    risk_mode,
                    execution_summary,
                    market_vectors
                FROM audit_logs
                WHERE timestamp >= ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (since_value, limit),
            )
        else:
            cursor = self.conn.execute(
                """
                SELECT
                    id,
                    timestamp,
                    intent,
                    risk_approved,
                    risk_reason,
                    decision_input_hash,
                    risk_mode,
                    execution_summary,
                    market_vectors
                FROM audit_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["intent"] = self._safe_json_loads(item.get("intent"), default={})
            item["execution_summary"] = self._safe_json_loads(item.get("execution_summary"), default={})
            item["market_vectors"] = self._safe_json_loads(item.get("market_vectors"), default={})
            rows.append(item)
        return rows

    def get_audit_log(self, audit_id: int) -> Optional[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM audit_logs
            WHERE id = ?
            LIMIT 1
            """,
            (audit_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        decoded = self._decode_audit_row(dict(row))
        decoded["fills"] = self.get_execution_fills(audit_id)
        decoded["ledger_entries"] = self.get_position_ledger_for_audit(audit_id)
        decoded["replay_runs"] = self.get_replay_runs_for_audit(audit_id, limit=10)
        decoded["order_state_transitions"] = self.get_order_state_transitions_for_audit(audit_id)
        decoded["position_state_transitions"] = self.get_position_state_transitions_for_audit(audit_id)
        decoded["symbol_journal_entries"] = self.get_symbol_journal_for_audit(audit_id)
        decoded["trade_cases"] = self.get_trade_cases_for_audit(audit_id)
        decoded["retrieval_evaluations"] = self.get_retrieval_evaluations_for_audit(audit_id, limit=25)
        return decoded

    def get_symbol_journal_for_audit(self, audit_log_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                id,
                timestamp,
                audit_log_id,
                symbol,
                decision,
                risk_reason,
                risk_mode,
                risk_approved,
                triggered,
                risk_triggered,
                net_realized_pnl,
                fee_cost,
                estimated_pnl_pct,
                trade_case_id,
                case_status,
                case_opened_at,
                case_closed_at,
                holding_minutes,
                evidence_signals,
                summary,
                payload
            FROM symbol_journal
            WHERE audit_log_id = ?
            ORDER BY id ASC
            """,
            (audit_log_id,),
        )
        return [self._decode_symbol_journal_row(dict(row)) for row in cursor.fetchall()]

    def get_trade_cases_for_audit(self, audit_log_id: int) -> List[Dict[str, Any]]:
        rows = self.get_symbol_journal_for_audit(audit_log_id)
        ordered_ids: List[str] = []
        seen: set[str] = set()
        for row in rows:
            trade_case_id = str(row.get("trade_case_id") or "").strip()
            if not trade_case_id or trade_case_id in seen:
                continue
            seen.add(trade_case_id)
            ordered_ids.append(trade_case_id)
        cases: List[Dict[str, Any]] = []
        for trade_case_id in ordered_ids:
            trade_case = self.get_trade_case(trade_case_id)
            if trade_case:
                cases.append(trade_case)
        return cases

    def get_retrieval_evaluations_for_audit(
        self,
        audit_log_id: int,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
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
            WHERE audit_log_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (audit_log_id, limit),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows
