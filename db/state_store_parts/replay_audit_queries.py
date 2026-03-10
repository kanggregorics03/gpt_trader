from datetime import datetime
from typing import Any, Dict, List, Optional


class StateStoreReplayAuditQueryMixin:
    @staticmethod
    def _coerce_replay_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _extract_replay_candidate_snapshot(self, item: Dict[str, Any]) -> Dict[str, Any]:
        request_payload = item.get("decision_request") or {}
        snapshots = request_payload.get("market_snapshots") or {}
        if isinstance(snapshots, dict) and snapshots:
            first_symbol = next(iter(snapshots.keys()))
            snapshot = snapshots.get(first_symbol) or {}
            if isinstance(snapshot, dict):
                return snapshot
        snapshot = request_payload.get("market_snapshot") or {}
        if isinstance(snapshot, dict):
            return snapshot
        return {}

    def _score_replay_batch_candidate(self, item: Dict[str, Any]) -> float:
        intent = item.get("intent") or {}
        decision = str(intent.get("decision") or "HOLD").upper()
        risk_ok = bool(item.get("risk_approved"))
        has_replay = bool(item.get("latest_created_at"))
        estimated_pnl_pct = self._coerce_replay_float(item.get("estimated_pnl_pct"))
        loss_severity = abs(min(0.0, estimated_pnl_pct))
        request_payload = item.get("decision_request") or {}
        similar_cases = list(request_payload.get("similar_past_cases") or [])
        latest_replay_payload = item.get("latest_replay_payload") or {}
        retrieval_counterfactual = latest_replay_payload.get("retrieval_counterfactual") or {}
        retrieval_comparison = retrieval_counterfactual.get("with_vs_without_comparison") or {}
        hold_counterfactual = latest_replay_payload.get("hold_opportunity_counterfactual") or {}
        snapshot = self._extract_replay_candidate_snapshot(item)
        trigger_flags = sum(
            1
            for key in ("fast_move_signal_active", "volume_surge_signal_active", "range_break_signal_active")
            if bool(snapshot.get(key))
        )
        is_trade = decision in ("BUY", "SELL", "REDUCE", "EXIT")
        is_rejected_entry = (not risk_ok) and decision in ("BUY", "SELL")
        is_hold = decision == "HOLD"

        score = 0.0
        if is_trade:
            score += 16.0
        elif decision != "HOLD":
            score += 7.0
        if is_rejected_entry:
            score += 10.0
        if not has_replay:
            score += 6.0

        score += min(12.0, loss_severity * 6.0)
        if similar_cases:
            score += min(4.0, float(len(similar_cases)) * 0.75)
        if retrieval_counterfactual:
            score += 2.0
        if bool(retrieval_comparison.get("decision_changed")):
            score += 4.0
        if bool(retrieval_comparison.get("side_changed")):
            score += 3.0
        score += min(3.0, abs(self._coerce_replay_float(retrieval_comparison.get("size_delta"))) * 2.0)
        score += min(2.0, abs(self._coerce_replay_float(retrieval_comparison.get("confidence_delta"))) * 5.0)

        if is_hold:
            hold_quality = str(hold_counterfactual.get("hold_quality") or "").strip().lower()
            if hold_quality == "missed_opportunity_hold":
                score += 6.0
            elif hold_quality == "probe_only_opportunity_hold":
                score += 4.0
            score += min(8.0, self._coerce_replay_float(hold_counterfactual.get("hold_opportunity_cost")) * 2.0)
            score += min(4.0, float(int(hold_counterfactual.get("missed_breakout_count") or 0)) * 1.5)
            score += min(4.0, float(trigger_flags))

        return round(score, 6)

    def _select_replay_batch_candidates(
        self,
        rows: List[Dict[str, Any]],
        *,
        limit: int,
        hold_quota: int = 0,
    ) -> List[Dict[str, Any]]:
        sorted_rows = sorted(
            rows,
            key=lambda item: (
                float(item.get("replay_priority_score") or 0.0),
                int(item.get("id") or 0),
            ),
            reverse=True,
        )
        limit = max(1, int(limit or 1))
        hold_quota = max(0, min(int(hold_quota or 0), limit))
        if hold_quota <= 0:
            return sorted_rows[:limit]

        hold_rows = [
            row
            for row in sorted_rows
            if str((row.get("intent") or {}).get("decision") or "HOLD").upper() == "HOLD"
        ]
        selected: List[Dict[str, Any]] = hold_rows[:hold_quota]
        selected_ids = {int(row.get("id") or 0) for row in selected}
        for row in sorted_rows:
            row_id = int(row.get("id") or 0)
            if row_id in selected_ids:
                continue
            selected.append(row)
            selected_ids.add(row_id)
            if len(selected) >= limit:
                break
        selected.sort(
            key=lambda item: (
                float(item.get("replay_priority_score") or 0.0),
                int(item.get("id") or 0),
            ),
            reverse=True,
        )
        return selected[:limit]

    def get_replay_candidates(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT id, timestamp, intent, risk_approved, risk_reason, execution_summary, market_vectors
            FROM audit_logs
            WHERE decision_request IS NOT NULL
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
            if not self.is_learning_eligible_audit_record(item):
                continue
            rows.append(item)
        return rows

    def get_replay_batch_candidates(
        self,
        *,
        since: datetime,
        limit: int = 40,
        hold_quota: int = 0,
    ) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            WITH latest_replay AS (
                SELECT replay.audit_log_id, replay.created_at AS latest_created_at, replay.payload AS latest_payload
                FROM replay_runs AS replay
                INNER JOIN (
                    SELECT audit_log_id, MAX(id) AS max_id
                    FROM replay_runs
                    GROUP BY audit_log_id
                ) AS latest
                    ON latest.max_id = replay.id
            ),
            journal_summary AS (
                SELECT audit_log_id, MIN(estimated_pnl_pct) AS estimated_pnl_pct
                FROM journal
                GROUP BY audit_log_id
            )
            SELECT
                audit.id,
                audit.timestamp,
                audit.intent,
                audit.risk_approved,
                audit.risk_reason,
                audit.risk_mode,
                audit.decision_request,
                audit.execution_summary,
                audit.market_vectors,
                journal.estimated_pnl_pct,
                replay.latest_created_at,
                replay.latest_payload
            FROM audit_logs AS audit
            LEFT JOIN journal_summary AS journal
                ON journal.audit_log_id = audit.id
            LEFT JOIN latest_replay AS replay
                ON replay.audit_log_id = audit.id
            WHERE audit.decision_request IS NOT NULL
              AND audit.timestamp >= ?
            ORDER BY audit.id DESC
            """,
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["intent"] = self._safe_json_loads(item.get("intent"), default={})
            item["decision_request"] = self._safe_json_loads(item.get("decision_request"), default={})
            item["execution_summary"] = self._safe_json_loads(item.get("execution_summary"), default={})
            item["market_vectors"] = self._safe_json_loads(item.get("market_vectors"), default={})
            item["latest_replay_payload"] = self._safe_json_loads(item.get("latest_payload"), default={})
            item.pop("latest_payload", None)
            if not self.is_learning_eligible_audit_record(item):
                continue
            item["replay_priority_score"] = self._score_replay_batch_candidate(item)
            rows.append(item)
        return self._select_replay_batch_candidates(
            rows,
            limit=max(1, int(limit or 40)),
            hold_quota=hold_quota,
        )

    def get_recent_rag_candidates(self, limit: int = 500) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                id,
                timestamp,
                market_state,
                account_state,
                market_vectors,
                intent,
                risk_approved,
                risk_reason,
                execution_summary
            FROM audit_logs
            WHERE market_state IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["market_state"] = self._safe_json_loads(item.get("market_state"), default={})
            item["account_state"] = self._safe_json_loads(item.get("account_state"), default={})
            item["market_vectors"] = self._safe_json_loads(item.get("market_vectors"), default={})
            item["intent"] = self._safe_json_loads(item.get("intent"), default={})
            item["execution_summary"] = self._safe_json_loads(item.get("execution_summary"), default={})
            if not self.is_learning_eligible_audit_record(item):
                continue
            rows.append(item)
        return rows

    def get_recent_replay_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
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
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [self._decode_replay_run_row(dict(row)) for row in cursor.fetchall()]

    def get_replay_runs_for_audit(self, audit_log_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
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
            WHERE audit_log_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (audit_log_id, limit),
        )
        return [self._decode_replay_run_row(dict(row)) for row in cursor.fetchall()]

    def _decode_replay_run_row(self, item: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(item)
        row["rationale_keywords_added"] = self._safe_json_loads(
            row.get("rationale_keywords_added"),
            default=[],
        )
        row["rationale_keywords_removed"] = self._safe_json_loads(
            row.get("rationale_keywords_removed"),
            default=[],
        )
        row["evidence_signals_added"] = self._safe_json_loads(
            row.get("evidence_signals_added"),
            default=[],
        )
        row["evidence_signals_removed"] = self._safe_json_loads(
            row.get("evidence_signals_removed"),
            default=[],
        )
        row["payload"] = self._safe_json_loads(row.get("payload"), default={})
        return row
