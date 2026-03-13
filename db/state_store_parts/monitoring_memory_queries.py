from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional


class StateStoreMonitoringMemoryQueryMixin:
    def get_recent_long_term_memory(self, limit: int = 10) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT id, timestamp, rule, version, metrics
            FROM long_term_memory
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            rows.append(self._format_memory_row(dict(row)))
        rows.sort(
            key=lambda row: (
                1 if bool(row.get("retired")) else 0,
                -float(row.get("confidence_score") or 0.0),
                -float(row.get("validity_score") or 0.0),
                -int(row.get("id") or 0),
            )
        )
        return rows

    def _parse_db_timestamp(self, value: Any) -> Optional[datetime]:
        text = str(value or "").strip()
        if not text:
            return None
        for parser in (datetime.fromisoformat,):
            try:
                parsed = parser(text)
                if parsed.tzinfo is not None:
                    return parsed.astimezone(UTC).replace(tzinfo=None)
                return parsed
            except ValueError:
                continue
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None

    def _build_learning_liveness_summary(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._utcnow()
        since = now - timedelta(hours=24)
        replay_batch = self.get_learning_artifact("replay_batch_heartbeat")
        memory_batch = self.get_learning_artifact("memory_updater_heartbeat")
        shadow_dataset = self.get_learning_artifact("shadow_model_dataset")
        journal_reranker = self.get_learning_artifact("journal_reranker_profile")
        similar_case_reranker = self.get_learning_artifact("similar_case_reranker_profile")
        replay_run_count_24h = int(
            (self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM replay_runs
                WHERE created_at >= ?
                """,
                (since.strftime("%Y-%m-%d %H:%M:%S"),),
            ).fetchone() or {})["count"] or 0
        )
        replay_candidate_count_24h = len(self.get_replay_batch_candidates(since=since, limit=500))
        processed_rate = 1.0
        if replay_candidate_count_24h > 0:
            processed_rate = min(1.0, replay_run_count_24h / max(1, replay_candidate_count_24h))

        replay_updated_at = self._parse_db_timestamp(replay_batch.get("updated_at"))
        memory_updated_at = self._parse_db_timestamp(memory_batch.get("updated_at"))
        shadow_dataset_updated_at = self._parse_db_timestamp(shadow_dataset.get("updated_at"))
        stale_reasons: List[str] = []
        if replay_updated_at is None or (now - replay_updated_at) > timedelta(hours=30):
            stale_reasons.append("replay_batch_stale")
        if memory_updated_at is None or (now - memory_updated_at) > timedelta(hours=30):
            stale_reasons.append("memory_update_stale")
        if replay_candidate_count_24h > 0 and processed_rate < 0.5:
            stale_reasons.append("replay_coverage_low")
        if shadow_dataset_updated_at is None or (now - shadow_dataset_updated_at) > timedelta(hours=48):
            stale_reasons.append("shadow_dataset_stale")

        return {
            "last_replay_batch_at": replay_updated_at.isoformat() if replay_updated_at else None,
            "last_memory_update_at": memory_updated_at.isoformat() if memory_updated_at else None,
            "last_shadow_dataset_at": shadow_dataset_updated_at.isoformat() if shadow_dataset_updated_at else None,
            "replay_candidate_count_24h": replay_candidate_count_24h,
            "replay_run_count_24h": replay_run_count_24h,
            "replay_processed_rate_24h": round(processed_rate, 4),
            "stale_reasons": stale_reasons,
            "stale": bool(stale_reasons),
            "promotion_blocked": bool(stale_reasons),
            "reranker_ready": {
                "recent_journal": bool(journal_reranker.get("payload")),
                "similar_cases": bool(similar_case_reranker.get("payload")),
            },
            "shadow_dataset_ready": bool(shadow_dataset.get("payload")),
        }

    def _is_fallback_hold_row(self, row: Dict[str, Any]) -> bool:
        execution_summary = row.get("execution_summary") or {}
        decision_quality = execution_summary.get("decision_quality") or {}
        ai_usage = execution_summary.get("ai_usage") or {}
        if bool(ai_usage.get("fallback_safe_intent")):
            return True
        if not bool(decision_quality.get("decision_valid", True)):
            return True
        if not bool(decision_quality.get("explanation_valid", True)):
            return True
        text_values: List[str] = []
        for key in ("summary", "risk_reason"):
            value = row.get(key)
            if value:
                text_values.append(str(value).strip().lower())
        for value in (row.get("rationale") or []):
            text_values.append(str(value or "").strip().lower())
        for value in (row.get("risk_notes") or []):
            text_values.append(str(value or "").strip().lower())
        markers = (
            "fallback due to json validation error from ai",
            "fallback due to invalid semantic contract from ai",
            "fallback due to invalid explanation contract from ai",
            "semantic contract validation failed",
            "explanation validation failed",
            "system error",
        )
        return any(any(marker in text for marker in markers) for text in text_values if text)

    def _extract_hold_quality_bucket(self, row: Dict[str, Any]) -> str:
        if str(row.get("decision") or "").upper() != "HOLD":
            return ""
        if self._is_fallback_hold_row(row):
            return "fallback_hold"
        rationale_items = row.get("rationale_items") or row.get("rationale_json") or []
        if isinstance(rationale_items, list):
            for item in rationale_items:
                if not isinstance(item, dict):
                    continue
                hold_quality = str(item.get("hold_quality") or "").strip().lower()
                if hold_quality == "good_hold":
                    return "good_hold"
                if hold_quality in {"missed_opportunity_hold", "probe_only_opportunity_hold"}:
                    return "bad_hold"
        return "unlabeled_hold"

    def _build_hold_quality_summary(self, now: Optional[datetime] = None, days: int = 7) -> Dict[str, Any]:
        now = now or self._utcnow()
        since = now - timedelta(days=max(1, int(days or 7)))
        rows = self.get_journal_entries_since(since, limit=5000)
        summary = {
            "window_days": max(1, int(days or 7)),
            "total_hold_count": 0,
            "good_hold_count": 0,
            "bad_hold_count": 0,
            "fallback_hold_count": 0,
            "unlabeled_hold_count": 0,
            "evaluated_hold_count": 0,
            "good_hold_rate": 0.0,
            "bad_hold_rate": 0.0,
            "fallback_hold_rate": 0.0,
        }
        for row in rows or []:
            bucket = self._extract_hold_quality_bucket(row)
            if not bucket:
                continue
            summary["total_hold_count"] += 1
            if bucket == "good_hold":
                summary["good_hold_count"] += 1
            elif bucket == "bad_hold":
                summary["bad_hold_count"] += 1
            elif bucket == "fallback_hold":
                summary["fallback_hold_count"] += 1
            else:
                summary["unlabeled_hold_count"] += 1
        summary["evaluated_hold_count"] = (
            summary["good_hold_count"] + summary["bad_hold_count"] + summary["fallback_hold_count"]
        )
        total = max(1, summary["total_hold_count"])
        summary["good_hold_rate"] = round(summary["good_hold_count"] / total, 4) if summary["total_hold_count"] else 0.0
        summary["bad_hold_rate"] = round(summary["bad_hold_count"] / total, 4) if summary["total_hold_count"] else 0.0
        summary["fallback_hold_rate"] = (
            round(summary["fallback_hold_count"] / total, 4) if summary["total_hold_count"] else 0.0
        )
        return summary

    def _coerce_monitoring_float(self, value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _build_retrieval_outcome_metrics(self, row: Dict[str, Any]) -> Dict[str, Any]:
        realized_pnl = self._coerce_monitoring_float(row.get("outcome_realized_pnl"))
        estimated_pnl = self._coerce_monitoring_float(row.get("outcome_estimated_pnl_pct"))
        pnl = realized_pnl if realized_pnl is not None else estimated_pnl
        mfe = self._coerce_monitoring_float(row.get("outcome_observed_mfe_pct"))
        mae = self._coerce_monitoring_float(row.get("outcome_observed_mae_pct"))
        slippage_bps = self._coerce_monitoring_float(row.get("outcome_slippage_bps"))
        holding_minutes = self._coerce_monitoring_float(row.get("outcome_holding_minutes"))
        metrics = {
            "pnl": pnl,
            "mfe": max(0.0, mfe or 0.0) if mfe is not None else None,
            "mae": abs(mae) if mae is not None else None,
            "slippage_bps": max(0.0, slippage_bps or 0.0) if slippage_bps is not None else None,
            "holding_minutes": max(0.0, holding_minutes or 0.0) if holding_minutes is not None else None,
            "_has_realized_pnl": realized_pnl is not None,
        }
        metrics["_data_points"] = sum(
            1 for key in ("pnl", "mfe", "mae", "slippage_bps", "holding_minutes") if metrics.get(key) is not None
        )
        return metrics

    def _merge_retrieval_outcome_metrics(
        self,
        existing: Dict[str, Any] | None,
        candidate: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not existing:
            return candidate
        existing_rank = (1 if existing.get("_has_realized_pnl") else 0, int(existing.get("_data_points") or 0))
        candidate_rank = (1 if candidate.get("_has_realized_pnl") else 0, int(candidate.get("_data_points") or 0))
        merged = dict(candidate if candidate_rank > existing_rank else existing)
        fallback = existing if merged is candidate else candidate
        for key in ("pnl", "mfe", "mae", "slippage_bps", "holding_minutes"):
            if merged.get(key) is None and fallback.get(key) is not None:
                merged[key] = fallback.get(key)
        merged["_has_realized_pnl"] = bool(existing.get("_has_realized_pnl") or candidate.get("_has_realized_pnl"))
        merged["_data_points"] = sum(
            1 for key in ("pnl", "mfe", "mae", "slippage_bps", "holding_minutes") if merged.get(key) is not None
        )
        return merged

    def _score_decision_against_outcome_utility(self, decision: str, outcome_metrics: Dict[str, Any]) -> float:
        decision_text = str(decision or "").upper().strip()
        pnl = self._coerce_monitoring_float(outcome_metrics.get("pnl"))
        if pnl is None:
            return 0.0
        mfe = max(0.0, self._coerce_monitoring_float(outcome_metrics.get("mfe")) or 0.0)
        mae = max(0.0, self._coerce_monitoring_float(outcome_metrics.get("mae")) or 0.0)
        slippage_bps = max(0.0, self._coerce_monitoring_float(outcome_metrics.get("slippage_bps")) or 0.0)
        holding_minutes = max(0.0, self._coerce_monitoring_float(outcome_metrics.get("holding_minutes")) or 0.0)
        reward_signal = max(pnl, 0.0) + (mfe * 0.35)
        holding_penalty = (
            max(0.0, holding_minutes - 60.0) / 240.0
            if pnl <= 0.0 or mae >= max(max(pnl, 0.0), 0.5)
            else max(0.0, holding_minutes - 180.0) / 720.0
        )
        risk_signal = max(-pnl, 0.0) + (mae * 0.55) + (slippage_bps * 0.1) + holding_penalty
        active_score = reward_signal - risk_signal
        reduce_score = (risk_signal * 0.75) - (reward_signal * 0.15)
        exit_score = risk_signal - (reward_signal * 0.3)
        hold_score = (min(reward_signal, risk_signal) * 0.35) - (abs(reward_signal - risk_signal) * 0.4) - (
            slippage_bps * 0.01
        )
        decision_scores = {
            "BUY": active_score,
            "SELL": active_score,
            "HOLD": hold_score,
            "REDUCE": reduce_score,
            "EXIT": exit_score,
        }
        return decision_scores.get(decision_text, 0.0)

    def _build_replay_learning_summary(self, now: Optional[datetime] = None, days: int = 7) -> Dict[str, Any]:
        now = now or self._utcnow()
        since = now - timedelta(days=max(1, int(days or 7)))
        replay_runs = self.get_replay_runs_since(since, limit=5000)
        retrieval_evaluations = self.get_retrieval_evaluations_since(since, limit=5000)
        settled_outcomes: Dict[int, Dict[str, Any]] = {}
        for row in retrieval_evaluations or []:
            audit_log_id = row.get("audit_log_id")
            if audit_log_id is None:
                continue
            try:
                audit_key = int(audit_log_id)
            except (TypeError, ValueError):
                continue
            candidate = self._build_retrieval_outcome_metrics(row)
            if candidate.get("pnl") is None:
                continue
            settled_outcomes[audit_key] = self._merge_retrieval_outcome_metrics(
                settled_outcomes.get(audit_key),
                candidate,
            )
        evaluation_count = 0
        decision_changed_count = 0
        side_changed_count = 0
        confidence_delta_sum = 0.0
        size_delta_sum = 0.0
        outcome_linked_count = 0
        with_learning_better_count = 0
        without_learning_better_count = 0
        tie_count = 0
        utility_delta_sum = 0.0
        for row in replay_runs or []:
            counterfactual = ((row.get("payload") or {}).get("retrieval_counterfactual") or {})
            comparison = counterfactual.get("with_vs_without_comparison") or {}
            if not counterfactual:
                continue
            evaluation_count += 1
            if bool(comparison.get("decision_changed")):
                decision_changed_count += 1
            if bool(comparison.get("side_changed")):
                side_changed_count += 1
            confidence_delta_sum += float(self._coerce_monitoring_float(comparison.get("confidence_delta")) or 0.0)
            size_delta_sum += abs(float(self._coerce_monitoring_float(comparison.get("size_delta")) or 0.0))
            audit_log_id = row.get("audit_log_id")
            try:
                outcome_metrics = settled_outcomes[int(audit_log_id)]
            except (KeyError, TypeError, ValueError):
                continue
            with_intent = counterfactual.get("with_retrieval_intent") or {}
            without_intent = counterfactual.get("without_retrieval_intent") or {}
            with_score = self._score_decision_against_outcome_utility(with_intent.get("decision"), outcome_metrics)
            without_score = self._score_decision_against_outcome_utility(without_intent.get("decision"), outcome_metrics)
            outcome_linked_count += 1
            utility_delta_sum += with_score - without_score
            if abs(with_score - without_score) <= 0.05:
                tie_count += 1
            elif with_score > without_score:
                with_learning_better_count += 1
            else:
                without_learning_better_count += 1
        return {
            "window_days": max(1, int(days or 7)),
            "evaluation_count": evaluation_count,
            "decision_changed_count": decision_changed_count,
            "side_changed_count": side_changed_count,
            "avg_confidence_delta": round(confidence_delta_sum / evaluation_count, 6) if evaluation_count else 0.0,
            "avg_size_delta": round(size_delta_sum / evaluation_count, 6) if evaluation_count else 0.0,
            "outcome_linked_count": outcome_linked_count,
            "with_learning_better_count": with_learning_better_count,
            "without_learning_better_count": without_learning_better_count,
            "tie_count": tie_count,
            "avg_outcome_utility_delta": round(utility_delta_sum / outcome_linked_count, 6) if outcome_linked_count else 0.0,
        }

    def _build_label_quality_summary(self, now: Optional[datetime] = None, days: int = 7) -> Dict[str, Any]:
        now = now or self._utcnow()
        since = now - timedelta(days=max(1, int(days or 7)))
        rows = self.get_execution_policy_evaluations_since(since)
        tracked_labels = {
            "edge_failed": 0,
            "missed_due_to_no_fill": 0,
            "risk_exit": 0,
            "stale_entry_cancelled": 0,
        }
        relevant_count = 0
        tracked_total = 0
        outcome_linked_count = 0
        for row in rows or []:
            action_type = str(row.get("action_type") or "").strip().lower()
            if action_type not in {"entry", "exit", "cancel"}:
                continue
            relevant_count += 1
            if row.get("outcome_trade_case_id") or row.get("outcome_status"):
                outcome_linked_count += 1
            label = str(row.get("outcome_label") or "").strip().lower()
            if label in tracked_labels:
                tracked_labels[label] += 1
                tracked_total += 1
        top_label = ""
        top_count = 0
        if tracked_total:
            top_label, top_count = max(tracked_labels.items(), key=lambda item: item[1])
        return {
            "window_days": max(1, int(days or 7)),
            "relevant_count": relevant_count,
            "tracked_total": tracked_total,
            "outcome_linked_count": outcome_linked_count,
            "coverage_rate": round(tracked_total / relevant_count, 4) if relevant_count else 0.0,
            "outcome_linked_rate": round(outcome_linked_count / relevant_count, 4) if relevant_count else 0.0,
            "top_label": top_label,
            "top_count": top_count,
            "label_counts": tracked_labels,
        }
