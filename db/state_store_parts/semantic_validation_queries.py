from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


class StateStoreSemanticValidationQueryMixin:
    def _build_semantic_validation_summary(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._utcnow()
        last_24h_logs = self.get_recent_audits(limit=500, since=now - timedelta(hours=24))
        last_7d_logs = self.get_recent_audits(limit=5000, since=now - timedelta(days=7))
        latest_50_logs = self.get_recent_audits(limit=50)
        return {
            "last_24h": self._summarize_semantic_validation_issues(last_24h_logs),
            "last_7d": self._summarize_semantic_validation_issues(last_7d_logs),
            "latest_50": self._summarize_semantic_validation_issues(latest_50_logs),
        }

    def _summarize_semantic_validation_issues(self, recent_logs: List[Dict[str, Any]]) -> Dict[str, Any]:
        issue_counts: Dict[str, int] = {}
        model_failure_counts: Dict[str, int] = {}
        model_total_counts: Dict[str, int] = {}
        validation_failures = 0
        for row in recent_logs:
            execution_summary = row.get("execution_summary") or {}
            ai_usage = execution_summary.get("ai_usage") or {}
            model_name = str(ai_usage.get("model_name") or "unknown").strip() or "unknown"
            if ai_usage:
                model_total_counts[model_name] = model_total_counts.get(model_name, 0) + 1
            if not ai_usage.get("semantic_validation_error"):
                continue
            validation_failures += 1
            model_failure_counts[model_name] = model_failure_counts.get(model_name, 0) + 1
            for issue in ai_usage.get("issues") or []:
                key = str(issue).strip()
                if not key:
                    continue
                issue_counts[key] = issue_counts.get(key, 0) + 1
        top_issues = [
            {"issue": issue, "count": count}
            for issue, count in sorted(issue_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        ]
        top_models = []
        for model_name, failure_count in sorted(model_failure_counts.items(), key=lambda item: (-item[1], item[0]))[:5]:
            total_count = model_total_counts.get(model_name, failure_count)
            failure_rate = (failure_count / total_count) if total_count > 0 else 0.0
            top_models.append(
                {
                    "model_name": model_name,
                    "count": failure_count,
                    "total_calls": total_count,
                    "failure_rate": round(failure_rate, 4),
                }
            )
        return {
            "total_calls": sum(model_total_counts.values()),
            "failure_count": validation_failures,
            "top_issues": top_issues,
            "top_models": top_models,
        }
