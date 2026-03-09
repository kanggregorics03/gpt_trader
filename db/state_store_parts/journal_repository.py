import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStoreJournalRepositoryMixin:
    def save_journal(
        self,
        decision: str,
        rationale: List[str],
        confidence: float,
        audit_log_id: Optional[int] = None,
        rationale_items: Optional[List[Dict[str, Any]]] = None,
        risk_reason: str = "",
        estimated_pnl_pct: Optional[float] = None,
        loss_signal: bool = False,
        trade_case_ids: Optional[List[str]] = None,
        case_statuses: Optional[Dict[str, str]] = None,
        case_summary: str = "",
    ):
        try:
            serialized_rationale_items = self._serialize_json_payload(rationale_items)
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO journal (
                        audit_log_id,
                        decision,
                        rationale,
                        rationale_json,
                        confidence,
                        risk_reason,
                        estimated_pnl_pct,
                        loss_signal,
                        trade_case_ids,
                        case_statuses,
                        case_summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        audit_log_id,
                        decision,
                        "\\n".join(rationale),
                        json.dumps(serialized_rationale_items) if serialized_rationale_items is not None else None,
                        confidence,
                        risk_reason,
                        estimated_pnl_pct,
                        int(loss_signal),
                        json.dumps(trade_case_ids or []),
                        json.dumps(case_statuses or {}),
                        case_summary,
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to save journal log: {e}")

    def _serialize_json_payload(self, payload: Any) -> Any:
        if hasattr(payload, "model_dump"):
            return payload.model_dump(mode="json")
        if isinstance(payload, dict):
            return {str(key): self._serialize_json_payload(value) for key, value in payload.items()}
        if isinstance(payload, (list, tuple)):
            return [self._serialize_json_payload(item) for item in payload]
        return payload

    def get_recent_journals(self, limit: int = 10) -> List[str]:
        try:
            return [
                self._format_journal_summary(item)
                for item in self.get_recent_journal_context(limit=limit)
            ]
        except Exception as e:
            logger.error(f"Failed to fetch journals: {e}")
            return []

    def _build_journal_context_base_query(self, where_clause: str = "", order_clause: str = "") -> str:
        query = """
            SELECT
                j.id,
                j.timestamp,
                j.audit_log_id,
                j.decision,
                j.rationale,
                j.rationale_json,
                j.confidence,
                j.risk_reason,
                j.estimated_pnl_pct,
                j.loss_signal,
                j.trade_case_ids,
                j.case_statuses,
                j.case_summary,
                COALESCE(ledger.net_realized_pnl, 0) AS ledger_net_realized_pnl,
                COALESCE(ledger.fee_cost, 0) AS ledger_fee_cost,
                a.risk_approved,
                a.risk_mode,
                a.market_state,
                a.execution_summary
            FROM journal j
            LEFT JOIN (
                SELECT
                    audit_log_id,
                    COALESCE(SUM(net_realized_pnl), 0) AS net_realized_pnl,
                    COALESCE(SUM(fee_cost), 0) AS fee_cost
                FROM position_ledger
                GROUP BY audit_log_id
            ) ledger ON ledger.audit_log_id = j.audit_log_id
            LEFT JOIN audit_logs a ON a.id = j.audit_log_id
        """
        if where_clause:
            query += f"\n            {where_clause}"
        if order_clause:
            query += f"\n            {order_clause}"
        return query

    def _fetch_journal_context_ids(self, query: str, params: tuple[Any, ...]) -> List[int]:
        cursor = self.conn.execute(query, params)
        ids: List[int] = []
        for row in cursor.fetchall():
            try:
                ids.append(int(row["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return ids

    def _fetch_journal_context_rows_by_ids(self, ids: List[int]) -> List[Dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        query = self._build_journal_context_base_query(
            where_clause=f"WHERE j.id IN ({placeholders})",
            order_clause="ORDER BY j.id DESC",
        )
        rows = []
        cursor = self.conn.execute(query, tuple(ids))
        for row in cursor.fetchall():
            rows.append(self._decode_journal_context_row(dict(row)))
        return rows

    def get_recent_journal_context(
        self,
        limit: int = 10,
        candidate_limit: Optional[int] = None,
        anchor_limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if candidate_limit is None and anchor_limit is None:
            query = self._build_journal_context_base_query(order_clause="ORDER BY j.id DESC\n            LIMIT ?")
            rows = []
            cursor = self.conn.execute(query, (limit,))
            for row in cursor.fetchall():
                rows.append(self._decode_journal_context_row(dict(row)))
            return rows

        recent_limit = max(int(limit or 0), int(candidate_limit or 0), 1)
        semantic_anchor_limit = max(int(anchor_limit or 0), 0)
        recent_ids = self._fetch_journal_context_ids(
            "SELECT id FROM journal ORDER BY id DESC LIMIT ?",
            (recent_limit,),
        )
        anchor_ids: List[int] = []
        if semantic_anchor_limit > 0:
            anchor_ids = self._fetch_journal_context_ids(
                """
                SELECT id
                FROM journal
                ORDER BY
                    CASE
                        WHEN COALESCE(rationale_json, '') LIKE '%good_hold%' THEN 6
                        WHEN COALESCE(rationale_json, '') LIKE '%missed_opportunity_hold%' THEN 6
                        WHEN COALESCE(rationale_json, '') LIKE '%probe_only_opportunity_hold%' THEN 5
                        WHEN COALESCE(loss_signal, 0) = 1 THEN 5
                        WHEN decision IN ('EXIT', 'REDUCE', 'CANCEL', 'REPLACE_ENTRY') THEN 4
                        WHEN decision IN ('BUY', 'SELL') THEN 3
                        ELSE 0
                    END DESC,
                    ABS(COALESCE(estimated_pnl_pct, 0)) DESC,
                    id DESC
                LIMIT ?
                """,
                (semantic_anchor_limit,),
            )
        candidate_ids = list(dict.fromkeys(recent_ids + anchor_ids))
        return self._fetch_journal_context_rows_by_ids(candidate_ids)

    def get_long_term_memory_pool(self) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT id, timestamp, rule, version, metrics
            FROM long_term_memory
            """
        )
        rows = [self._format_memory_row(dict(row)) for row in cursor.fetchall()]
        rows.sort(
            key=lambda row: (
                1 if bool(row.get("retired")) else 0,
                -float(row.get("confidence_score") or 0.0),
                -float(row.get("validity_score") or 0.0),
                -float(row.get("freshness_score") or 0.0),
                -int(row.get("id") or 0),
            )
        )
        return rows

    def get_long_term_memory(self) -> List[Dict[str, Any]]:
        try:
            rows = self.get_long_term_memory_pool()
            if not rows:
                return [
                    {
                        "rule": "No long term lessons recorded yet. Be conservative.",
                        "category": "risk_control",
                        "status": "rule",
                        "support": 0,
                        "regime": "general",
                        "retired": False,
                        "confidence_score": 0.0,
                        "validity_score": 0.0,
                        "freshness_score": 0.0,
                        "apply_when": {
                            "regime": ["general"],
                            "volatility": ["unknown"],
                            "trend": ["unknown"],
                        },
                        "ignore_when": ["No validated long-term evidence yet."],
                        "evidence_journal_ids": [],
                        "metrics": {},
                    }
                ]
            return rows
        except Exception as e:
            logger.error(f"Failed to fetch long term memory: {e}")
            return []

    def get_journal_entries_since(
        self,
        since: datetime,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT
                j.id,
                j.timestamp,
                j.audit_log_id,
                j.decision,
                j.rationale,
                j.rationale_json,
                j.confidence,
                j.risk_reason,
                j.estimated_pnl_pct,
                j.loss_signal,
                j.trade_case_ids,
                j.case_statuses,
                j.case_summary,
                COALESCE(ledger.net_realized_pnl, 0) AS ledger_net_realized_pnl,
                COALESCE(ledger.fee_cost, 0) AS ledger_fee_cost,
                a.risk_approved,
                a.risk_mode,
                a.market_state,
                a.execution_summary
            FROM journal j
            LEFT JOIN (
                SELECT
                    audit_log_id,
                    COALESCE(SUM(net_realized_pnl), 0) AS net_realized_pnl,
                    COALESCE(SUM(fee_cost), 0) AS fee_cost
                FROM position_ledger
                GROUP BY audit_log_id
            ) ledger ON ledger.audit_log_id = j.audit_log_id
            LEFT JOIN audit_logs a ON a.id = j.audit_log_id
            WHERE j.timestamp >= ?
            ORDER BY j.id ASC
        """
        params: List[Any] = [since.strftime("%Y-%m-%d %H:%M:%S")]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            rows.append(self._decode_journal_context_row(dict(row)))
        return rows

    def save_long_term_memory_entries(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                for entry in entries:
                    if entry.get("id") is not None:
                        self.conn.execute(
                            """
                            UPDATE long_term_memory
                            SET timestamp = CURRENT_TIMESTAMP,
                                rule = ?,
                                version = ?,
                                metrics = ?
                            WHERE id = ?
                            """,
                            (
                                entry["rule"],
                                int(entry.get("version", 1)),
                                json.dumps(entry.get("metrics", {})),
                                int(entry["id"]),
                            ),
                        )
                    else:
                        self.conn.execute(
                            """
                            INSERT INTO long_term_memory (rule, version, metrics)
                            VALUES (?, ?, ?)
                            """,
                            (
                                entry["rule"],
                                int(entry.get("version", 1)),
                                json.dumps(entry.get("metrics", {})),
                            ),
                        )
        except Exception as e:
            logger.error(f"Failed to save long-term memory entries: {e}")

    def save_symbol_journal_entries(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO symbol_journal (
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            int(entry.get("audit_log_id") or 0),
                            entry.get("symbol"),
                            entry.get("decision"),
                            entry.get("risk_reason"),
                            entry.get("risk_mode"),
                            int(bool(entry.get("risk_approved"))),
                            int(bool(entry.get("triggered"))),
                            int(bool(entry.get("risk_triggered"))),
                            entry.get("net_realized_pnl"),
                            entry.get("fee_cost"),
                            entry.get("estimated_pnl_pct"),
                            entry.get("trade_case_id"),
                            entry.get("case_status"),
                            entry.get("case_opened_at"),
                            entry.get("case_closed_at"),
                            entry.get("holding_minutes"),
                            json.dumps(entry.get("evidence_signals") or []),
                            entry.get("summary"),
                            json.dumps(entry.get("payload") or {}),
                        )
                        for entry in entries
                        if entry.get("symbol")
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save symbol journal entries: {e}")

    def get_recent_symbol_journal(self, limit: int = 50, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """
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
        """
        params: List[Any] = []
        if symbol:
            query += " WHERE symbol = ?"
            params.append(symbol)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            rows.append(self._decode_symbol_journal_row(dict(row)))
        return rows

    def get_symbol_journal_entries_since(
        self,
        since: datetime,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
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
            WHERE timestamp >= ?
            ORDER BY id ASC
        """
        params: List[Any] = [since.strftime("%Y-%m-%d %H:%M:%S")]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            rows.append(self._decode_symbol_journal_row(dict(row)))
        return rows
