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
