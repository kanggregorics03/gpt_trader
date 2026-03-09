from datetime import datetime, timedelta
from typing import Any, Dict, Optional


class StateStoreContextBiasQueryMixin:
    def _summarize_context_mix_counts(self, counts: Dict[str, int]) -> Dict[str, Any]:
        raw_counts = {str(key): int(value) for key, value in (counts or {}).items() if int(value or 0) > 0}
        total = sum(raw_counts.values())
        group_counts = {
            "good_hold": raw_counts.get("good_hold", 0),
            "missed_opportunity_hold": raw_counts.get("missed_opportunity_hold", 0),
            "fallback": raw_counts.get("system_fallback", 0),
            "other_hold": raw_counts.get("hold", 0),
            "action": (
                raw_counts.get("entry_action", 0)
                + raw_counts.get("risk_outcome", 0)
                + raw_counts.get("other_action", 0)
            ),
        }
        group_counts = {key: value for key, value in group_counts.items() if value > 0}
        group_total = sum(group_counts.values())
        return {
            "bucket_counts": raw_counts,
            "bucket_ratios": {
                key: round((value / total), 4) for key, value in raw_counts.items()
            } if total else {},
            "group_counts": group_counts,
            "group_ratios": {
                key: round((value / group_total), 4) for key, value in group_counts.items()
            } if group_total else {},
            "total": total,
        }

    def _build_context_bias_summary(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._utcnow()
        recent_logs = self.get_recent_audits(limit=500, since=now - timedelta(hours=24))
        journal_selected: Dict[str, int] = {}
        journal_available: Dict[str, int] = {}
        journal_excluded: Dict[str, int] = {}
        memory_selected: Dict[str, int] = {}
        memory_available: Dict[str, int] = {}
        memory_excluded: Dict[str, int] = {}
        audits_with_context = 0

        for row in recent_logs:
            execution_summary = row.get("execution_summary") or {}
            context_summary = execution_summary.get("context_selection_summary") or {}
            journal_summary = context_summary.get("recent_journal_context") or {}
            memory_summary = context_summary.get("long_term_memory") or {}
            if journal_summary or memory_summary:
                audits_with_context += 1
            for key, value in (journal_summary.get("selected_bucket_counts") or {}).items():
                journal_selected[str(key)] = journal_selected.get(str(key), 0) + int(value or 0)
            for key, value in (journal_summary.get("available_bucket_counts") or {}).items():
                journal_available[str(key)] = journal_available.get(str(key), 0) + int(value or 0)
            for key, value in (journal_summary.get("excluded_bucket_counts") or {}).items():
                journal_excluded[str(key)] = journal_excluded.get(str(key), 0) + int(value or 0)
            for key, value in (memory_summary.get("selected_bucket_counts") or {}).items():
                memory_selected[str(key)] = memory_selected.get(str(key), 0) + int(value or 0)
            for key, value in (memory_summary.get("available_bucket_counts") or {}).items():
                memory_available[str(key)] = memory_available.get(str(key), 0) + int(value or 0)
            for key, value in (memory_summary.get("excluded_bucket_counts") or {}).items():
                memory_excluded[str(key)] = memory_excluded.get(str(key), 0) + int(value or 0)

        return {
            "last_24h": {
                "audit_count": len(recent_logs),
                "audits_with_context": audits_with_context,
                "journal_selected": self._summarize_context_mix_counts(journal_selected),
                "journal_available": self._summarize_context_mix_counts(journal_available),
                "journal_excluded": self._summarize_context_mix_counts(journal_excluded),
                "memory_selected": self._summarize_context_mix_counts(memory_selected),
                "memory_available": self._summarize_context_mix_counts(memory_available),
                "memory_excluded": self._summarize_context_mix_counts(memory_excluded),
            }
        }
