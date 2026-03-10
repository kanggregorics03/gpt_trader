import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStoreTradeCaseRepositoryMixin:
    def upsert_trade_case(self, record: Dict[str, Any]):
        trade_case_id = str(record.get("trade_case_id") or "").strip()
        symbol = str(record.get("symbol") or "").strip()
        status = str(record.get("status") or "open").strip() or "open"
        if not trade_case_id or not symbol:
            return
        try:
            payload = record.get("payload") or {}
            loop_mode = record.get("loop_mode")
            guarded_attention = record.get("guarded_attention")
            safety_mode = record.get("safety_mode")
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO trade_cases (
                        trade_case_id,
                        symbol,
                        status,
                        loop_mode,
                        risk_mode,
                        guarded_attention,
                        safety_mode,
                        opened_at,
                        closed_at,
                        entry_audit_log_id,
                        exit_audit_log_id,
                        entry_decision,
                        exit_decision,
                        entry_side,
                        entry_price,
                        exit_price,
                        estimated_pnl_pct,
                        realized_pnl,
                        fee_cost,
                        holding_minutes,
                        observed_mfe_pct,
                        observed_mae_pct,
                        slippage_cost,
                        slippage_bps,
                        thesis_failure_reason,
                        thesis_id,
                        management_template_id,
                        exit_reason,
                        evidence_signals,
                        trigger_reasons,
                        risk_trigger_reasons,
                        feature_vector,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_case_id) DO UPDATE SET
                        symbol = excluded.symbol,
                        status = excluded.status,
                        loop_mode = excluded.loop_mode,
                        risk_mode = excluded.risk_mode,
                        guarded_attention = excluded.guarded_attention,
                        safety_mode = excluded.safety_mode,
                        opened_at = excluded.opened_at,
                        closed_at = excluded.closed_at,
                        entry_audit_log_id = excluded.entry_audit_log_id,
                        exit_audit_log_id = excluded.exit_audit_log_id,
                        entry_decision = excluded.entry_decision,
                        exit_decision = excluded.exit_decision,
                        entry_side = excluded.entry_side,
                        entry_price = excluded.entry_price,
                        exit_price = excluded.exit_price,
                        estimated_pnl_pct = excluded.estimated_pnl_pct,
                        realized_pnl = excluded.realized_pnl,
                        fee_cost = excluded.fee_cost,
                        holding_minutes = excluded.holding_minutes,
                        observed_mfe_pct = excluded.observed_mfe_pct,
                        observed_mae_pct = excluded.observed_mae_pct,
                        slippage_cost = excluded.slippage_cost,
                        slippage_bps = excluded.slippage_bps,
                        thesis_failure_reason = excluded.thesis_failure_reason,
                        thesis_id = excluded.thesis_id,
                        management_template_id = excluded.management_template_id,
                        exit_reason = excluded.exit_reason,
                        evidence_signals = excluded.evidence_signals,
                        trigger_reasons = excluded.trigger_reasons,
                        risk_trigger_reasons = excluded.risk_trigger_reasons,
                        feature_vector = excluded.feature_vector,
                        payload = excluded.payload
                    """,
                    (
                        trade_case_id,
                        symbol,
                        status,
                        loop_mode,
                        record.get("risk_mode"),
                        int(bool(guarded_attention)),
                        int(bool(safety_mode)),
                        record.get("opened_at"),
                        record.get("closed_at"),
                        record.get("entry_audit_log_id"),
                        record.get("exit_audit_log_id"),
                        record.get("entry_decision"),
                        record.get("exit_decision"),
                        record.get("entry_side"),
                        record.get("entry_price"),
                        record.get("exit_price"),
                        record.get("estimated_pnl_pct"),
                        record.get("realized_pnl"),
                        record.get("fee_cost"),
                        record.get("holding_minutes"),
                        record.get("observed_mfe_pct"),
                        record.get("observed_mae_pct"),
                        record.get("slippage_cost"),
                        record.get("slippage_bps"),
                        record.get("thesis_failure_reason"),
                        record.get("thesis_id"),
                        record.get("management_template_id"),
                        record.get("exit_reason"),
                        json.dumps(record.get("evidence_signals") or []),
                        json.dumps(record.get("trigger_reasons") or []),
                        json.dumps(record.get("risk_trigger_reasons") or []),
                        json.dumps(record.get("feature_vector") or []),
                        json.dumps(payload),
                    ),
                )
                feature_vector = record.get("feature_vector") or []
                self.conn.execute(
                    """
                    INSERT INTO trade_case_vector_index (
                        trade_case_id,
                        symbol,
                        status,
                        loop_mode,
                        risk_mode,
                        guarded_attention,
                        safety_mode,
                        opened_at,
                        closed_at,
                        estimated_pnl_pct,
                        realized_pnl,
                        slippage_bps,
                        thesis_failure_reason,
                        vector_norm,
                        feature_vector,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_case_id) DO UPDATE SET
                        symbol = excluded.symbol,
                        status = excluded.status,
                        loop_mode = excluded.loop_mode,
                        risk_mode = excluded.risk_mode,
                        guarded_attention = excluded.guarded_attention,
                        safety_mode = excluded.safety_mode,
                        opened_at = excluded.opened_at,
                        closed_at = excluded.closed_at,
                        estimated_pnl_pct = excluded.estimated_pnl_pct,
                        realized_pnl = excluded.realized_pnl,
                        slippage_bps = excluded.slippage_bps,
                        thesis_failure_reason = excluded.thesis_failure_reason,
                        vector_norm = excluded.vector_norm,
                        feature_vector = excluded.feature_vector,
                        payload = excluded.payload
                    """,
                    (
                        trade_case_id,
                        symbol,
                        status,
                        loop_mode,
                        record.get("risk_mode"),
                        int(bool(guarded_attention)),
                        int(bool(safety_mode)),
                        record.get("opened_at"),
                        record.get("closed_at"),
                        record.get("estimated_pnl_pct"),
                        record.get("realized_pnl"),
                        record.get("slippage_bps"),
                        record.get("thesis_failure_reason"),
                        self._vector_norm(feature_vector),
                        json.dumps(feature_vector),
                        json.dumps(
                            {
                                "trigger_reasons": record.get("trigger_reasons") or [],
                                "risk_trigger_reasons": record.get("risk_trigger_reasons") or [],
                                "evidence_signals": record.get("evidence_signals") or [],
                                "payload": payload,
                            }
                        ),
                    ),
                )
                self.conn.execute(
                    "DELETE FROM trade_case_vector_bands WHERE trade_case_id = ?",
                    (trade_case_id,),
                )
                band_entries = self._build_trade_case_vector_band_entries(
                    trade_case_id=trade_case_id,
                    symbol=symbol,
                    feature_vector=feature_vector,
                    closed_at=record.get("closed_at"),
                )
                if band_entries:
                    self.conn.executemany(
                        """
                        INSERT INTO trade_case_vector_bands (
                            trade_case_id,
                            symbol,
                            band_index,
                            band_hash,
                            closed_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        band_entries,
                    )
        except Exception as e:
            logger.error(f"Failed to upsert trade case: {e}")

    def get_trade_case(self, trade_case_id: str) -> Dict[str, Any]:
        if not trade_case_id:
            return {}
        cursor = self.conn.execute(
            """
            SELECT *
            FROM trade_cases
            WHERE trade_case_id = ?
            LIMIT 1
            """,
            (trade_case_id,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = self._decode_trade_case_row(dict(row))
        if not self.is_prompt_eligible_trade_case_record(item):
            return {}
        return item

    def get_recent_trade_cases(
        self,
        limit: int = 50,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT *
            FROM trade_cases
        """
        params: List[Any] = []
        clauses: List[str] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY COALESCE(closed_at, opened_at) DESC, trade_case_id DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = self._decode_trade_case_row(dict(row))
            if not self.is_prompt_eligible_trade_case_record(item):
                continue
            rows.append(item)
        return rows

    def get_trade_cases_since(
        self,
        since: datetime,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT *
            FROM trade_cases
            WHERE COALESCE(closed_at, opened_at) >= ?
            ORDER BY COALESCE(closed_at, opened_at) ASC, trade_case_id ASC
        """
        params: List[Any] = [since.strftime("%Y-%m-%d %H:%M:%S")]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = self._decode_trade_case_row(dict(row))
            if not self.is_prompt_eligible_trade_case_record(item):
                continue
            rows.append(item)
        return rows

    def get_recent_closed_trade_case_for_symbol(
        self,
        symbol: str,
        close_cooldown_minutes: int = 0,
        loss_cooldown_minutes: int = 0,
        loss_threshold_pct: float = 0.0,
        require_no_active_case: bool = False,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return {}
        if require_no_active_case and self.get_active_trade_case(symbol):
            return {}
        cursor = self.conn.execute(
            """
            SELECT *
            FROM trade_cases
            WHERE symbol = ?
              AND status = 'closed'
              AND closed_at IS NOT NULL
            ORDER BY closed_at DESC, trade_case_id DESC
            LIMIT 1
            """,
            (symbol,),
        )
        row = cursor.fetchone()
        if row is None:
            return {}
        item = self._decode_trade_case_row(dict(row))
        closed_at_text = str(item.get("closed_at") or "").strip()
        if not closed_at_text:
            return {}
        try:
            closed_at = datetime.strptime(closed_at_text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return {}
        now = now or self._utcnow()
        elapsed_minutes = max(0.0, (now - closed_at).total_seconds() / 60.0)
        estimated_pnl_pct = item.get("estimated_pnl_pct")
        try:
            estimated_pnl_pct = float(estimated_pnl_pct) if estimated_pnl_pct is not None else None
        except (TypeError, ValueError):
            estimated_pnl_pct = None

        active = False
        reason = ""
        remaining_minutes = 0
        if (
            loss_cooldown_minutes > 0
            and estimated_pnl_pct is not None
            and estimated_pnl_pct <= float(loss_threshold_pct)
            and elapsed_minutes < float(loss_cooldown_minutes)
        ):
            active = True
            reason = "recent_symbol_loss_guard"
            remaining_minutes = max(0, int(round(float(loss_cooldown_minutes) - elapsed_minutes)))
        elif close_cooldown_minutes > 0 and elapsed_minutes < float(close_cooldown_minutes):
            active = True
            reason = "recent_symbol_close_guard"
            remaining_minutes = max(0, int(round(float(close_cooldown_minutes) - elapsed_minutes)))

        return {
            "active": active,
            "reason": reason,
            "remaining_minutes": remaining_minutes,
            "trade_case_id": item.get("trade_case_id"),
            "symbol": symbol,
            "closed_at": closed_at_text,
            "estimated_pnl_pct": estimated_pnl_pct,
            "exit_reason": item.get("exit_reason"),
            "elapsed_minutes": round(elapsed_minutes, 2),
        }

    def get_trade_case_vector_candidates(
        self,
        limit: int = 200,
        symbols: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT
                v.*,
                t.entry_audit_log_id,
                t.exit_audit_log_id
            FROM trade_case_vector_index v
            LEFT JOIN trade_cases t
                ON t.trade_case_id = v.trade_case_id
        """
        params: List[Any] = []
        clauses: List[str] = []
        clean_symbols = [str(symbol).strip() for symbol in (symbols or []) if str(symbol).strip()]
        clean_statuses = [str(status).strip() for status in (statuses or []) if str(status).strip()]
        if clean_symbols:
            placeholders = ", ".join("?" for _ in clean_symbols)
            clauses.append(f"v.symbol IN ({placeholders})")
            params.extend(clean_symbols)
        if clean_statuses:
            placeholders = ", ".join("?" for _ in clean_statuses)
            clauses.append(f"v.status IN ({placeholders})")
            params.extend(clean_statuses)
        if since is not None:
            clauses.append("COALESCE(v.closed_at, v.opened_at) >= ?")
            params.append(since.strftime("%Y-%m-%d %H:%M:%S"))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY COALESCE(v.closed_at, v.opened_at) DESC, v.trade_case_id DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = self._decode_trade_case_row(dict(row))
            if not self.is_prompt_eligible_trade_case_record(item):
                continue
            rows.append(self._decode_trade_case_vector_row(item))
        return rows

    def get_trade_case_vector_band_candidates(
        self,
        current_feature_vectors: Dict[str, List[float]],
        limit: int = 200,
        statuses: Optional[List[str]] = None,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        probes: List[tuple[str, int, str]] = []
        for symbol, feature_vector in (current_feature_vectors or {}).items():
            symbol_text = str(symbol).strip()
            if not symbol_text:
                continue
            for band_index, band_hash in self._build_vector_band_hashes(feature_vector):
                probes.append((symbol_text, band_index, band_hash))
        if not probes:
            return []
        query = """
            SELECT
                v.*,
                t.entry_audit_log_id,
                t.exit_audit_log_id,
                COUNT(*) AS matched_band_count
            FROM trade_case_vector_bands b
            JOIN trade_case_vector_index v
                ON v.trade_case_id = b.trade_case_id
            LEFT JOIN trade_cases t
                ON t.trade_case_id = v.trade_case_id
            WHERE
        """
        params: List[Any] = []
        probe_clauses = []
        for symbol, band_index, band_hash in probes:
            probe_clauses.append("(b.symbol = ? AND b.band_index = ? AND b.band_hash = ?)")
            params.extend([symbol, band_index, band_hash])
        query += " (" + " OR ".join(probe_clauses) + ")"

        clean_statuses = [str(status).strip() for status in (statuses or []) if str(status).strip()]
        if clean_statuses:
            placeholders = ", ".join("?" for _ in clean_statuses)
            query += f" AND v.status IN ({placeholders})"
            params.extend(clean_statuses)
        if since is not None:
            query += " AND COALESCE(v.closed_at, v.opened_at) >= ?"
            params.append(since.strftime("%Y-%m-%d %H:%M:%S"))

        query += """
            GROUP BY v.trade_case_id
            ORDER BY matched_band_count DESC, COALESCE(v.closed_at, v.opened_at) DESC, v.trade_case_id DESC
            LIMIT ?
        """
        params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = self._decode_trade_case_row(dict(row))
            if not self.is_prompt_eligible_trade_case_record(item):
                continue
            rows.append(self._decode_trade_case_vector_row(item))
        return rows
