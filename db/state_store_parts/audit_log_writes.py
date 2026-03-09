import json
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStoreAuditLogWriteMixin:
    def save_audit(
        self,
        account: dict,
        market: dict,
        intent: dict,
        risk_ok: bool,
        reason: str,
        receipts: list,
        policy_snapshot: Optional[str] = None,
        decision_input_hash: Optional[str] = None,
        risk_mode: Optional[str] = None,
        risk_profile: Optional[dict] = None,
        decision_request: Optional[dict] = None,
        ai_raw_response: Optional[str] = None,
        execution_summary: Optional[dict] = None,
        market_vectors: Optional[dict] = None,
    ) -> Optional[int]:
        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    INSERT INTO audit_logs (
                        account_state,
                        market_state,
                        market_vectors,
                        intent,
                        risk_approved,
                        risk_reason,
                        receipts,
                        policy_snapshot,
                        decision_input_hash,
                        risk_mode,
                        risk_profile,
                        decision_request,
                        ai_raw_response,
                        execution_summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        json.dumps(account),
                        json.dumps(market),
                        json.dumps(market_vectors) if market_vectors is not None else None,
                        json.dumps(intent),
                        risk_ok,
                        reason,
                        json.dumps(receipts),
                        policy_snapshot,
                        decision_input_hash,
                        risk_mode,
                        json.dumps(risk_profile) if risk_profile is not None else None,
                        json.dumps(decision_request) if decision_request is not None else None,
                        ai_raw_response,
                        json.dumps(execution_summary) if execution_summary is not None else None,
                    ),
                )
                return int(cursor.lastrowid)
        except Exception as e:
            logger.error(f"Failed to save audit log: {e}")
            return None

    def save_execution_fills(self, audit_log_id: int, fills: List[Dict[str, Any]]):
        if not fills:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO audit_fills (
                        audit_log_id,
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_log_id,
                            fill.get("timestamp"),
                            fill.get("symbol"),
                            fill.get("order_id"),
                            fill.get("client_order_id"),
                            fill.get("trade_id"),
                            fill.get("side"),
                            fill.get("price"),
                            fill.get("amount"),
                            fill.get("cost"),
                            fill.get("fee_cost"),
                            fill.get("fee_currency"),
                            fill.get("realized_pnl"),
                            json.dumps(fill),
                        )
                        for fill in fills
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save execution fills: {e}")

    def save_position_ledger_entries(self, audit_log_id: int, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO position_ledger (
                        audit_log_id,
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_log_id,
                            entry.get("symbol"),
                            entry.get("decision"),
                            entry.get("fill_count", 0),
                            entry.get("total_amount", 0.0),
                            entry.get("gross_realized_pnl", 0.0),
                            entry.get("fee_cost", 0.0),
                            entry.get("net_realized_pnl", 0.0),
                            entry.get("balance_delta"),
                            entry.get("balance_delta_pct"),
                            json.dumps(entry),
                        )
                        for entry in entries
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save position ledger entries: {e}")
