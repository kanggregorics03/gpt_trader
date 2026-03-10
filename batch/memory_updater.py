import json
import math
import os
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Literal

from dotenv import load_dotenv
from loguru import logger
from pydantic import BaseModel, Field

from ai.llm_provider import (
    create_llm_provider,
    normalize_provider_name,
    resolve_api_key,
    resolve_base_url,
    resolve_memory_model_name,
)
from core.models import MemoryCondition
from db.state_store import StateStore

load_dotenv()


class MemoryLesson(BaseModel):
    rule: str = Field(description="Concrete lesson or anti-pattern in English.")
    regime: str = Field(description="Applicable regime such as trending_up or high_volatility.")
    category: Literal["lesson", "anti_pattern", "risk_control", "opportunity"]
    support_count: int = Field(ge=1)
    evidence_journal_ids: List[int] = Field(default_factory=list)
    hypothesis: bool = False
    apply_when: MemoryCondition = Field(
        description="Conditions for applying the lesson. Must include regime, volatility, and trend tags."
    )
    ignore_when: List[str] = Field(
        default_factory=list,
        description="Counterexamples or conditions where the lesson should be ignored.",
        min_length=1,
    )
    trigger_context: str = ""
    execution_context: str = ""
    trend_context: str = ""
    hold_quality: str = ""


class MemoryUpdater:
    def __init__(self, db_path: str = "data/gpt_trader.db"):
        self.db_path = db_path
        self.state_store = StateStore(db_path)
        self.provider_name = normalize_provider_name(os.getenv("MEMORY_PROVIDER"))
        self.shared_ai_api_key = os.getenv("AI_API_KEY")
        self.shared_ai_base_url = os.getenv("AI_BASE_URL")
        try:
            self.api_key = resolve_api_key(
                self.provider_name,
                explicit_api_key=os.getenv("MEMORY_API_KEY"),
                shared_api_key=self.shared_ai_api_key,
            )
            self.base_url = resolve_base_url(
                self.provider_name,
                explicit_base_url=os.getenv("MEMORY_BASE_URL"),
                shared_base_url=self.shared_ai_base_url,
            )
        except ValueError as exc:
            logger.error(str(exc))
            raise SystemExit(1)
        self.llm_provider = create_llm_provider(
            self.provider_name,
            api_key=self.api_key,
            base_url=self.base_url,
        )
        self.client = getattr(self.llm_provider, "client", None)
        self.model_name = self._resolve_model_name()

    def _resolve_model_name(self) -> str:
        provider_name = getattr(
            self,
            "provider_name",
            normalize_provider_name(os.getenv("MEMORY_PROVIDER")),
        )
        return resolve_memory_model_name(
            provider_name,
            explicit_model=os.getenv("MEMORY_MODEL"),
        )

    def _normalize_text(self, value: Any, *, default: str = "") -> str:
        if value is None:
            return default
        if isinstance(value, str):
            text = value.strip()
            return text or default
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, list):
            parts = [self._normalize_text(item, default="") for item in value]
            parts = [part for part in parts if part]
            return ", ".join(parts) or default
        if isinstance(value, dict):
            try:
                return json.dumps(value, sort_keys=True, ensure_ascii=True)
            except TypeError:
                return str(value)
        return str(value)

    def _normalize_string_list(self, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, list):
            normalized = [self._normalize_text(item, default="") for item in value]
            return [item for item in normalized if item]
        if isinstance(value, dict):
            normalized = [
                f"{key}: {self._normalize_text(item, default='unknown')}"
                for key, item in value.items()
                if self._normalize_text(item, default="")
            ]
            return normalized
        normalized = self._normalize_text(value, default="")
        return [normalized] if normalized else []

    def _coerce_bool(self, value: Any, *, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in {"true", "1", "yes", "y", "on"}:
            return True
        if text in {"false", "0", "no", "n", "off"}:
            return False
        return default

    def _coerce_int(self, value: Any, *, default: int = 0) -> int:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return default

    def _normalize_memory_condition_payload(self, payload: Any) -> Dict[str, List[str]]:
        if isinstance(payload, MemoryCondition):
            raw = payload.model_dump()
        elif isinstance(payload, dict):
            raw = dict(payload)
        else:
            raw = {}
        return {
            "regime": self._normalize_string_list(raw.get("regime")),
            "volatility": self._normalize_string_list(raw.get("volatility")),
            "trend": self._normalize_string_list(raw.get("trend")),
        }

    def _normalize_memory_lesson_payload(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        apply_when = self._normalize_memory_condition_payload(payload.get("apply_when"))
        evidence_journal_ids = [
            self._coerce_int(item)
            for item in (
                payload.get("evidence_journal_ids")
                if isinstance(payload.get("evidence_journal_ids"), list)
                else [payload.get("evidence_journal_ids")]
            )
            if item is not None and self._coerce_int(item, default=0) > 0
        ]
        category = self._normalize_text(payload.get("category"), default="").lower()
        if category not in {"lesson", "anti_pattern", "risk_control", "opportunity"}:
            if self._coerce_bool(payload.get("risk_control"), default=False):
                category = "risk_control"
            else:
                category = "lesson"
        ignore_when = self._normalize_string_list(payload.get("ignore_when"))
        if not ignore_when:
            ignore_when = ["Ignore when live evidence contradicts this lesson."]
        rule = self._normalize_text(
            payload.get("rule") or payload.get("title") or payload.get("lesson") or payload.get("summary"),
            default="Derived lesson from replay evidence.",
        )
        regime = self._normalize_text(payload.get("regime"), default="")
        if not regime:
            regime = apply_when["regime"][0] if apply_when["regime"] else "general"
        support_count = max(
            1,
            self._coerce_int(
                payload.get("support_count", payload.get("support", len(evidence_journal_ids) or 1)),
                default=len(evidence_journal_ids) or 1,
            ),
        )
        return {
            "rule": rule,
            "regime": regime,
            "category": category,
            "support_count": support_count,
            "evidence_journal_ids": evidence_journal_ids,
            "hypothesis": self._coerce_bool(payload.get("hypothesis"), default=False),
            "apply_when": apply_when,
            "ignore_when": ignore_when,
            "trigger_context": self._normalize_text(payload.get("trigger_context"), default=""),
            "execution_context": self._normalize_text(payload.get("execution_context"), default=""),
            "trend_context": self._normalize_text(payload.get("trend_context"), default=""),
            "hold_quality": self._normalize_text(payload.get("hold_quality"), default=""),
        }

    def _fetch_window_entries(self, window_hours: int = 24) -> List[Dict[str, Any]]:
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
        return self.state_store.get_journal_entries_since(since)

    def _fetch_symbol_window_entries(self, window_hours: int = 24) -> List[Dict[str, Any]]:
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
        fetcher = getattr(self.state_store, "get_symbol_journal_entries_since", None)
        if callable(fetcher):
            return fetcher(since)
        return []

    def _fetch_trade_case_window_entries(self, window_hours: int = 24) -> List[Dict[str, Any]]:
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
        fetcher = getattr(self.state_store, "get_trade_cases_since", None)
        if callable(fetcher):
            return fetcher(since)
        return []

    def _fetch_retrieval_window_entries(self, window_hours: int = 24) -> List[Dict[str, Any]]:
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
        fetcher = getattr(self.state_store, "get_retrieval_evaluations_since", None)
        if callable(fetcher):
            return fetcher(since)
        return []

    def _fetch_replay_window_entries(self, window_hours: int = 24) -> List[Dict[str, Any]]:
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
        fetcher = getattr(self.state_store, "get_replay_runs_since", None)
        if callable(fetcher):
            return fetcher(since)
        return []

    def _fetch_execution_policy_window_entries(self, window_hours: int = 24) -> List[Dict[str, Any]]:
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
        fetcher = getattr(self.state_store, "get_execution_policy_evaluations_since", None)
        if callable(fetcher):
            return fetcher(since)
        return []

    def _fetch_shadow_decision_window_entries(self, window_hours: int = 24) -> List[Dict[str, Any]]:
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
        fetcher = getattr(self.state_store, "get_shadow_decisions_since", None)
        if callable(fetcher):
            return fetcher(since)
        return []

    def _filter_learning_eligible_journals(
        self,
        journals: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        eligible: List[Dict[str, Any]] = []
        excluded: List[Dict[str, Any]] = []
        for row in journals:
            if StateStore.is_learning_eligible_audit_record(row):
                eligible.append(row)
            else:
                excluded.append(row)
        return eligible, excluded

    def _filter_audit_linked_rows(
        self,
        rows: List[Dict[str, Any]] | None,
        *,
        audit_ids: set[int],
        audit_key: str = "audit_log_id",
    ) -> List[Dict[str, Any]]:
        if not rows or not audit_ids:
            return []
        filtered: List[Dict[str, Any]] = []
        for row in rows:
            audit_id = int(row.get(audit_key) or 0)
            if audit_id and audit_id in audit_ids:
                filtered.append(row)
        return filtered

    def _filter_trade_cases_for_learning(
        self,
        trade_cases: List[Dict[str, Any]] | None,
        *,
        audit_ids: set[int],
    ) -> List[Dict[str, Any]]:
        if not trade_cases:
            return []
        if not audit_ids:
            return []
        filtered: List[Dict[str, Any]] = []
        for row in trade_cases:
            entry_audit_id = int(row.get("entry_audit_log_id") or 0)
            exit_audit_id = int(row.get("exit_audit_log_id") or 0)
            if entry_audit_id and entry_audit_id in audit_ids:
                filtered.append(row)
                continue
            if exit_audit_id and exit_audit_id in audit_ids:
                filtered.append(row)
                continue
            if not entry_audit_id and not exit_audit_id:
                filtered.append(row)
        return filtered

    def _build_stats_package(
        self,
        journals: List[Dict[str, Any]],
        window_hours: int,
        symbol_journals: List[Dict[str, Any]] | None = None,
        trade_cases: List[Dict[str, Any]] | None = None,
        retrieval_evaluations: List[Dict[str, Any]] | None = None,
        replay_runs: List[Dict[str, Any]] | None = None,
        execution_policy_evaluations: List[Dict[str, Any]] | None = None,
        shadow_decisions: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        symbol_journals = symbol_journals or []
        trade_cases = trade_cases or []
        retrieval_evaluations = retrieval_evaluations or []
        replay_runs = replay_runs or []
        execution_policy_evaluations = execution_policy_evaluations or []
        shadow_decisions = shadow_decisions or []
        ledger_stats = self.state_store.get_position_ledger_stats(hours=window_hours)
        symbol_report = self.state_store.get_symbol_performance_report(hours=window_hours)
        daily_report = self.state_store.get_daily_ledger_report(days=max(1, round(window_hours / 24)))
        pnl_rows = [row for row in journals if row.get("estimated_pnl_pct") is not None]
        wins = [row for row in pnl_rows if float(row["estimated_pnl_pct"]) > 0]
        losses = [row for row in pnl_rows if float(row["estimated_pnl_pct"]) < 0]
        flat = [row for row in pnl_rows if float(row["estimated_pnl_pct"]) == 0]

        decision_breakdown: Dict[str, int] = {}
        reject_reason_counts: Dict[str, int] = {}
        evidence_signal_counts: Dict[str, int] = {}
        winning_evidence_signals: Dict[str, int] = {}
        losing_evidence_signals: Dict[str, int] = {}
        for row in journals:
            decision = row.get("decision") or "UNKNOWN"
            decision_breakdown[decision] = decision_breakdown.get(decision, 0) + 1
            risk_reason = (row.get("risk_reason") or "").strip()
            if risk_reason and risk_reason not in ("Approved", "Safe action."):
                reject_reason_counts[risk_reason] = reject_reason_counts.get(risk_reason, 0) + 1
            signal_keys = self._extract_evidence_keys(row.get("rationale_json") or [])
            for signal_key in signal_keys:
                evidence_signal_counts[signal_key] = evidence_signal_counts.get(signal_key, 0) + 1
                estimated_pnl_pct = row.get("estimated_pnl_pct")
                if estimated_pnl_pct is None:
                    continue
                if float(estimated_pnl_pct) > 0:
                    winning_evidence_signals[signal_key] = winning_evidence_signals.get(signal_key, 0) + 1
                elif float(estimated_pnl_pct) < 0:
                    losing_evidence_signals[signal_key] = losing_evidence_signals.get(signal_key, 0) + 1

        avg_pnl = 0.0
        if pnl_rows:
            avg_pnl = sum(float(row["estimated_pnl_pct"]) for row in pnl_rows) / len(pnl_rows)

        top_losses = sorted(
            losses,
            key=lambda row: float(row["estimated_pnl_pct"]),
        )[:5]
        top_wins = sorted(
            wins,
            key=lambda row: float(row["estimated_pnl_pct"]),
            reverse=True,
        )[:5]
        evidence_quality_patterns = self._build_evidence_quality_patterns(pnl_rows)
        symbol_evidence_signal_counts = self._build_symbol_evidence_signal_counts(symbol_journals)
        symbol_candidate_lessons = self._build_symbol_candidate_lessons(symbol_journals)
        trade_case_patterns = self._build_trade_case_patterns(trade_cases)
        trade_case_clusters = self._build_trade_case_clusters(trade_cases)
        trade_case_cluster_evaluation = self._summarize_trade_case_cluster_quality(trade_case_clusters)
        retrieval_evaluation_summary = self._build_retrieval_evaluation_summary(retrieval_evaluations)
        retrieval_counterfactual_summary = self._build_retrieval_counterfactual_summary(replay_runs)
        retrieval_counterfactual_summary = self._apply_retrieval_outcome_counterfactual_summary(
            retrieval_counterfactual_summary,
            replay_runs=replay_runs,
            retrieval_evaluations=retrieval_evaluations,
        )
        hold_opportunity_summary = self._build_hold_opportunity_summary(replay_runs, journals=journals)
        trading_balance_summary = self._build_trading_balance_summary(
            journals=journals,
            trade_cases=trade_cases,
            hold_opportunity_summary=hold_opportunity_summary,
        )
        confidence_calibration_summary = self._build_confidence_calibration_summary(journals)
        execution_policy_summary = self._build_execution_policy_summary(execution_policy_evaluations)
        execution_policy_recommendations = self._build_execution_policy_recommendations(
            execution_policy_evaluations
        )
        shadow_policy_summary = self._build_shadow_policy_summary(shadow_decisions)
        trade_case_candidate_lessons = self._build_trade_case_candidate_lessons(trade_case_patterns)
        trade_case_cluster_candidate_lessons = self._build_trade_case_cluster_candidate_lessons(trade_case_clusters)
        merged_candidate_lessons = self._merge_candidate_lesson_dicts(
            self._build_candidate_lessons(
                journals=journals,
                winning_evidence_signals=winning_evidence_signals,
                losing_evidence_signals=losing_evidence_signals,
                evidence_quality_patterns=evidence_quality_patterns,
            ),
            self._build_undertrading_candidate_lessons(
                hold_opportunity_summary=hold_opportunity_summary,
                journals=journals,
            ),
            self._build_overtrading_candidate_lessons(
                trading_balance_summary=trading_balance_summary,
                journals=journals,
            ),
            symbol_candidate_lessons,
            trade_case_candidate_lessons,
            trade_case_cluster_candidate_lessons,
        )
        candidate_policies = self._build_candidate_policies(
            stats_package_seed={
                "candidate_lessons": merged_candidate_lessons,
                "trade_case_patterns": trade_case_patterns,
            },
            trade_cases=trade_cases,
            execution_policy_recommendations=execution_policy_recommendations,
        )

        return {
            "window_hours": window_hours,
            "journal_count": len(journals),
            "symbol_journal_count": len(symbol_journals),
            "trade_case_count": len(trade_cases),
            "retrieval_evaluation_count": len(retrieval_evaluations),
            "replay_run_count": len(replay_runs),
            "execution_policy_evaluation_count": len(execution_policy_evaluations),
            "shadow_decision_count": len(shadow_decisions),
            "closed_trade_case_count": len([case for case in trade_cases if str(case.get("status") or "") == "closed"]),
            "evaluated_trade_count": len(pnl_rows),
            "win_count": len(wins),
            "loss_count": len(losses),
            "flat_count": len(flat),
            "win_rate": round((len(wins) / len(pnl_rows) * 100), 2) if pnl_rows else 0.0,
            "avg_estimated_pnl_pct": round(avg_pnl, 4),
            "ledger_stats": ledger_stats,
            "symbol_performance": symbol_report[:10],
            "daily_ledger": daily_report[:10],
            "decision_breakdown": decision_breakdown,
            "risk_reason_counts": reject_reason_counts,
            "evidence_signal_counts": evidence_signal_counts,
            "winning_evidence_signals": winning_evidence_signals,
            "losing_evidence_signals": losing_evidence_signals,
            "symbol_evidence_signal_counts": symbol_evidence_signal_counts,
            "evidence_quality_patterns": evidence_quality_patterns,
            "trade_case_patterns": trade_case_patterns,
            "trade_case_clusters": trade_case_clusters,
            "trade_case_cluster_evaluation": trade_case_cluster_evaluation,
            "retrieval_evaluation_summary": retrieval_evaluation_summary,
            "retrieval_counterfactual_summary": retrieval_counterfactual_summary,
            "hold_opportunity_summary": hold_opportunity_summary,
            "trading_balance_summary": trading_balance_summary,
            "confidence_calibration_summary": confidence_calibration_summary,
            "execution_policy_summary": execution_policy_summary,
            "execution_policy_recommendations": execution_policy_recommendations,
            "shadow_policy_summary": shadow_policy_summary,
            "candidate_policies": candidate_policies,
            "candidate_lessons": merged_candidate_lessons,
            "top_losses": [
                {
                    "id": row["id"],
                    "audit_log_id": row.get("audit_log_id"),
                    "decision": row["decision"],
                    "estimated_pnl_pct": row["estimated_pnl_pct"],
                    "ledger_net_realized_pnl": row.get("ledger_net_realized_pnl"),
                    "ledger_fee_cost": row.get("ledger_fee_cost"),
                    "rationale_json": row.get("rationale_json") or [],
                    "evidence_summary": self._build_structured_evidence_summary(row.get("rationale_json") or []),
                    "risk_reason": row["risk_reason"],
                    "risk_mode": row.get("risk_mode"),
                    "market_regime": self._extract_market_regime_summary(row.get("market_state") or {}),
                }
                for row in top_losses
            ],
            "top_wins": [
                {
                    "id": row["id"],
                    "audit_log_id": row.get("audit_log_id"),
                    "decision": row["decision"],
                    "estimated_pnl_pct": row["estimated_pnl_pct"],
                    "ledger_net_realized_pnl": row.get("ledger_net_realized_pnl"),
                    "ledger_fee_cost": row.get("ledger_fee_cost"),
                    "rationale_json": row.get("rationale_json") or [],
                    "evidence_summary": self._build_structured_evidence_summary(row.get("rationale_json") or []),
                    "risk_reason": row["risk_reason"],
                    "risk_mode": row.get("risk_mode"),
                    "market_regime": self._extract_market_regime_summary(row.get("market_state") or {}),
                }
                for row in top_wins
            ],
            "recent_examples": [
                {
                    "id": row["id"],
                    "audit_log_id": row.get("audit_log_id"),
                    "decision": row["decision"],
                    "estimated_pnl_pct": row["estimated_pnl_pct"],
                    "ledger_net_realized_pnl": row.get("ledger_net_realized_pnl"),
                    "ledger_fee_cost": row.get("ledger_fee_cost"),
                    "loss_signal": row["loss_signal"],
                    "rationale_json": row.get("rationale_json") or [],
                    "evidence_summary": self._build_structured_evidence_summary(row.get("rationale_json") or []),
                    "risk_reason": row["risk_reason"],
                    "risk_mode": row.get("risk_mode"),
                    "market_regime": self._extract_market_regime_summary(row.get("market_state") or {}),
                }
                for row in journals[-10:]
            ],
            "recent_symbol_examples": [
                {
                    "id": row.get("id"),
                    "audit_log_id": row.get("audit_log_id"),
                    "symbol": row.get("symbol"),
                    "decision": row.get("decision"),
                    "risk_mode": row.get("risk_mode"),
                    "triggered": bool(row.get("triggered")),
                    "risk_triggered": bool(row.get("risk_triggered")),
                    "estimated_pnl_pct": row.get("estimated_pnl_pct"),
                    "net_realized_pnl": row.get("net_realized_pnl"),
                    "evidence_signals": row.get("evidence_signals") or [],
                }
                for row in symbol_journals[-20:]
            ],
            "recent_trade_cases": [
                {
                    "trade_case_id": row.get("trade_case_id"),
                    "symbol": row.get("symbol"),
                    "status": row.get("status"),
                    "risk_mode": row.get("risk_mode"),
                    "estimated_pnl_pct": row.get("estimated_pnl_pct"),
                    "realized_pnl": row.get("realized_pnl"),
                    "fee_cost": row.get("fee_cost"),
                    "holding_minutes": row.get("holding_minutes"),
                    "observed_mfe_pct": row.get("observed_mfe_pct"),
                    "observed_mae_pct": row.get("observed_mae_pct"),
                    "slippage_bps": row.get("slippage_bps"),
                    "thesis_failure_reason": row.get("thesis_failure_reason"),
                    "evidence_signals": row.get("evidence_signals") or [],
                }
                for row in trade_cases[-20:]
            ],
            "recent_retrieval_evaluations": [
                {
                    "audit_log_id": row.get("audit_log_id"),
                    "context_type": row.get("context_type"),
                    "trade_case_id": row.get("trade_case_id"),
                    "case_symbol": row.get("case_symbol"),
                    "matched_symbol": row.get("matched_symbol"),
                    "decision": row.get("decision"),
                    "decision_symbol_aligned": bool(row.get("decision_symbol_aligned")),
                    "estimated_pnl_pct": row.get("estimated_pnl_pct"),
                    "similarity": row.get("similarity"),
                    "final_score": row.get("final_score"),
                    "retrieval_source": row.get("retrieval_source"),
                    "matched_band_count": row.get("matched_band_count"),
                }
                for row in retrieval_evaluations[-20:]
            ],
            "recent_retrieval_counterfactuals": [
                {
                    "audit_log_id": row.get("audit_log_id"),
                    "decision_changed": bool(((row.get("payload") or {}).get("retrieval_counterfactual") or {}).get("with_vs_without_comparison", {}).get("decision_changed")),
                    "side_changed": bool(((row.get("payload") or {}).get("retrieval_counterfactual") or {}).get("with_vs_without_comparison", {}).get("side_changed")),
                    "confidence_delta": ((row.get("payload") or {}).get("retrieval_counterfactual") or {}).get("with_vs_without_comparison", {}).get("confidence_delta"),
                    "size_delta": ((row.get("payload") or {}).get("retrieval_counterfactual") or {}).get("with_vs_without_comparison", {}).get("size_delta"),
                }
                for row in replay_runs[-20:]
                if ((row.get("payload") or {}).get("retrieval_counterfactual") or {})
            ],
        }

    def _average(self, values: List[float]) -> float:
        clean = [float(value) for value in values if value is not None]
        if not clean:
            return 0.0
        return float(sum(clean) / len(clean))

    def _linear_slope(self, pairs: List[tuple[float, float]], clip: float = 2.0) -> float:
        clean = [(float(x), float(y)) for x, y in pairs if x is not None and y is not None]
        if len(clean) < 2:
            return 0.0
        x_mean = self._average([x for x, _ in clean])
        y_mean = self._average([y for _, y in clean])
        variance = sum((x - x_mean) ** 2 for x, _ in clean)
        if variance <= 1e-9:
            return 0.0
        covariance = sum((x - x_mean) * (y - y_mean) for x, y in clean)
        return round(max(-clip, min(clip, covariance / variance)), 4)

    def _classify_journal_learning_bucket(self, row: Dict[str, Any]) -> str:
        decision = str(row.get("decision") or "HOLD").strip().upper()
        rationale_items = row.get("rationale_json") or row.get("rationale_items") or []
        hold_quality = ""
        if rationale_items and isinstance(rationale_items, list):
            for item in rationale_items:
                hold_quality = str((item or {}).get("hold_quality") or "").strip().lower()
                if hold_quality:
                    break
        if hold_quality == "good_hold":
            return "good_hold"
        if hold_quality in {"missed_opportunity_hold", "probe_only_opportunity_hold"}:
            return "missed_opportunity_hold"
        if bool(row.get("loss_signal")) or decision in {"EXIT", "REDUCE", "CANCEL", "REPLACE_ENTRY"}:
            return "risk_outcome"
        if decision in {"BUY", "SELL"}:
            return "entry_action"
        if decision == "HOLD":
            return "hold"
        return "other_action"

    def _build_recent_journal_reranker_profile(self, journals: List[Dict[str, Any]]) -> Dict[str, Any]:
        targets: List[float] = []
        bucket_targets: Dict[str, List[float]] = {}
        decision_targets: Dict[str, List[float]] = {}
        loss_targets = {True: [], False: []}
        pnl_presence_targets = {True: [], False: []}
        for row in journals or []:
            target = self._coerce_float(row.get("estimated_pnl_pct")) or 0.0
            bucket = self._classify_journal_learning_bucket(row)
            decision = str(row.get("decision") or "HOLD").strip().upper()
            targets.append(target)
            bucket_targets.setdefault(bucket, []).append(target)
            decision_targets.setdefault(decision, []).append(target)
            loss_targets[bool(row.get("loss_signal"))].append(target)
            pnl_presence_targets[row.get("estimated_pnl_pct") is not None].append(target)
        baseline = self._average(targets)
        return {
            "model_kind": "bucket_bias_profile_v1",
            "sample_count": len(targets),
            "baseline_utility": round(baseline, 4),
            "bucket_weights": {
                key: round(self._average(values) - baseline, 4)
                for key, values in sorted(bucket_targets.items())
            },
            "decision_weights": {
                key: round(self._average(values) - baseline, 4)
                for key, values in sorted(decision_targets.items())
            },
            "loss_signal_weight": round(self._average(loss_targets[True]) - baseline, 4) if loss_targets[True] else 0.0,
            "pnl_presence_weight": round(self._average(pnl_presence_targets[True]) - baseline, 4) if pnl_presence_targets[True] else 0.0,
        }

    def _build_similar_case_reranker_profile(
        self,
        retrieval_evaluations: List[Dict[str, Any]],
        replay_runs: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        counterfactual_bonus_by_audit: Dict[int, float] = {}
        for replay_row in replay_runs or []:
            audit_id = int(replay_row.get("audit_log_id") or 0)
            if not audit_id:
                continue
            comparison = (((replay_row.get("payload") or {}).get("retrieval_counterfactual") or {}).get("with_vs_without_comparison") or {})
            if comparison.get("with_retrieval_better") is True:
                counterfactual_bonus_by_audit[audit_id] = 0.25
            elif comparison.get("without_retrieval_better") is True:
                counterfactual_bonus_by_audit[audit_id] = -0.25
            else:
                counterfactual_bonus_by_audit.setdefault(audit_id, 0.0)

        targets: List[float] = []
        similarity_pairs: List[tuple[float, float]] = []
        final_score_pairs: List[tuple[float, float]] = []
        band_pairs: List[tuple[float, float]] = []
        source_targets: Dict[str, List[float]] = {}
        failure_reason_targets = {True: [], False: []}
        negative_case_targets = {True: [], False: []}
        for row in retrieval_evaluations or []:
            outcome = self._build_retrieval_outcome_metrics(row)
            target = float(outcome.get("utility_score") or 0.0) + float(counterfactual_bonus_by_audit.get(int(row.get("audit_log_id") or 0), 0.0))
            targets.append(target)
            similarity = self._coerce_float(row.get("similarity"))
            final_score = self._coerce_float(row.get("final_score"))
            matched_bands = self._coerce_float(row.get("matched_band_count"))
            if similarity is not None:
                similarity_pairs.append((similarity, target))
            if final_score is not None:
                final_score_pairs.append((final_score, target))
            if matched_bands is not None:
                band_pairs.append((matched_bands, target))
            source = str(row.get("retrieval_source") or "full_scan").strip().lower() or "full_scan"
            source_targets.setdefault(source, []).append(target)
            payload = row.get("payload") or {}
            has_failure = bool(str(payload.get("thesis_failure_reason") or "").strip())
            failure_reason_targets[has_failure].append(target)
            pnl = self._coerce_float(row.get("outcome_realized_pnl"))
            if pnl is None:
                pnl = self._coerce_float(row.get("outcome_estimated_pnl_pct"))
            negative_case_targets[bool(pnl is not None and pnl < 0)].append(target)
        baseline = self._average(targets)
        return {
            "model_kind": "linear_profile_v1",
            "sample_count": len(targets),
            "baseline_utility": round(baseline, 4),
            "similarity_weight": self._linear_slope(similarity_pairs, clip=1.5),
            "final_score_weight": self._linear_slope(final_score_pairs, clip=0.75),
            "matched_band_weight": self._linear_slope(band_pairs, clip=0.5),
            "source_weights": {
                key: round(self._average(values) - baseline, 4)
                for key, values in sorted(source_targets.items())
            },
            "failure_reason_weight": round(self._average(failure_reason_targets[True]) - baseline, 4) if failure_reason_targets[True] else 0.0,
            "negative_outcome_weight": round(self._average(negative_case_targets[True]) - baseline, 4) if negative_case_targets[True] else 0.0,
        }

    def _build_shadow_model_dataset(
        self,
        journals: List[Dict[str, Any]],
        replay_runs: List[Dict[str, Any]],
        shadow_decisions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        replay_by_audit: Dict[int, List[Dict[str, Any]]] = {}
        shadow_by_audit: Dict[int, List[Dict[str, Any]]] = {}
        for row in replay_runs or []:
            audit_id = int(row.get("audit_log_id") or 0)
            if audit_id:
                replay_by_audit.setdefault(audit_id, []).append(row)
        for row in shadow_decisions or []:
            audit_id = int(row.get("audit_log_id") or 0)
            if audit_id:
                shadow_by_audit.setdefault(audit_id, []).append(row)

        dataset: List[Dict[str, Any]] = []
        for row in journals or []:
            audit_id = int(row.get("audit_log_id") or 0)
            replay_rows = replay_by_audit.get(audit_id, [])
            shadow_rows = shadow_by_audit.get(audit_id, [])
            estimated_pnl = self._coerce_float(row.get("estimated_pnl_pct")) or 0.0
            hold_quality = self._classify_journal_learning_bucket(row)
            replay_changed = any(bool((item.get("payload") or {}).get("retrieval_counterfactual")) for item in replay_rows)
            shadow_outcomes = [self._coerce_float(item.get("outcome_estimated_pnl_pct")) for item in shadow_rows]
            shadow_outcomes = [value for value in shadow_outcomes if value is not None]
            dataset.append(
                {
                    "audit_log_id": audit_id,
                    "journal_id": int(row.get("id") or 0),
                    "decision": str(row.get("decision") or "HOLD"),
                    "risk_mode": str(row.get("risk_mode") or ""),
                    "confidence": float(row.get("confidence") or 0.0),
                    "estimated_pnl_pct": round(estimated_pnl, 4),
                    "hold_quality_bucket": hold_quality,
                    "evidence_count": len(self._extract_evidence_keys(row.get("rationale_json") or [])),
                    "replay_counterfactual_available": replay_changed,
                    "shadow_decision_count": len(shadow_rows),
                    "shadow_avg_outcome_estimated_pnl_pct": round(self._average(shadow_outcomes), 4) if shadow_outcomes else None,
                }
            )
        return dataset

    def _summarize_shadow_model_dataset(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        ordered = list(rows or [])
        if not ordered:
            return {
                "sample_count": 0,
                "train_count": 0,
                "holdout_count": 0,
                "avg_estimated_pnl_pct": 0.0,
                "holdout_avg_estimated_pnl_pct": None,
                "positive_rate": 0.0,
                "holdout_positive_rate": None,
            }
        split_index = max(1, int(math.floor(len(ordered) * 0.8)))
        if split_index >= len(ordered):
            split_index = max(1, len(ordered) - 1)
        train_rows = ordered[:split_index]
        holdout_rows = ordered[split_index:]
        holdout_pnls = [float(row.get("estimated_pnl_pct") or 0.0) for row in holdout_rows]
        return {
            "sample_count": len(ordered),
            "train_count": len(train_rows),
            "holdout_count": len(holdout_rows),
            "avg_estimated_pnl_pct": round(self._average([float(row.get("estimated_pnl_pct") or 0.0) for row in ordered]), 4),
            "holdout_avg_estimated_pnl_pct": round(self._average(holdout_pnls), 4) if holdout_rows else None,
            "positive_rate": round(sum(1 for row in ordered if float(row.get("estimated_pnl_pct") or 0.0) > 0) / max(1, len(ordered)), 4),
            "holdout_positive_rate": (
                round(sum(1 for row in holdout_rows if float(row.get("estimated_pnl_pct") or 0.0) > 0) / max(1, len(holdout_rows)), 4)
                if holdout_rows else None
            ),
        }

    def _shadow_dataset_export_path(self) -> Path:
        db_parent = Path(self.db_path).resolve().parent
        learning_dir = db_parent / "learning"
        learning_dir.mkdir(parents=True, exist_ok=True)
        return learning_dir / "shadow_decision_dataset.jsonl"

    def _export_shadow_model_dataset(self, rows: List[Dict[str, Any]]) -> str:
        path = self._shadow_dataset_export_path()
        with path.open("w", encoding="utf-8") as handle:
            for row in rows or []:
                handle.write(json.dumps(row, ensure_ascii=True) + "\n")
        return str(path)

    def _build_learning_liveness_payload(
        self,
        *,
        window_hours: int,
        replay_runs: List[Dict[str, Any]],
        shadow_dataset_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        now = datetime.now(UTC).replace(tzinfo=None)
        since = now - timedelta(hours=max(1, int(window_hours or 24)))
        replay_candidates = []
        if hasattr(self.state_store, "get_replay_batch_candidates"):
            replay_candidates = self.state_store.get_replay_batch_candidates(since=since, limit=500)
        replay_processed_rate = 1.0
        if replay_candidates:
            replay_processed_rate = min(1.0, len(replay_runs or []) / max(1, len(replay_candidates)))
        replay_heartbeat = self.state_store.get_learning_artifact("replay_batch_heartbeat") if hasattr(self.state_store, "get_learning_artifact") else {}
        replay_heartbeat_at = str(replay_heartbeat.get("updated_at") or "")
        stale_reasons: List[str] = []
        if not replay_heartbeat_at:
            stale_reasons.append("replay_batch_missing")
        if replay_candidates and replay_processed_rate < 0.5:
            stale_reasons.append("replay_coverage_low")
        if int(shadow_dataset_summary.get("holdout_count") or 0) <= 0:
            stale_reasons.append("shadow_holdout_missing")
        return {
            "window_hours": int(window_hours or 24),
            "last_memory_update_at": now.isoformat(),
            "last_replay_batch_at": replay_heartbeat_at or None,
            "replay_candidate_count_24h": len(replay_candidates),
            "replay_run_count_24h": len(replay_runs or []),
            "replay_processed_rate_24h": round(replay_processed_rate, 4),
            "shadow_dataset_sample_count": int(shadow_dataset_summary.get("sample_count") or 0),
            "shadow_dataset_holdout_count": int(shadow_dataset_summary.get("holdout_count") or 0),
            "stale_reasons": stale_reasons,
            "stale": bool(stale_reasons),
            "promotion_blocked": bool(stale_reasons),
        }

    def _learning_promotions_allowed(self) -> bool:
        if not hasattr(self.state_store, "get_learning_artifact"):
            return True
        artifact = self.state_store.get_learning_artifact("learning_liveness")
        payload = artifact.get("payload") or {}
        return not bool(payload.get("promotion_blocked"))

    def _build_symbol_evidence_signal_counts(self, symbol_journals: List[Dict[str, Any]]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in symbol_journals:
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            for signal in row.get("evidence_signals") or []:
                key = f"{symbol}:{signal}"
                counts[key] = counts.get(key, 0) + 1
        return counts

    def _resolve_symbol_row_pnl(self, row: Dict[str, Any]) -> float | None:
        estimated = self._coerce_float(row.get("estimated_pnl_pct"))
        if estimated is not None:
            return estimated
        net_realized = self._coerce_float(row.get("net_realized_pnl"))
        return net_realized

    def _build_symbol_candidate_lessons(self, symbol_journals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        outcomes: Dict[str, Dict[str, Any]] = {}
        for row in symbol_journals:
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            pnl = self._resolve_symbol_row_pnl(row)
            if pnl is None:
                continue
            evidence_signals = row.get("evidence_signals") or []
            for signal in evidence_signals:
                signal_key = f"{symbol}:{signal}"
                bucket = outcomes.setdefault(
                    signal_key,
                    {"wins": 0, "losses": 0, "journal_ids": []},
                )
                if pnl > 0:
                    bucket["wins"] += 1
                elif pnl < 0:
                    bucket["losses"] += 1
                journal_id = row.get("id")
                if isinstance(journal_id, int) and journal_id not in bucket["journal_ids"]:
                    bucket["journal_ids"].append(journal_id)

        candidates: List[Dict[str, Any]] = []
        for signal_key, bucket in sorted(outcomes.items()):
            wins = int(bucket.get("wins") or 0)
            losses = int(bucket.get("losses") or 0)
            support = wins + losses
            if support < 2:
                continue
            symbol, signal = signal_key.split(":", 1)
            signal_name, timeframe = (signal.split("@", 1) + ["context"])[:2] if "@" in signal else (signal, "context")
            if wins > losses:
                candidates.append(
                    {
                        "rule": (
                            f"For {symbol}, favor setups with {signal_name} on {timeframe} when the same "
                            "symbol-level evidence repeatedly aligns with positive outcomes."
                        ),
                        "regime": "symbol_specific_general",
                        "category": "opportunity",
                        "support_count": support,
                        "evidence_journal_ids": (bucket.get("journal_ids") or [])[:10],
                        "hypothesis": support < 3,
                        "apply_when": {
                            "regime": ["symbol_specific_general"],
                            "volatility": ["unknown"],
                            "trend": ["mixed"],
                        },
                        "ignore_when": [
                            f"Ignore for {symbol} when {signal_name} on {timeframe} appears without supporting context."
                        ],
                        "seed_signal": signal_key,
                        "seed_origin": "symbol_journal_candidate",
                    }
                )
            elif losses > wins:
                candidates.append(
                    {
                        "rule": (
                            f"For {symbol}, avoid setups dominated by {signal_name} on {timeframe} when the same "
                            "symbol-level evidence repeatedly aligns with losses."
                        ),
                        "regime": "symbol_specific_general",
                        "category": "anti_pattern",
                        "support_count": support,
                        "evidence_journal_ids": (bucket.get("journal_ids") or [])[:10],
                        "hypothesis": support < 3,
                        "apply_when": {
                            "regime": ["symbol_specific_general"],
                            "volatility": ["unknown"],
                            "trend": ["mixed"],
                        },
                        "ignore_when": [
                            f"Ignore for {symbol} when broader evidence quality is strong and risk triggers are absent."
                        ],
                        "seed_signal": signal_key,
                        "seed_origin": "symbol_journal_candidate",
                    }
                )
        return candidates[:10]

    def _build_trade_case_patterns(self, trade_cases: List[Dict[str, Any]]) -> Dict[str, Any]:
        closed_cases = [row for row in trade_cases if str(row.get("status") or "") == "closed"]
        failure_reason_counts: Dict[str, Dict[str, Any]] = {}
        signal_failure_counts: Dict[str, Dict[str, Any]] = {}
        avg_slippage_bps = 0.0
        avg_mfe_pct = 0.0
        avg_mae_pct = 0.0
        counted_slippage = counted_mfe = counted_mae = 0

        for row in closed_cases:
            trade_case_id = row.get("trade_case_id")
            failure_reason = str(row.get("thesis_failure_reason") or "").strip()
            estimated_pnl_pct = self._coerce_float(row.get("estimated_pnl_pct"))
            if failure_reason:
                bucket = failure_reason_counts.setdefault(
                    failure_reason,
                    {"count": 0, "loss_count": 0, "symbols": set(), "trade_case_ids": []},
                )
                bucket["count"] += 1
                if estimated_pnl_pct is not None and estimated_pnl_pct < 0:
                    bucket["loss_count"] += 1
                symbol = str(row.get("symbol") or "").strip()
                if symbol:
                    bucket["symbols"].add(symbol)
                if trade_case_id and trade_case_id not in bucket["trade_case_ids"]:
                    bucket["trade_case_ids"].append(trade_case_id)

            for signal in row.get("evidence_signals") or []:
                signal_key = f"{row.get('symbol')}:{signal}"
                bucket = signal_failure_counts.setdefault(
                    signal_key,
                    {"count": 0, "loss_count": 0, "trade_case_ids": []},
                )
                bucket["count"] += 1
                if estimated_pnl_pct is not None and estimated_pnl_pct < 0:
                    bucket["loss_count"] += 1
                if trade_case_id and trade_case_id not in bucket["trade_case_ids"]:
                    bucket["trade_case_ids"].append(trade_case_id)

            slippage_bps = self._coerce_float(row.get("slippage_bps"))
            if slippage_bps is not None:
                avg_slippage_bps += slippage_bps
                counted_slippage += 1
            observed_mfe_pct = self._coerce_float(row.get("observed_mfe_pct"))
            if observed_mfe_pct is not None:
                avg_mfe_pct += observed_mfe_pct
                counted_mfe += 1
            observed_mae_pct = self._coerce_float(row.get("observed_mae_pct"))
            if observed_mae_pct is not None:
                avg_mae_pct += observed_mae_pct
                counted_mae += 1

        return {
            "closed_case_count": len(closed_cases),
            "failure_reason_counts": {
                reason: {
                    "count": data["count"],
                    "loss_count": data["loss_count"],
                    "symbols": sorted(data["symbols"]),
                    "trade_case_ids": data["trade_case_ids"][:10],
                }
                for reason, data in failure_reason_counts.items()
            },
            "signal_failure_counts": signal_failure_counts,
            "avg_slippage_bps": round(avg_slippage_bps / counted_slippage, 4) if counted_slippage else 0.0,
            "avg_observed_mfe_pct": round(avg_mfe_pct / counted_mfe, 4) if counted_mfe else 0.0,
            "avg_observed_mae_pct": round(avg_mae_pct / counted_mae, 4) if counted_mae else 0.0,
        }

    def _encode_trade_case_trend(self, trend_alignment: str) -> float:
        trend = str(trend_alignment or "").strip().lower()
        if trend in {"aligned_up", "bullish", "up"}:
            return 1.0
        if trend in {"aligned_down", "bearish", "down"}:
            return -1.0
        return 0.0

    def _encode_trade_case_volatility(self, volatility_regime: str) -> float:
        volatility = str(volatility_regime or "").strip().lower()
        if volatility in {"high", "elevated"}:
            return 1.0
        if volatility in {"low", "compressed"}:
            return -1.0
        return 0.0

    def _extract_trade_case_cluster_features(self, row: Dict[str, Any]) -> Dict[str, Any]:
        payload = row.get("payload") or {}
        feature_snapshot = payload.get("feature_snapshot") or {}
        regime_tags = payload.get("regime_tags") or []
        trend_alignment = "unknown"
        volatility_regime = "unknown"
        for tag in regime_tags:
            tag_text = str(tag or "")
            if tag_text.startswith("trend:") and tag_text.split(":", 1)[1]:
                trend_alignment = tag_text.split(":", 1)[1]
            if tag_text.startswith("volatility:") and tag_text.split(":", 1)[1]:
                volatility_regime = tag_text.split(":", 1)[1]
        vector = [
            self._coerce_float(feature_snapshot.get("spread_percentile_1h")) or 0.0,
            self._coerce_float(feature_snapshot.get("oi_change_15m")) or 0.0,
            self._coerce_float(feature_snapshot.get("volume_ratio_15m")) or 0.0,
            self._coerce_float(feature_snapshot.get("distance_to_invalid_level")) or 0.0,
            self._coerce_float(row.get("slippage_bps")) or 0.0,
            self._coerce_float(row.get("observed_mfe_pct")) or 0.0,
            self._coerce_float(row.get("observed_mae_pct")) or 0.0,
            self._encode_trade_case_trend(trend_alignment),
            self._encode_trade_case_volatility(volatility_regime),
        ]
        return {
            "vector": vector,
            "symbol": str(row.get("symbol") or "UNKNOWN"),
            "trend_alignment": trend_alignment,
            "volatility_regime": volatility_regime,
            "failure_reason": str(row.get("thesis_failure_reason") or "unknown"),
            "estimated_pnl_pct": self._coerce_float(row.get("estimated_pnl_pct")),
            "realized_pnl": self._coerce_float(row.get("realized_pnl")),
            "slippage_bps": self._coerce_float(row.get("slippage_bps")) or 0.0,
            "observed_mfe_pct": self._coerce_float(row.get("observed_mfe_pct")) or 0.0,
            "observed_mae_pct": self._coerce_float(row.get("observed_mae_pct")) or 0.0,
        }

    def _standardize_trade_case_vectors(self, rows: List[Dict[str, Any]]) -> List[List[float]]:
        extracted = [self._extract_trade_case_cluster_features(row) for row in rows]
        if not extracted:
            return []
        width = len(extracted[0]["vector"])
        means = []
        stds = []
        for index in range(width):
            values = [item["vector"][index] for item in extracted]
            mean = sum(values) / len(values)
            variance = sum((value - mean) ** 2 for value in values) / max(1, len(values))
            std = math.sqrt(variance) or 1.0
            means.append(mean)
            stds.append(std)
        standardized = []
        for item in extracted:
            standardized.append(
                [
                    (item["vector"][index] - means[index]) / stds[index]
                    if stds[index] > 0
                    else 0.0
                    for index in range(width)
                ]
            )
        return standardized

    def _euclidean_distance(self, left: List[float], right: List[float]) -> float:
        width = max(len(left), len(right))
        if width == 0:
            return 0.0
        padded_left = list(left) + ([0.0] * (width - len(left)))
        padded_right = list(right) + ([0.0] * (width - len(right)))
        return math.sqrt(sum((a - b) ** 2 for a, b in zip(padded_left, padded_right)))

    def _trade_case_cluster_sort_key(self, row: Dict[str, Any]) -> tuple[str, str]:
        return (
            str(row.get("closed_at") or row.get("opened_at") or ""),
            str(row.get("trade_case_id") or ""),
        )

    def _cluster_trade_case_rows(
        self,
        closed_cases: List[Dict[str, Any]],
        extracted: List[Dict[str, Any]],
        standardized_vectors: List[List[float]],
        distance_threshold: float = 4.0,
    ) -> List[Dict[str, Any]]:
        clusters: List[Dict[str, Any]] = []
        for row, extracted_row, vector in zip(closed_cases, extracted, standardized_vectors):
            best_index = None
            best_distance = None
            for index, cluster in enumerate(clusters):
                centroid = cluster["centroid"]
                distance = self._euclidean_distance(vector, centroid)
                if best_distance is None or distance < best_distance:
                    best_distance = distance
                    best_index = index
            if best_index is None or best_distance is None or best_distance > distance_threshold:
                clusters.append(
                    {
                        "cases": [row],
                        "features": [extracted_row],
                        "vectors": [vector],
                        "centroid": list(vector),
                    }
                )
                continue
            cluster = clusters[best_index]
            cluster["cases"].append(row)
            cluster["features"].append(extracted_row)
            cluster["vectors"].append(vector)
            count = len(cluster["vectors"])
            cluster["centroid"] = [
                sum(candidate[i] for candidate in cluster["vectors"]) / count
                for i in range(len(vector))
            ]
        return clusters

    def _build_trade_case_cluster_temporal_metrics(self, cluster: Dict[str, Any]) -> Dict[str, Any]:
        ordered = sorted(
            zip(cluster["cases"], cluster["features"], cluster["vectors"]),
            key=lambda item: self._trade_case_cluster_sort_key(item[0]),
        )
        count = len(ordered)
        losses = [
            item for item in ordered
            if (
                (item[1].get("estimated_pnl_pct") is not None and item[1].get("estimated_pnl_pct") < 0)
                or (item[1].get("realized_pnl") is not None and item[1].get("realized_pnl") < 0)
            )
        ]
        baseline_loss_rate = round(len(losses) / max(1, count), 4) if count else 0.0
        if count < 3:
            return {
                "train_count": count,
                "holdout_count": 0,
                "train_loss_rate": baseline_loss_rate,
                "holdout_loss_rate": None,
                "loss_rate_gap": None,
                "centroid_drift": None,
                "pnl_gap": None,
                "consistency_score": round(0.6 if count >= 2 else 0.4, 4),
            }

        split_index = max(1, int(math.ceil(count * 0.7)))
        if split_index >= count:
            split_index = count - 1
        train_rows = ordered[:split_index]
        holdout_rows = ordered[split_index:]
        train_vectors = [item[2] for item in train_rows]
        holdout_vectors = [item[2] for item in holdout_rows]

        def _loss_rate(items: List[tuple[Dict[str, Any], Dict[str, Any], List[float]]]) -> float:
            if not items:
                return 0.0
            losses = 0
            for _, feature, _ in items:
                if (
                    (feature.get("estimated_pnl_pct") is not None and feature.get("estimated_pnl_pct") < 0)
                    or (feature.get("realized_pnl") is not None and feature.get("realized_pnl") < 0)
                ):
                    losses += 1
            return round(losses / max(1, len(items)), 4)

        def _avg_outcome(items: List[tuple[Dict[str, Any], Dict[str, Any], List[float]]]) -> float:
            values = []
            for _, feature, _ in items:
                if feature.get("realized_pnl") is not None:
                    values.append(float(feature["realized_pnl"]))
                elif feature.get("estimated_pnl_pct") is not None:
                    values.append(float(feature["estimated_pnl_pct"]))
            return sum(values) / max(1, len(values)) if values else 0.0

        train_centroid = [
            sum(candidate[i] for candidate in train_vectors) / max(1, len(train_vectors))
            for i in range(len(train_vectors[0]))
        ]
        holdout_centroid = [
            sum(candidate[i] for candidate in holdout_vectors) / max(1, len(holdout_vectors))
            for i in range(len(holdout_vectors[0]))
        ]
        train_loss_rate = _loss_rate(train_rows)
        holdout_loss_rate = _loss_rate(holdout_rows)
        loss_rate_gap = abs(holdout_loss_rate - train_loss_rate)
        centroid_drift = self._euclidean_distance(train_centroid, holdout_centroid)
        pnl_gap = abs(_avg_outcome(train_rows) - _avg_outcome(holdout_rows))
        consistency_score = max(
            0.0,
            1.0
            - (min(1.0, loss_rate_gap) * 0.5)
            - (min(1.0, centroid_drift / 3.0) * 0.3)
            - (min(1.0, pnl_gap / 2.0) * 0.2),
        )
        return {
            "train_count": len(train_rows),
            "holdout_count": len(holdout_rows),
            "train_loss_rate": train_loss_rate,
            "holdout_loss_rate": holdout_loss_rate,
            "loss_rate_gap": round(loss_rate_gap, 4),
            "centroid_drift": round(centroid_drift, 4),
            "pnl_gap": round(pnl_gap, 4),
            "consistency_score": round(consistency_score, 4),
        }

    def _build_trade_case_cluster_internal_metrics(
        self,
        cluster_index: int,
        clusters: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        cluster = clusters[cluster_index]
        vectors = cluster["vectors"]
        centroid = cluster["centroid"]
        avg_intra_distance = (
            sum(self._euclidean_distance(vector, centroid) for vector in vectors) / max(1, len(vectors))
            if vectors
            else 0.0
        )
        nearest_cluster_distance = None
        if len(clusters) > 1:
            nearest_cluster_distance = min(
                self._euclidean_distance(centroid, other["centroid"])
                for other_index, other in enumerate(clusters)
                if other_index != cluster_index
            )

        silhouette_values = []
        if len(vectors) > 1 and len(clusters) > 1:
            for vector in vectors:
                intra_candidates = [
                    self._euclidean_distance(vector, other_vector)
                    for other_vector in vectors
                    if other_vector is not vector
                ]
                a_score = sum(intra_candidates) / max(1, len(intra_candidates)) if intra_candidates else 0.0
                b_candidates = []
                for other_index, other_cluster in enumerate(clusters):
                    if other_index == cluster_index or not other_cluster["vectors"]:
                        continue
                    mean_distance = sum(
                        self._euclidean_distance(vector, other_vector)
                        for other_vector in other_cluster["vectors"]
                    ) / max(1, len(other_cluster["vectors"]))
                    b_candidates.append(mean_distance)
                if not b_candidates:
                    continue
                b_score = min(b_candidates)
                denominator = max(a_score, b_score, 1e-9)
                silhouette_values.append((b_score - a_score) / denominator)
        silhouette_score = (
            sum(silhouette_values) / len(silhouette_values)
            if silhouette_values
            else 0.0
        )
        return {
            "avg_intra_distance": round(avg_intra_distance, 4),
            "nearest_cluster_distance": round(nearest_cluster_distance, 4) if nearest_cluster_distance is not None else None,
            "silhouette_score": round(silhouette_score, 4),
        }

    def _summarize_trade_case_cluster_quality(self, trade_case_clusters: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not trade_case_clusters:
            return {
                "cluster_count": 0,
                "validated_cluster_count": 0,
                "monitor_cluster_count": 0,
                "weak_cluster_count": 0,
                "retired_cluster_count": 0,
                "avg_quality_score": 0.0,
                "retirement_reason_counts": {},
            }
        validated = sum(1 for cluster in trade_case_clusters if str(cluster.get("quality_label") or "") == "validated")
        monitor = sum(1 for cluster in trade_case_clusters if str(cluster.get("quality_label") or "") == "monitor")
        weak = sum(1 for cluster in trade_case_clusters if str(cluster.get("quality_label") or "") == "weak")
        retired = sum(1 for cluster in trade_case_clusters if bool(cluster.get("retired")))
        retirement_reason_counts: Dict[str, int] = {}
        for cluster in trade_case_clusters:
            reason = str(cluster.get("retirement_reason") or "").strip()
            if reason:
                retirement_reason_counts[reason] = retirement_reason_counts.get(reason, 0) + 1
        avg_quality_score = sum(float(cluster.get("quality_score") or 0.0) for cluster in trade_case_clusters) / len(trade_case_clusters)
        return {
            "cluster_count": len(trade_case_clusters),
            "validated_cluster_count": validated,
            "monitor_cluster_count": monitor,
            "weak_cluster_count": weak,
            "retired_cluster_count": retired,
            "avg_quality_score": round(avg_quality_score, 4),
            "retirement_reason_counts": retirement_reason_counts,
        }

    def _build_retrieval_evaluation_summary(self, retrieval_evaluations: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not retrieval_evaluations:
            return {
                "evaluation_count": 0,
                "aligned_count": 0,
                "settled_count": 0,
                "avg_similarity": 0.0,
                "avg_final_score": 0.0,
                "avg_matched_band_count": 0.0,
                "win_rate_pct": 0.0,
                "aligned_win_rate_pct": 0.0,
                "unaligned_win_rate_pct": 0.0,
                "source_counts": {},
            }
        source_counts: Dict[str, int] = {}
        aligned_count = 0
        settled_count = 0
        similarity_sum = 0.0
        similarity_count = 0
        final_score_sum = 0.0
        final_score_count = 0
        matched_band_sum = 0.0
        win_count = 0
        aligned_win_count = 0
        aligned_total = 0
        unaligned_win_count = 0
        unaligned_total = 0
        for row in retrieval_evaluations:
            source = str(row.get("retrieval_source") or "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
            aligned = bool(row.get("decision_symbol_aligned"))
            if aligned:
                aligned_count += 1
                aligned_total += 1
            else:
                unaligned_total += 1
            try:
                similarity = row.get("similarity")
                if similarity is not None:
                    similarity_sum += float(similarity)
                    similarity_count += 1
            except (TypeError, ValueError):
                pass
            try:
                final_score = row.get("final_score")
                if final_score is not None:
                    final_score_sum += float(final_score)
                    final_score_count += 1
            except (TypeError, ValueError):
                pass
            try:
                matched_band_sum += float(row.get("matched_band_count") or 0.0)
            except (TypeError, ValueError):
                pass
            pnl = row.get("estimated_pnl_pct")
            try:
                pnl_source = (
                    row.get("outcome_realized_pnl")
                    if row.get("outcome_realized_pnl") is not None
                    else row.get("outcome_estimated_pnl_pct")
                )
                if pnl_source is None:
                    pnl_source = pnl
                pnl_value = float(pnl_source) if pnl_source is not None else None
            except (TypeError, ValueError):
                pnl_value = None
            if row.get("outcome_status") is not None or row.get("outcome_trade_case_id") is not None:
                settled_count += 1
            if pnl_value is not None and pnl_value > 0:
                win_count += 1
                if aligned:
                    aligned_win_count += 1
                else:
                    unaligned_win_count += 1
        total = len(retrieval_evaluations)
        return {
            "evaluation_count": total,
            "aligned_count": aligned_count,
            "settled_count": settled_count,
            "avg_similarity": round(similarity_sum / similarity_count, 4) if similarity_count else 0.0,
            "avg_final_score": round(final_score_sum / final_score_count, 4) if final_score_count else 0.0,
            "avg_matched_band_count": round(matched_band_sum / total, 4) if total else 0.0,
            "win_rate_pct": round((win_count / total) * 100, 2) if total else 0.0,
            "aligned_win_rate_pct": round((aligned_win_count / aligned_total) * 100, 2) if aligned_total else 0.0,
            "unaligned_win_rate_pct": round((unaligned_win_count / unaligned_total) * 100, 2) if unaligned_total else 0.0,
            "source_counts": source_counts,
        }

    def _build_retrieval_counterfactual_summary(self, replay_runs: List[Dict[str, Any]]) -> Dict[str, Any]:
        rows = [
            {
                "audit_log_id": row.get("audit_log_id"),
                "counterfactual": ((row.get("payload") or {}).get("retrieval_counterfactual") or {}),
            }
            for row in replay_runs or []
        ]
        rows = [row for row in rows if row.get("counterfactual")]
        if not rows:
            return {
                "evaluation_count": 0,
                "decision_changed_count": 0,
                "side_changed_count": 0,
                "avg_confidence_delta": 0.0,
                "avg_size_delta": 0.0,
                "outcome_linked_count": 0,
                "with_retrieval_better_count": 0,
                "without_retrieval_better_count": 0,
                "outcome_tie_count": 0,
                "avg_outcome_utility_delta": 0.0,
            }
        decision_changed = 0
        side_changed = 0
        confidence_delta_sum = 0.0
        size_delta_sum = 0.0
        for row in rows:
            comparison = row.get("counterfactual", {}).get("with_vs_without_comparison") or {}
            if bool(comparison.get("decision_changed")):
                decision_changed += 1
            if bool(comparison.get("side_changed")):
                side_changed += 1
            try:
                confidence_delta_sum += float(comparison.get("confidence_delta") or 0.0)
            except (TypeError, ValueError):
                pass
            try:
                size_delta_sum += abs(float(comparison.get("size_delta") or 0.0))
            except (TypeError, ValueError):
                pass
        total = len(rows)
        return {
            "evaluation_count": total,
            "decision_changed_count": decision_changed,
            "side_changed_count": side_changed,
            "avg_confidence_delta": round(confidence_delta_sum / total, 6) if total else 0.0,
            "avg_size_delta": round(size_delta_sum / total, 6) if total else 0.0,
            "outcome_linked_count": 0,
            "with_retrieval_better_count": 0,
            "without_retrieval_better_count": 0,
            "outcome_tie_count": 0,
            "avg_outcome_utility_delta": 0.0,
        }

    def _build_hold_opportunity_summary(
        self,
        replay_runs: List[Dict[str, Any]],
        *,
        journals: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        journal_by_audit_id = {
            int(row.get("audit_log_id") or 0): row
            for row in journals or []
            if int(row.get("audit_log_id") or 0) > 0
        }
        evaluated = 0
        baseline_beats_hold_count = 0
        missed_breakout_count = 0
        opportunity_costs: List[float] = []
        missed_moves: List[float] = []
        evidence_journal_ids: List[int] = []
        regime_counts: Dict[str, int] = {}
        hold_quality_counts: Dict[str, int] = {}
        trigger_context_counts: Dict[str, int] = {}
        execution_context_counts: Dict[str, int] = {}
        trend_context_counts: Dict[str, int] = {}
        simple_rule_beats_hold_count = 0
        for row in replay_runs or []:
            payload = row.get("payload") or {}
            counterfactual = payload.get("hold_opportunity_counterfactual") or {}
            if not counterfactual:
                continue
            evaluated += 1
            if bool(counterfactual.get("baseline_beats_hold")):
                baseline_beats_hold_count += 1
            if bool(counterfactual.get("simple_rule_baseline_beats_hold")):
                simple_rule_beats_hold_count += 1
            missed_breakout_count += int(counterfactual.get("missed_breakout_count") or 0)
            cost_value = self._coerce_float(counterfactual.get("hold_opportunity_cost"))
            if cost_value is not None:
                opportunity_costs.append(cost_value)
            move_value = self._coerce_float(counterfactual.get("missed_move_median_pct"))
            if move_value is not None and move_value > 0:
                missed_moves.append(move_value)
            hold_quality = str(counterfactual.get("hold_quality") or "").strip()
            if hold_quality:
                hold_quality_counts[hold_quality] = hold_quality_counts.get(hold_quality, 0) + 1
            trigger_signature = str(((counterfactual.get("trigger_context") or {}).get("signature")) or "").strip()
            if trigger_signature:
                trigger_context_counts[trigger_signature] = trigger_context_counts.get(trigger_signature, 0) + 1
            execution_signature = str(((counterfactual.get("execution_context") or {}).get("signature")) or "").strip()
            if execution_signature:
                execution_context_counts[execution_signature] = execution_context_counts.get(execution_signature, 0) + 1
            trend_signature = str(((counterfactual.get("trend_context") or {}).get("signature")) or "").strip()
            if trend_signature:
                trend_context_counts[trend_signature] = trend_context_counts.get(trend_signature, 0) + 1
            audit_id = int(row.get("audit_log_id") or 0)
            journal = journal_by_audit_id.get(audit_id) or {}
            journal_id = int(journal.get("id") or journal.get("journal_id") or 0)
            if journal_id > 0 and journal_id not in evidence_journal_ids:
                evidence_journal_ids.append(journal_id)
            regime_summary = self._extract_market_regime_summary(journal.get("market_state") or {})
            regime_key = self._derive_regime_label(regime_summary)
            regime_counts[regime_key] = regime_counts.get(regime_key, 0) + 1
        if not evaluated:
            return {
                "evaluation_count": 0,
                "baseline_beats_hold_count": 0,
                "missed_breakout_count": 0,
                "missed_move_median_pct": 0.0,
                "hold_opportunity_cost": 0.0,
                "baseline_beats_hold_rate": 0.0,
                "evidence_journal_ids": [],
                "dominant_regime": "general",
                "hold_quality_counts": {},
                "simple_rule_beats_hold_count": 0,
                "dominant_trigger_context": "none",
                "dominant_execution_context": "unknown",
                "dominant_trend_context": "UNKNOWN|UNKNOWN",
            }
        missed_moves_sorted = sorted(missed_moves)
        median_move = (
            missed_moves_sorted[len(missed_moves_sorted) // 2]
            if missed_moves_sorted
            else 0.0
        )
        dominant_regime = max(regime_counts.items(), key=lambda item: (item[1], item[0]))[0] if regime_counts else "general"
        return {
            "evaluation_count": evaluated,
            "baseline_beats_hold_count": baseline_beats_hold_count,
            "missed_breakout_count": missed_breakout_count,
            "missed_move_median_pct": round(median_move, 4),
            "hold_opportunity_cost": round(sum(opportunity_costs), 4),
            "baseline_beats_hold_rate": round(baseline_beats_hold_count / max(1, evaluated), 4),
            "evidence_journal_ids": evidence_journal_ids[:10],
            "dominant_regime": dominant_regime,
            "hold_quality_counts": hold_quality_counts,
            "simple_rule_beats_hold_count": simple_rule_beats_hold_count,
            "dominant_trigger_context": max(trigger_context_counts.items(), key=lambda item: (item[1], item[0]))[0] if trigger_context_counts else "none",
            "dominant_execution_context": max(execution_context_counts.items(), key=lambda item: (item[1], item[0]))[0] if execution_context_counts else "unknown",
            "dominant_trend_context": max(trend_context_counts.items(), key=lambda item: (item[1], item[0]))[0] if trend_context_counts else "UNKNOWN|UNKNOWN",
        }

    def _build_trading_balance_summary(
        self,
        *,
        journals: List[Dict[str, Any]],
        trade_cases: List[Dict[str, Any]],
        hold_opportunity_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        entry_journals = [
            row for row in journals or []
            if str(row.get("decision") or "").upper() in {"BUY", "SELL"}
        ]
        hold_journals = [
            row for row in journals or []
            if str(row.get("decision") or "").upper() == "HOLD"
        ]
        closed_entry_cases = [
            row for row in trade_cases or []
            if str(row.get("status") or "") == "closed"
            and str(row.get("entry_decision") or "").upper() in {"BUY", "SELL"}
        ]
        false_entry_cost_pct_values: List[float] = []
        false_entry_cost_amount_values: List[float] = []
        normalized_loss_count = 0
        win_count = 0
        loss_count = 0
        for row in closed_entry_cases:
            estimated_pct = self._coerce_float(row.get("estimated_pnl_pct"))
            realized_amount = self._coerce_float(row.get("realized_pnl"))
            if estimated_pct is not None and estimated_pct < 0:
                loss_count += 1
                normalized_loss_count += 1
                false_entry_cost_pct_values.append(abs(float(estimated_pct)))
                if realized_amount is not None and realized_amount < 0:
                    false_entry_cost_amount_values.append(abs(float(realized_amount)))
            elif estimated_pct is not None and estimated_pct > 0:
                win_count += 1
            elif estimated_pct is None and realized_amount is not None:
                if realized_amount < 0:
                    loss_count += 1
                    false_entry_cost_amount_values.append(abs(float(realized_amount)))
                elif realized_amount > 0:
                    win_count += 1
        false_entry_cost = round(sum(false_entry_cost_pct_values), 4)
        false_entry_cost_amount = round(sum(false_entry_cost_amount_values), 4)
        closed_case_count = len(closed_entry_cases)
        holding_minutes_values = [
            float(value)
            for value in (
                self._coerce_float(row.get("holding_minutes"))
                for row in closed_entry_cases
            )
            if value is not None and value >= 0
        ]
        slippage_values = [
            float(value)
            for value in (
                self._coerce_float(row.get("slippage_bps"))
                for row in closed_entry_cases
            )
            if value is not None and value >= 0
        ]
        losing_mae_values = [
            abs(float(value))
            for row in closed_entry_cases
            for value in [self._coerce_float(row.get("observed_mae_pct"))]
            if value is not None
            and (
                (self._coerce_float(row.get("estimated_pnl_pct")) is not None and float(self._coerce_float(row.get("estimated_pnl_pct")) or 0.0) < 0)
                or (self._coerce_float(row.get("realized_pnl")) is not None and float(self._coerce_float(row.get("realized_pnl")) or 0.0) < 0)
            )
        ]
        undertrading_pressure = float(hold_opportunity_summary.get("hold_opportunity_cost") or 0.0)
        overtrading_pressure = false_entry_cost
        total_pressure = undertrading_pressure + overtrading_pressure
        balance_score = 1.0
        if total_pressure > 0:
            balance_score = max(
                0.0,
                1.0 - (abs(undertrading_pressure - overtrading_pressure) / total_pressure),
            )
        if undertrading_pressure > (overtrading_pressure * 1.15):
            stance = "undertrading"
        elif overtrading_pressure > (undertrading_pressure * 1.15):
            stance = "overtrading"
        else:
            stance = "balanced"
        pressure_ratio = 1.0
        if overtrading_pressure > 0:
            pressure_ratio = undertrading_pressure / overtrading_pressure
        elif undertrading_pressure > 0:
            pressure_ratio = float("inf")
        journal_count = max(1, len(journals))
        turnover_rate = closed_case_count / journal_count
        capital_idle_ratio = len(hold_journals) / journal_count
        avg_holding_minutes = sum(holding_minutes_values) / max(1, len(holding_minutes_values)) if holding_minutes_values else 0.0
        avg_entry_slippage_bps = sum(slippage_values) / max(1, len(slippage_values)) if slippage_values else 0.0
        drawdown_path_penalty = sum(losing_mae_values) / max(1, len(losing_mae_values)) if losing_mae_values else 0.0
        regime_capacity_penalty = max(
            0.0,
            (int(hold_opportunity_summary.get("missed_breakout_count") or 0) * 0.12)
            - (closed_case_count * 0.03),
        )
        portfolio_utility_score = max(
            0.0,
            min(
                1.0,
                0.55
                + (balance_score * 0.2)
                - min(0.18, capital_idle_ratio * 0.18)
                - min(0.14, turnover_rate * 0.1)
                - min(0.18, drawdown_path_penalty * 0.06)
                - min(0.12, avg_entry_slippage_bps * 0.01)
                - min(0.12, regime_capacity_penalty * 0.05),
            ),
        )
        return {
            "entry_decision_count": len(entry_journals),
            "hold_decision_count": len(hold_journals),
            "closed_entry_case_count": closed_case_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "false_entry_cost": false_entry_cost,
            "false_entry_cost_amount": false_entry_cost_amount,
            "false_entry_cost_pct": false_entry_cost,
            "normalized_false_entry_loss_count": normalized_loss_count,
            "hold_opportunity_cost": undertrading_pressure,
            "missed_breakout_count": int(hold_opportunity_summary.get("missed_breakout_count") or 0),
            "missed_move_median_pct": float(hold_opportunity_summary.get("missed_move_median_pct") or 0.0),
            "undertrading_pressure": round(undertrading_pressure, 4),
            "overtrading_pressure": round(overtrading_pressure, 4),
            "pressure_ratio": round(pressure_ratio, 4) if math.isfinite(pressure_ratio) else None,
            "balance_score": round(balance_score, 4),
            "turnover_rate": round(turnover_rate, 4),
            "capital_idle_ratio": round(capital_idle_ratio, 4),
            "avg_holding_minutes": round(avg_holding_minutes, 4),
            "avg_entry_slippage_bps": round(avg_entry_slippage_bps, 4),
            "drawdown_path_penalty": round(drawdown_path_penalty, 4),
            "regime_capacity_penalty": round(regime_capacity_penalty, 4),
            "portfolio_utility_score": round(portfolio_utility_score, 4),
            "stance": stance,
        }

    def _classify_confidence_outcome(self, row: Dict[str, Any]) -> float | None:
        confidence = self._coerce_float(row.get("confidence"))
        if confidence is None:
            return None
        decision = str(row.get("decision") or "").upper().strip()
        pnl = self._coerce_float(row.get("estimated_pnl_pct"))
        if pnl is None:
            return None
        if decision in {"BUY", "SELL"}:
            return 1.0 if pnl > 0 else 0.0
        if decision == "HOLD":
            return 1.0 if pnl <= 0.2 else 0.0
        return None

    def _build_confidence_calibration_summary(self, journals: List[Dict[str, Any]]) -> Dict[str, Any]:
        evaluated_rows = []
        for row in journals or []:
            observed = self._classify_confidence_outcome(row)
            confidence = self._coerce_float(row.get("confidence"))
            if observed is None or confidence is None:
                continue
            evaluated_rows.append((float(confidence), float(observed)))
        if not evaluated_rows:
            return {
                "evaluated_count": 0,
                "avg_confidence": 0.0,
                "empirical_success_rate": 0.0,
                "calibration_gap": 0.0,
                "brier_score": 0.0,
                "high_conf_error_rate": 0.0,
                "low_conf_success_rate": 0.0,
                "alignment_score": 0.5,
                "buckets": [],
            }
        calibration_gap = sum(abs(conf - observed) for conf, observed in evaluated_rows) / len(evaluated_rows)
        brier_score = sum((conf - observed) ** 2 for conf, observed in evaluated_rows) / len(evaluated_rows)
        high_conf_rows = [(conf, observed) for conf, observed in evaluated_rows if conf >= 0.7]
        low_conf_rows = [(conf, observed) for conf, observed in evaluated_rows if conf <= 0.45]
        high_conf_error_rate = (
            sum(1.0 for _, observed in high_conf_rows if observed < 0.5) / len(high_conf_rows)
            if high_conf_rows
            else 0.0
        )
        low_conf_success_rate = (
            sum(observed for _, observed in low_conf_rows) / len(low_conf_rows)
            if low_conf_rows
            else 0.0
        )
        buckets = []
        for lower, upper in ((0.0, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)):
            bucket_rows = [(conf, observed) for conf, observed in evaluated_rows if lower <= conf < upper]
            if not bucket_rows:
                continue
            bucket_avg_conf = sum(conf for conf, _ in bucket_rows) / len(bucket_rows)
            bucket_success = sum(observed for _, observed in bucket_rows) / len(bucket_rows)
            buckets.append(
                {
                    "bucket": f"{lower:.1f}-{min(1.0, upper):.1f}",
                    "count": len(bucket_rows),
                    "avg_confidence": round(bucket_avg_conf, 4),
                    "success_rate": round(bucket_success, 4),
                    "gap": round(abs(bucket_avg_conf - bucket_success), 4),
                }
            )
        alignment_score = max(
            0.0,
            min(
                1.0,
                1.0
                - min(0.6, calibration_gap * 0.9)
                - min(0.25, brier_score * 0.5)
                - min(0.2, high_conf_error_rate * 0.35)
                + min(0.08, low_conf_success_rate * 0.08),
            ),
        )
        return {
            "evaluated_count": len(evaluated_rows),
            "avg_confidence": round(sum(conf for conf, _ in evaluated_rows) / len(evaluated_rows), 4),
            "empirical_success_rate": round(sum(observed for _, observed in evaluated_rows) / len(evaluated_rows), 4),
            "calibration_gap": round(calibration_gap, 4),
            "brier_score": round(brier_score, 4),
            "high_conf_error_rate": round(high_conf_error_rate, 4),
            "low_conf_success_rate": round(low_conf_success_rate, 4),
            "alignment_score": round(alignment_score, 4),
            "buckets": buckets,
        }

    def _coerce_float(self, value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _build_retrieval_outcome_metrics(self, row: Dict[str, Any]) -> Dict[str, Any]:
        realized_pnl = self._coerce_float(row.get("outcome_realized_pnl"))
        estimated_pnl = self._coerce_float(row.get("outcome_estimated_pnl_pct"))
        pnl = realized_pnl if realized_pnl is not None else estimated_pnl
        mfe = self._coerce_float(row.get("outcome_observed_mfe_pct"))
        mae = self._coerce_float(row.get("outcome_observed_mae_pct"))
        slippage_bps = self._coerce_float(row.get("outcome_slippage_bps"))
        holding_minutes = self._coerce_float(row.get("outcome_holding_minutes"))
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
        pnl = self._coerce_float(outcome_metrics.get("pnl"))
        if pnl is None:
            return 0.0
        mfe = max(0.0, self._coerce_float(outcome_metrics.get("mfe")) or 0.0)
        mae = max(0.0, self._coerce_float(outcome_metrics.get("mae")) or 0.0)
        slippage_bps = max(0.0, self._coerce_float(outcome_metrics.get("slippage_bps")) or 0.0)
        holding_minutes = max(0.0, self._coerce_float(outcome_metrics.get("holding_minutes")) or 0.0)

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

    def _apply_retrieval_outcome_counterfactual_summary(
        self,
        summary: Dict[str, Any],
        replay_runs: List[Dict[str, Any]],
        retrieval_evaluations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
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
        outcome_linked_count = 0
        with_better = 0
        without_better = 0
        tie_count = 0
        utility_delta_sum = 0.0
        for replay_row in replay_runs or []:
            audit_log_id = replay_row.get("audit_log_id")
            if audit_log_id is None:
                continue
            try:
                outcome_metrics = settled_outcomes[int(audit_log_id)]
            except (KeyError, TypeError, ValueError):
                continue
            counterfactual = ((replay_row.get("payload") or {}).get("retrieval_counterfactual") or {})
            if not counterfactual:
                continue
            with_intent = counterfactual.get("with_retrieval_intent") or {}
            without_intent = counterfactual.get("without_retrieval_intent") or {}
            with_score = self._score_decision_against_outcome_utility(with_intent.get("decision"), outcome_metrics)
            without_score = self._score_decision_against_outcome_utility(
                without_intent.get("decision"),
                outcome_metrics,
            )
            outcome_linked_count += 1
            utility_delta_sum += with_score - without_score
            if math.isclose(with_score, without_score, abs_tol=0.05):
                tie_count += 1
            elif with_score > without_score:
                with_better += 1
            else:
                without_better += 1
        enriched = dict(summary)
        enriched["outcome_linked_count"] = outcome_linked_count
        enriched["with_retrieval_better_count"] = with_better
        enriched["without_retrieval_better_count"] = without_better
        enriched["outcome_tie_count"] = tie_count
        enriched["avg_outcome_utility_delta"] = (
            round(utility_delta_sum / outcome_linked_count, 6) if outcome_linked_count else 0.0
        )
        return enriched

    def _build_execution_policy_summary(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {
                "profile_counts": {},
                "order_type_counts": {},
                "avg_slippage_bps": 0.0,
                "avg_fill_ratio": 0.0,
                "sample_count": 0,
                "cancel_count": 0,
                "cancel_success_rate": 0.0,
                "cancel_timeout_rate": 0.0,
            }

        profile_counts: Dict[str, int] = {}
        order_type_counts: Dict[str, int] = {}
        slippage_total = 0.0
        fill_ratio_total = 0.0
        slippage_count = 0
        fill_ratio_count = 0
        cancel_count = 0
        cancel_success_count = 0
        cancel_timeout_count = 0
        for row in rows:
            profile = str(row.get("execution_profile") or "unknown")
            order_type = str(row.get("order_type") or "unknown")
            profile_counts[profile] = profile_counts.get(profile, 0) + 1
            order_type_counts[order_type] = order_type_counts.get(order_type, 0) + 1
            if str(row.get("action_type") or "").strip().lower() == "cancel":
                cancel_count += 1
                status = str(row.get("status") or "").lower()
                if status in ("canceled", "cancelled", "closed"):
                    cancel_success_count += 1
                if status in ("timeout_unresolved", "failed", "rejected"):
                    cancel_timeout_count += 1
                continue
            slippage_bps = self._coerce_float(row.get("slippage_bps"))
            if slippage_bps is not None:
                slippage_total += slippage_bps
                slippage_count += 1
            fill_ratio = self._coerce_float(row.get("fill_ratio"))
            if fill_ratio is not None:
                fill_ratio_total += fill_ratio
                fill_ratio_count += 1
        return {
            "profile_counts": profile_counts,
            "order_type_counts": order_type_counts,
            "avg_slippage_bps": round(slippage_total / max(1, slippage_count), 4),
            "avg_fill_ratio": round(fill_ratio_total / max(1, fill_ratio_count), 4),
            "sample_count": len(rows),
            "cancel_count": cancel_count,
            "cancel_success_rate": round((cancel_success_count / cancel_count), 4) if cancel_count else 0.0,
            "cancel_timeout_rate": round((cancel_timeout_count / cancel_count), 4) if cancel_count else 0.0,
        }

    def _build_execution_policy_recommendations(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[tuple[str, str, str, str, str, str], Dict[str, Dict[str, float]]] = {}
        for row in rows or []:
            symbol = str(row.get("symbol") or "").strip()
            loop_mode = str(row.get("loop_mode") or "").strip()
            action_type = str(row.get("action_type") or "").strip()
            urgency_bucket = str(row.get("urgency_bucket") or "").strip()
            liquidity_bucket = str(row.get("liquidity_bucket") or "").strip()
            risk_mode = str(row.get("risk_mode") or "").strip() or "normal"
            profile = str(row.get("execution_profile") or "").strip()
            order_type = str(row.get("order_type") or "").strip()
            if not loop_mode or not action_type or not urgency_bucket or not liquidity_bucket or not profile or not order_type:
                continue
            for scoped_symbol in (symbol, "*"):
                if not scoped_symbol:
                    continue
                key = (
                    scoped_symbol,
                    loop_mode,
                    action_type,
                    urgency_bucket,
                    liquidity_bucket,
                    risk_mode,
                )
                bucket = grouped.setdefault(key, {})
                stats = bucket.setdefault(
                    profile,
                    {
                        "sample_count": 0.0,
                        "fill_ratio_total": 0.0,
                        "fill_ratio_count": 0.0,
                        "slippage_total": 0.0,
                        "slippage_count": 0.0,
                        "outcome_total": 0.0,
                        "outcome_count": 0.0,
                        "timeout_count": 0.0,
                        "open_count": 0.0,
                        "order_type": order_type,
                    },
                )
                stats["sample_count"] += 1.0
                fill_ratio = self._coerce_float(row.get("fill_ratio"))
                if fill_ratio is not None:
                    stats["fill_ratio_total"] += fill_ratio
                    stats["fill_ratio_count"] += 1.0
                slippage_bps = self._coerce_float(row.get("slippage_bps"))
                if slippage_bps is not None:
                    stats["slippage_total"] += slippage_bps
                    stats["slippage_count"] += 1.0
                outcome = self._coerce_float(row.get("outcome_estimated_pnl_pct"))
                if outcome is not None:
                    stats["outcome_total"] += outcome
                    stats["outcome_count"] += 1.0
                status = str(row.get("status") or "").lower()
                if status in ("timeout_unresolved", "failed", "rejected"):
                    stats["timeout_count"] += 1.0
                if status in ("open", "submitted", "partial_filled", "partially_filled", "partiallyfilled"):
                    stats["open_count"] += 1.0

        recommendations: List[Dict[str, Any]] = []
        for key, profiles in grouped.items():
            symbol, loop_mode, action_type, urgency_bucket, liquidity_bucket, risk_mode = key
            ranked = []
            for profile_name, stats in profiles.items():
                sample_count = float(stats["sample_count"])
                avg_fill_ratio = stats["fill_ratio_total"] / max(1.0, stats["fill_ratio_count"])
                avg_slippage_bps = stats["slippage_total"] / max(1.0, stats["slippage_count"])
                avg_outcome = stats["outcome_total"] / max(1.0, stats["outcome_count"]) if stats["outcome_count"] else 0.0
                timeout_rate = stats["timeout_count"] / max(1.0, sample_count)
                open_rate = stats["open_count"] / max(1.0, sample_count)
                if action_type == "cancel":
                    utility = (
                        (avg_fill_ratio * 50.0)
                        - (timeout_rate * 30.0)
                        - (open_rate * 20.0)
                    )
                else:
                    utility = (
                        (avg_fill_ratio * 60.0)
                        - (avg_slippage_bps * 1.5)
                        + (avg_outcome * 2.0)
                        - (timeout_rate * 25.0)
                        - (open_rate * 10.0)
                    )
                ranked.append(
                    (
                        utility,
                        {
                            "profile_name": profile_name,
                            "order_type": stats["order_type"],
                            "sample_count": int(sample_count),
                            "avg_fill_ratio": round(avg_fill_ratio, 4),
                            "avg_slippage_bps": round(avg_slippage_bps, 4),
                            "avg_outcome_estimated_pnl_pct": round(avg_outcome, 4),
                            "timeout_rate": round(timeout_rate, 4),
                            "open_rate": round(open_rate, 4),
                            "utility_score": round(utility, 4),
                        },
                    )
                )
            if not ranked:
                continue
            ranked.sort(key=lambda item: (item[0], item[1]["sample_count"]), reverse=True)
            best = ranked[0][1]
            recommendations.append(
                {
                    "symbol": symbol,
                    "loop_mode": loop_mode,
                    "risk_mode": risk_mode,
                    "action_type": action_type,
                    "urgency_bucket": urgency_bucket,
                    "liquidity_bucket": liquidity_bucket,
                    "recommended_order_type": best["order_type"],
                    "recommended_execution_profile": best["profile_name"],
                    "metrics": {
                        "sample_count": best["sample_count"],
                        "avg_fill_ratio": best["avg_fill_ratio"],
                        "avg_slippage_bps": best["avg_slippage_bps"],
                        "avg_outcome_estimated_pnl_pct": best["avg_outcome_estimated_pnl_pct"],
                        "timeout_rate": best["timeout_rate"],
                        "open_rate": best["open_rate"],
                        "utility_score": best["utility_score"],
                        "profile_ranking": [item[1] for item in ranked[:3]],
                    },
                }
            )
        return recommendations

    def _build_shadow_policy_summary(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not rows:
            return {"candidate_count": 0, "divergence_counts": {}, "avg_outcome_estimated_pnl_pct": 0.0}
        divergence_counts: Dict[str, int] = {}
        outcome_total = 0.0
        outcome_count = 0
        candidate_ids = set()
        for row in rows:
            candidate_ids.add(row.get("candidate_policy_id"))
            divergence = str(row.get("divergence_type") or "none")
            divergence_counts[divergence] = divergence_counts.get(divergence, 0) + 1
            outcome = self._coerce_float(row.get("outcome_estimated_pnl_pct"))
            if outcome is not None:
                outcome_total += outcome
                outcome_count += 1
        return {
            "candidate_count": len(candidate_ids),
            "divergence_counts": divergence_counts,
            "avg_outcome_estimated_pnl_pct": round(outcome_total / max(1, outcome_count), 4),
        }

    def _build_candidate_policies(
        self,
        stats_package_seed: Dict[str, Any],
        trade_cases: List[Dict[str, Any]],
        execution_policy_recommendations: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for recommendation in execution_policy_recommendations or []:
            metrics = recommendation.get("metrics") or {}
            sample_count = int(metrics.get("sample_count") or 0)
            if sample_count < 4:
                continue
            candidates.append(
                {
                    "domain": "execution_5m",
                    "policy_type": "execution_profile",
                    "policy_name": f"exec_{recommendation.get('symbol')}_{recommendation.get('loop_mode')}_{recommendation.get('action_type')}",
                    "source": "memory_updater",
                    "status": "candidate",
                    "rule_text": (
                        f"Prefer {recommendation.get('recommended_execution_profile')} for "
                        f"{recommendation.get('symbol')} {recommendation.get('loop_mode')} {recommendation.get('action_type')} "
                        f"under {recommendation.get('urgency_bucket')}/{recommendation.get('liquidity_bucket')}."
                    ),
                    "config": dict(recommendation),
                    "metrics": dict(metrics, seed_type="execution_profile"),
                }
            )

        failure_counts = ((stats_package_seed.get("trade_case_patterns") or {}).get("failure_reason_counts") or {})
        adverse = (failure_counts.get("adverse_price_follow_through") or {}).get("count", 0)
        if adverse >= 2:
            candidates.append(
                {
                    "domain": "position_15m",
                    "policy_type": "management_fsm",
                    "policy_name": "position_time_stop_reduce",
                    "source": "memory_updater",
                    "status": "candidate",
                    "rule_text": "If no follow-through within 45 minutes then reduce 50 percent.",
                    "config": {
                        "stalled_minutes": 45.0,
                        "stalled_max_pnl_pct": 0.2,
                        "weakened_reduce_pct": 0.5,
                    },
                    "metrics": {"support_count": adverse, "seed_type": "failure_pattern"},
                }
            )

        candidates.extend(self._build_alpha_candidate_policies(trade_cases))
        candidates.extend(self._build_safety_candidate_policies(trade_cases))
        candidates.extend(self._build_exposure_candidate_policies(trade_cases))

        for lesson in (stats_package_seed.get("candidate_lessons") or [])[:6]:
            rule = str((lesson or {}).get("rule") or "").strip()
            if not rule:
                continue
            category = str((lesson or {}).get("category") or "lesson")
            domain = "alpha_1h"
            policy_type = "entry_filter"
            if category == "risk_control" or "reduce" in rule.lower() or "exit" in rule.lower():
                domain = "position_15m"
                policy_type = "management_rule"
            config = {"rule": rule, "category": category}
            if domain == "alpha_1h":
                continue
            candidates.append(
                {
                    "domain": domain,
                    "policy_type": policy_type,
                    "policy_name": f"{domain}_{abs(hash(rule)) % 100000}",
                    "source": "memory_updater",
                    "status": "candidate",
                    "rule_text": rule,
                    "config": config,
                    "metrics": {
                        "support_count": int((lesson or {}).get("support_count") or 0),
                        "seed_type": "candidate_lesson",
                    },
                }
            )
        return candidates

    def _build_safety_candidate_policies(self, trade_cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in trade_cases or []:
            if str(row.get("status") or "") != "closed":
                continue
            risk_mode = str(row.get("risk_mode") or "").strip().lower()
            if risk_mode not in ("trigger", "normal"):
                continue
            regime_key = self._extract_trade_case_regime_key(row)
            grouped.setdefault(regime_key, []).append(row)

        candidates: List[Dict[str, Any]] = []
        for regime_key, rows in grouped.items():
            if len(rows) < 3:
                continue
            pnl_values = [self._coerce_float(row.get("estimated_pnl_pct")) or 0.0 for row in rows]
            loss_values = [value for value in pnl_values if value < 0.0]
            loss_rate = len(loss_values) / max(1, len(rows))
            avg_loss = sum(loss_values) / max(1, len(loss_values)) if loss_values else 0.0
            feature_rows = [((row.get("payload") or {}).get("feature_snapshot") or {}) for row in rows]
            spread_samples = [
                self._coerce_float(feature.get("spread_percentile_1h"))
                for feature in feature_rows
                if self._coerce_float(feature.get("spread_percentile_1h")) is not None
            ]
            depth_samples = [
                self._coerce_float(feature.get("depth_percentile_1h"))
                for feature in feature_rows
                if self._coerce_float(feature.get("depth_percentile_1h")) is not None
            ]
            risk_trigger_counts = [
                len((row.get("risk_trigger_reasons") or []) or (((row.get("payload") or {}).get("risk_trigger_reasons")) or []))
                for row in rows
            ]

            config = {
                "loss_exit_pct": -0.9,
                "risk_trigger_exit_count": 1,
                "microstructure_exit_spread_percentile": 85.0,
                "microstructure_exit_depth_percentile": 15.0,
                "profit_protect_trigger_pct": 1.0,
                "profit_protect_reduce_fraction": 0.35,
                "stale_order_minutes": 12.0,
                "stale_order_risk_trigger_count": 1,
            }
            changed = False
            if loss_rate >= 0.55 or avg_loss <= -0.9:
                config["loss_exit_pct"] = -0.65
                config["microstructure_exit_spread_percentile"] = round(
                    min(spread_samples) if spread_samples else 78.0,
                    2,
                )
                config["microstructure_exit_depth_percentile"] = round(
                    max(depth_samples) if depth_samples else 20.0,
                    2,
                )
                changed = True
            if risk_trigger_counts and (sum(risk_trigger_counts) / max(1, len(risk_trigger_counts))) >= 1.5:
                config["risk_trigger_exit_count"] = 1
                config["profit_protect_reduce_fraction"] = 0.5
                changed = True
            if not changed:
                continue
            support_count = len(rows)
            utility_score = round(abs(avg_loss) + loss_rate, 4)
            candidates.append(
                {
                    "domain": "safety_5m",
                    "policy_type": "execution_safety_fsm",
                    "regime_key": regime_key or None,
                    "policy_name": f"safety_{(regime_key or 'global').replace('|', '_')}",
                    "source": "memory_updater",
                    "status": "candidate",
                    "rule_text": (
                        f"5m safety loop exits faster in {regime_key or 'global'} when loss acceleration, "
                        "risk triggers, and degraded microstructure align."
                    ),
                    "config": config,
                    "metrics": {
                        "support_count": support_count,
                        "sample_count": support_count,
                        "utility_score": utility_score,
                        "loss_rate": round(loss_rate, 4),
                        "avg_loss_pct": round(avg_loss, 4),
                        "seed_type": "trade_case_safety",
                        "regime_key": regime_key or "",
                    },
                }
            )
        return candidates

    def _build_exposure_candidate_policies(self, trade_cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        mode_defaults = {
            "normal": {"base_cap_pct": 100.0, "min_cap_pct": 25.0, "max_cap_pct": 125.0},
        }
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for row in trade_cases or []:
            if str(row.get("status") or "") != "closed":
                continue
            risk_mode = str(row.get("risk_mode") or "").strip().lower()
            if risk_mode not in mode_defaults:
                continue
            regime_key = self._extract_trade_case_regime_key(row)
            grouped.setdefault(f"{risk_mode}|{regime_key}", []).append(row)

        candidates: List[Dict[str, Any]] = []
        for exposure_regime_key, rows in grouped.items():
            if len(rows) < 3:
                continue
            risk_mode, _, market_regime_key = exposure_regime_key.partition("|")
            defaults = mode_defaults[risk_mode]
            pnl_values = [self._coerce_float(row.get("estimated_pnl_pct")) or 0.0 for row in rows]
            loss_count = len([value for value in pnl_values if value < 0.0])
            win_count = len([value for value in pnl_values if value > 0.0])
            avg_pnl = sum(pnl_values) / max(1, len(pnl_values))
            loss_rate = loss_count / max(1, len(rows))
            slippage_values = [
                abs(self._coerce_float(row.get("slippage_bps")) or 0.0)
                for row in rows
                if self._coerce_float(row.get("slippage_bps")) is not None
            ]
            avg_slippage = sum(slippage_values) / max(1, len(slippage_values)) if slippage_values else 0.0
            target_cap_pct = defaults["base_cap_pct"]
            entry_size_multiplier = 1.0
            if loss_rate >= 0.55 or avg_pnl <= -0.2:
                target_cap_pct = max(
                    defaults["min_cap_pct"],
                    round(defaults["base_cap_pct"] * 0.7, 2),
                )
                entry_size_multiplier = 0.85
            elif win_count >= 2 and (win_count / max(1, len(rows))) >= 0.67 and avg_pnl >= 0.35 and avg_slippage <= 6.0:
                target_cap_pct = min(
                    defaults["max_cap_pct"],
                    round(defaults["base_cap_pct"] * 1.1, 2),
                )
            if target_cap_pct == defaults["base_cap_pct"] and entry_size_multiplier == 1.0:
                continue
            utility_score = round(abs(avg_pnl) + abs(target_cap_pct - defaults["base_cap_pct"]) / defaults["base_cap_pct"], 4)
            direction = "reduce" if target_cap_pct < defaults["base_cap_pct"] else "expand"
            candidates.append(
                {
                    "domain": "exposure_budget",
                    "policy_type": "risk_profile_override",
                    "regime_key": exposure_regime_key,
                    "policy_name": f"exposure_{risk_mode}_{market_regime_key.replace('|', '_')}",
                    "source": "memory_updater",
                    "status": "candidate",
                    "rule_text": (
                        f"{risk_mode} regime {market_regime_key}: {direction} max participation to "
                        f"{target_cap_pct:.2f}% with entry multiplier {entry_size_multiplier:.2f}."
                    ),
                    "config": {
                        "max_order_notional_pct_balance": target_cap_pct,
                        "entry_size_multiplier": entry_size_multiplier,
                    },
                    "metrics": {
                        "risk_mode": risk_mode,
                        "market_regime_key": market_regime_key,
                        "support_count": len(rows),
                        "sample_count": len(rows),
                        "loss_rate": round(loss_rate, 4),
                        "avg_estimated_pnl_pct": round(avg_pnl, 4),
                        "avg_slippage_bps": round(avg_slippage, 4),
                        "utility_score": utility_score,
                        "seed_type": "trade_case_exposure_regime",
                    },
                }
            )
        return candidates

    def _build_alpha_candidate_policies(self, trade_cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        closed_cases = [
            row
            for row in (trade_cases or [])
            if str(row.get("status") or "") == "closed"
            and str(row.get("entry_decision") or "").upper() in {"BUY", "SELL"}
        ]
        if not closed_cases:
            return []
        grouped_cases: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for row in closed_cases:
            regime_key = self._extract_trade_case_regime_key(row)
            bucket = grouped_cases.setdefault(regime_key, {"losses": [], "wins": []})
            pnl = self._coerce_float(row.get("estimated_pnl_pct")) or 0.0
            if pnl < 0.0:
                bucket["losses"].append(row)
            elif pnl > 0.0:
                bucket["wins"].append(row)
        feature_specs = [
            {
                "feature_key": "volume_ratio_15m",
                "config_key": "min_volume_ratio_15m",
                "policy_name": "alpha_min_volume_ratio",
                "direction": "min",
                "support_min": 2,
                "loss_floor": 1.2,
                "delta_margin": 0.1,
                "adjustment": 0.1,
                "round_digits": 2,
                "rule_prefix": "Require volume_ratio_15m >=",
            },
            {
                "feature_key": "spread_percentile_1h",
                "config_key": "max_spread_percentile_1h",
                "policy_name": "alpha_max_spread_percentile",
                "direction": "max",
                "support_min": 2,
                "loss_floor": 75.0,
                "delta_margin": 8.0,
                "adjustment": -5.0,
                "round_digits": 1,
                "rule_prefix": "Disable entries when spread_percentile_1h >",
            },
            {
                "feature_key": "distance_to_invalid_level",
                "config_key": "max_distance_to_invalid_level",
                "policy_name": "alpha_max_distance_to_invalid",
                "direction": "max",
                "support_min": 2,
                "loss_floor": 1.0,
                "delta_margin": 0.15,
                "adjustment": -0.1,
                "round_digits": 3,
                "rule_prefix": "Disable entries when distance_to_invalid_level >",
            },
        ]

        candidates: List[Dict[str, Any]] = []
        for regime_key, buckets in grouped_cases.items():
            loss_cases = buckets["losses"]
            win_cases = buckets["wins"]
            if len(loss_cases) < 2:
                continue
            for spec in feature_specs:
                candidate = self._build_alpha_feature_candidate(loss_cases, win_cases, spec, regime_key=regime_key)
                if candidate:
                    candidates.append(candidate)
        return candidates

    def _build_alpha_feature_candidate(
        self,
        loss_cases: List[Dict[str, Any]],
        win_cases: List[Dict[str, Any]],
        spec: Dict[str, Any],
        regime_key: str = "",
    ) -> Dict[str, Any]:
        feature_key = str(spec["feature_key"])
        loss_values = self._extract_trade_case_feature_values(loss_cases, feature_key)
        win_values = self._extract_trade_case_feature_values(win_cases, feature_key)
        if len(loss_values) < int(spec.get("support_min") or 2):
            return {}
        loss_median = statistics.median(loss_values)
        win_median = statistics.median(win_values) if win_values else None
        direction = str(spec.get("direction") or "max")
        delta_margin = float(spec.get("delta_margin") or 0.0)
        loss_floor = float(spec.get("loss_floor") or 0.0)
        if direction == "min":
            if loss_median > loss_floor:
                return {}
            if win_median is not None and (win_median - loss_median) < delta_margin:
                return {}
        else:
            if loss_median < loss_floor:
                return {}
            if win_median is not None and (loss_median - win_median) < delta_margin:
                return {}
        threshold = round(
            loss_median + float(spec.get("adjustment") or 0.0),
            int(spec.get("round_digits") or 2),
        )
        utility_score = 0.0
        if win_median is not None:
            utility_score = round(abs(win_median - loss_median), 4)
        else:
            utility_score = round(abs(loss_median), 4)
        config_key = str(spec["config_key"])
        return {
            "domain": "alpha_1h",
            "policy_type": "entry_filter",
            "regime_key": regime_key or None,
            "policy_name": str(spec["policy_name"]),
            "source": "memory_updater",
            "status": "candidate",
            "rule_text": f"{spec['rule_prefix']} {threshold}.",
            "config": {config_key: threshold},
            "metrics": {
                "support_count": len(loss_values),
                "sample_count": len(loss_values),
                "utility_score": utility_score,
                "feature_key": feature_key,
                "regime_key": regime_key or "",
                "loss_median": round(loss_median, 4),
                "win_median": round(win_median, 4) if win_median is not None else None,
                "seed_type": "trade_case_feature_gap",
            },
        }

    def _extract_trade_case_feature_values(self, trade_cases: List[Dict[str, Any]], feature_key: str) -> List[float]:
        values: List[float] = []
        for row in trade_cases or []:
            payload = row.get("payload") or {}
            feature_snapshot = payload.get("feature_snapshot") or {}
            value = self._coerce_float(feature_snapshot.get(feature_key))
            if value is not None:
                values.append(value)
        return values

    def _extract_trade_case_regime_key(self, trade_case: Dict[str, Any]) -> str:
        payload = trade_case.get("payload") or {}
        regime_tags = payload.get("regime_tags") or []
        trend_alignment = "mixed"
        volatility_regime = "unknown"
        for tag in regime_tags:
            tag_text = str(tag or "").strip()
            if tag_text.startswith("trend:"):
                trend_alignment = tag_text.split(":", 1)[1] or trend_alignment
            elif tag_text.startswith("volatility:"):
                volatility_regime = tag_text.split(":", 1)[1] or volatility_regime
        return f"{trend_alignment}|{volatility_regime}"

    def _build_trade_case_clusters(self, trade_cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        closed_cases = [row for row in trade_cases if str(row.get("status") or "") == "closed"]
        if not closed_cases:
            return []
        extracted = [self._extract_trade_case_cluster_features(row) for row in closed_cases]
        standardized_vectors = self._standardize_trade_case_vectors(closed_cases)
        clusters = self._cluster_trade_case_rows(
            closed_cases=closed_cases,
            extracted=extracted,
            standardized_vectors=standardized_vectors,
            distance_threshold=4.0,
        )

        summaries = []
        for index, cluster in enumerate(clusters, start=1):
            cases = cluster["cases"]
            features = cluster["features"]
            count = len(cases)
            internal_metrics = self._build_trade_case_cluster_internal_metrics(index - 1, clusters)
            temporal_metrics = self._build_trade_case_cluster_temporal_metrics(cluster)
            symbol_counts: Dict[str, int] = {}
            failure_counts: Dict[str, int] = {}
            trend_counts: Dict[str, int] = {}
            volatility_counts: Dict[str, int] = {}
            loss_count = 0
            win_count = 0
            avg_estimated_pnl_pct = 0.0
            avg_slippage_bps = 0.0
            avg_observed_mfe_pct = 0.0
            avg_observed_mae_pct = 0.0
            trade_case_ids = []
            for row, feature in zip(cases, features):
                symbol_counts[feature["symbol"]] = symbol_counts.get(feature["symbol"], 0) + 1
                failure_counts[feature["failure_reason"]] = failure_counts.get(feature["failure_reason"], 0) + 1
                trend_counts[feature["trend_alignment"]] = trend_counts.get(feature["trend_alignment"], 0) + 1
                volatility_counts[feature["volatility_regime"]] = volatility_counts.get(feature["volatility_regime"], 0) + 1
                estimated_pnl_pct = feature["estimated_pnl_pct"]
                realized_pnl = feature["realized_pnl"]
                if (estimated_pnl_pct is not None and estimated_pnl_pct < 0) or (realized_pnl is not None and realized_pnl < 0):
                    loss_count += 1
                elif (estimated_pnl_pct is not None and estimated_pnl_pct > 0) or (realized_pnl is not None and realized_pnl > 0):
                    win_count += 1
                avg_estimated_pnl_pct += estimated_pnl_pct or 0.0
                avg_slippage_bps += feature["slippage_bps"]
                avg_observed_mfe_pct += feature["observed_mfe_pct"]
                avg_observed_mae_pct += feature["observed_mae_pct"]
                trade_case_id = row.get("trade_case_id")
                if trade_case_id and trade_case_id not in trade_case_ids:
                    trade_case_ids.append(trade_case_id)
            dominant_symbol = max(symbol_counts.items(), key=lambda item: (item[1], item[0]))[0]
            dominant_failure_reason = max(failure_counts.items(), key=lambda item: (item[1], item[0]))[0]
            dominant_trend = max(trend_counts.items(), key=lambda item: (item[1], item[0]))[0]
            dominant_volatility = max(volatility_counts.items(), key=lambda item: (item[1], item[0]))[0]
            silhouette_component = (float(internal_metrics.get("silhouette_score") or 0.0) + 1.0) / 2.0
            nearest_cluster_distance = internal_metrics.get("nearest_cluster_distance")
            avg_intra_distance = float(internal_metrics.get("avg_intra_distance") or 0.0)
            separation_component = (
                float(nearest_cluster_distance) / max(1e-6, float(nearest_cluster_distance) + avg_intra_distance)
                if nearest_cluster_distance is not None
                else 0.5
            )
            temporal_component = float(temporal_metrics.get("consistency_score") or 0.0)
            support_component = min(1.0, count / 4.0)
            quality_score = max(
                0.0,
                min(
                    1.0,
                    (silhouette_component * 0.35)
                    + (separation_component * 0.25)
                    + (temporal_component * 0.25)
                    + (support_component * 0.15),
                ),
            )
            if quality_score >= 0.65:
                quality_label = "validated"
            elif quality_score >= 0.45:
                quality_label = "monitor"
            else:
                quality_label = "weak"
            retired = False
            retirement_reason = None
            holdout_loss_rate = temporal_metrics.get("holdout_loss_rate")
            loss_rate_gap = temporal_metrics.get("loss_rate_gap")
            centroid_drift = temporal_metrics.get("centroid_drift")
            silhouette_score = internal_metrics.get("silhouette_score")
            if quality_score < 0.35:
                retired = True
                retirement_reason = "low_quality_score"
            elif temporal_metrics.get("holdout_count", 0) >= 2 and holdout_loss_rate is not None and float(holdout_loss_rate) < 0.5:
                retired = True
                retirement_reason = "holdout_not_confirmed"
            elif loss_rate_gap is not None and float(loss_rate_gap) > 0.5:
                retired = True
                retirement_reason = "unstable_holdout_gap"
            elif centroid_drift is not None and silhouette_score is not None and float(centroid_drift) > 3.0 and float(silhouette_score) < 0.05:
                retired = True
                retirement_reason = "cluster_drift"
            summaries.append(
                {
                    "cluster_id": f"cluster-{index}",
                    "algorithm": "online_centroid_euclidean",
                    "symbol": dominant_symbol,
                    "thesis_failure_reason": dominant_failure_reason,
                    "trend_alignment": dominant_trend,
                    "volatility_regime": dominant_volatility,
                    "count": count,
                    "loss_count": loss_count,
                    "win_count": win_count,
                    "loss_rate": round(loss_count / max(1, count), 4),
                    "avg_estimated_pnl_pct": round(avg_estimated_pnl_pct / max(1, count), 4),
                    "avg_slippage_bps": round(avg_slippage_bps / max(1, count), 4),
                    "avg_observed_mfe_pct": round(avg_observed_mfe_pct / max(1, count), 4),
                    "avg_observed_mae_pct": round(avg_observed_mae_pct / max(1, count), 4),
                    "quality_score": round(quality_score, 4),
                    "quality_label": quality_label,
                    "retired": retired,
                    "retirement_reason": retirement_reason,
                    "evaluation": {
                        **internal_metrics,
                        **temporal_metrics,
                    },
                    "numeric_feature_centroid": {
                        "spread_percentile_1h": round(sum(feature["vector"][0] for feature in features) / max(1, count), 4),
                        "oi_change_15m": round(sum(feature["vector"][1] for feature in features) / max(1, count), 4),
                        "volume_ratio_15m": round(sum(feature["vector"][2] for feature in features) / max(1, count), 4),
                        "distance_to_invalid_level": round(sum(feature["vector"][3] for feature in features) / max(1, count), 4),
                        "slippage_bps": round(sum(feature["vector"][4] for feature in features) / max(1, count), 4),
                        "observed_mfe_pct": round(sum(feature["vector"][5] for feature in features) / max(1, count), 4),
                        "observed_mae_pct": round(sum(feature["vector"][6] for feature in features) / max(1, count), 4),
                    },
                    "trade_case_ids": trade_case_ids[:10],
                }
            )
        summaries.sort(key=lambda item: (-item["quality_score"], -item["loss_count"], -item["count"], item["cluster_id"]))
        return summaries[:12]

    def _build_trade_case_candidate_lessons(self, trade_case_patterns: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        failure_reason_counts = trade_case_patterns.get("failure_reason_counts") or {}
        for reason, data in sorted(failure_reason_counts.items(), key=lambda item: (-item[1].get("loss_count", 0), item[0])):
            count = int(data.get("count") or 0)
            loss_count = int(data.get("loss_count") or 0)
            if count < 2 or loss_count < 2:
                continue
            symbols = data.get("symbols") or []
            symbol_text = ", ".join(symbols) if symbols else "portfolio"
            candidates.append(
                {
                    "rule": (
                        f"Treat repeated {reason} case closures for {symbol_text} as a risk-control pattern and reduce "
                        "aggressiveness until the setup quality improves."
                    ),
                    "regime": "case_pattern_general",
                    "category": "risk_control",
                    "support_count": count,
                    "evidence_journal_ids": [],
                    "hypothesis": count < 3,
                    "apply_when": {
                        "regime": ["case_pattern_general"],
                        "volatility": ["unknown"],
                        "trend": ["mixed"],
                    },
                    "ignore_when": [
                        "Ignore when the same setup closes green with improving execution quality and stable excursion."
                    ],
                    "seed_signal": f"trade_case_failure:{reason}",
                    "seed_origin": "trade_case_candidate",
                    "trade_case_ids": data.get("trade_case_ids") or [],
                }
            )
        return candidates[:10]

    def _build_trade_case_cluster_candidate_lessons(self, trade_case_clusters: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        for cluster in trade_case_clusters:
            count = int(cluster.get("count") or 0)
            loss_count = int(cluster.get("loss_count") or 0)
            quality_score = float(cluster.get("quality_score") or 0.0)
            if count < 2 or loss_count < 2 or quality_score < 0.35 or bool(cluster.get("retired")):
                continue
            symbol = str(cluster.get("symbol") or "portfolio")
            failure_reason = str(cluster.get("thesis_failure_reason") or "unknown")
            trend_alignment = str(cluster.get("trend_alignment") or "unknown")
            volatility_regime = str(cluster.get("volatility_regime") or "unknown")
            avg_slippage_bps = float(cluster.get("avg_slippage_bps") or 0.0)
            centroid = cluster.get("numeric_feature_centroid") or {}
            evaluation = cluster.get("evaluation") or {}
            candidates.append(
                {
                    "rule": (
                        f"For {symbol}, treat the repeated {failure_reason} cluster in {trend_alignment}/{volatility_regime} "
                        f"conditions as a defensive pattern. The cluster repeatedly combines spread percentile "
                        f"{float(centroid.get('spread_percentile_1h') or 0.0):.1f}, volume ratio "
                        f"{float(centroid.get('volume_ratio_15m') or 0.0):.2f}, invalidation distance "
                        f"{float(centroid.get('distance_to_invalid_level') or 0.0):.2f}, and slippage "
                        f"{avg_slippage_bps:.2f} bps. Cluster quality={quality_score:.2f}, "
                        f"silhouette={float(evaluation.get('silhouette_score') or 0.0):.2f}, "
                        f"holdout_gap={float(evaluation.get('loss_rate_gap') or 0.0):.2f}."
                    ),
                    "regime": "case_cluster",
                    "category": "risk_control",
                    "support_count": count,
                    "evidence_journal_ids": [],
                    "hypothesis": count < 3 or quality_score < 0.65,
                    "apply_when": {
                        "regime": ["case_cluster"],
                        "volatility": [volatility_regime or "unknown"],
                        "trend": [trend_alignment or "unknown"],
                    },
                    "ignore_when": [
                        "Ignore when the same cluster begins closing green with tighter slippage and healthier excursion."
                    ],
                    "seed_signal": f"trade_case_cluster:{symbol}:{failure_reason}",
                    "seed_origin": "trade_case_cluster_candidate",
                    "trade_case_ids": cluster.get("trade_case_ids") or [],
                }
            )
        return candidates[:10]

    def _merge_candidate_lesson_dicts(self, *groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        seen = set()
        for group in groups:
            for item in group or []:
                key = str(item.get("rule") or "").strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(item)
        return merged[:15]

    def _build_candidate_lessons(
        self,
        journals: List[Dict[str, Any]],
        winning_evidence_signals: Dict[str, int],
        losing_evidence_signals: Dict[str, int],
        evidence_quality_patterns: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        signal_examples = self._build_signal_examples(journals)
        for signal_key, wins in sorted(winning_evidence_signals.items(), key=lambda item: (-item[1], item[0])):
            losses = losing_evidence_signals.get(signal_key, 0)
            if wins < 2 or wins <= losses:
                continue
            example = signal_examples.get(signal_key) or {}
            candidates.append(
                self._build_candidate_lesson(
                    signal_key=signal_key,
                    kind="opportunity",
                    support_count=wins,
                    evidence_journal_ids=example.get("journal_ids") or [],
                    regime_summary=example.get("regime_summary") or {},
                )
            )
        for signal_key, losses in sorted(losing_evidence_signals.items(), key=lambda item: (-item[1], item[0])):
            wins = winning_evidence_signals.get(signal_key, 0)
            if losses < 2 or losses <= wins:
                continue
            example = signal_examples.get(signal_key) or {}
            candidates.append(
                self._build_candidate_lesson(
                    signal_key=signal_key,
                    kind="anti_pattern",
                    support_count=losses,
                    evidence_journal_ids=example.get("journal_ids") or [],
                    regime_summary=example.get("regime_summary") or {},
                )
            )
        candidates.extend(self._build_quality_pattern_candidates(evidence_quality_patterns))
        return candidates[:10]

    def _build_evidence_quality_patterns(self, journals: List[Dict[str, Any]]) -> Dict[str, Any]:
        buckets: Dict[str, Dict[str, Any]] = {
            "low": {
                "count": 0,
                "win_count": 0,
                "loss_count": 0,
                "avg_estimated_pnl_pct": 0.0,
                "journal_ids": [],
                "regime_summary": {},
            },
            "reduced": {
                "count": 0,
                "win_count": 0,
                "loss_count": 0,
                "avg_estimated_pnl_pct": 0.0,
                "journal_ids": [],
                "regime_summary": {},
            },
            "full": {
                "count": 0,
                "win_count": 0,
                "loss_count": 0,
                "avg_estimated_pnl_pct": 0.0,
                "journal_ids": [],
                "regime_summary": {},
            },
            "unknown": {
                "count": 0,
                "win_count": 0,
                "loss_count": 0,
                "avg_estimated_pnl_pct": 0.0,
                "journal_ids": [],
                "regime_summary": {},
            },
        }
        adjusted_cap = {
            "count": 0,
            "win_count": 0,
            "loss_count": 0,
            "avg_estimated_pnl_pct": 0.0,
            "journal_ids": [],
            "regime_summary": {},
        }
        unadjusted_cap = {
            "count": 0,
            "win_count": 0,
            "loss_count": 0,
            "avg_estimated_pnl_pct": 0.0,
            "journal_ids": [],
            "regime_summary": {},
        }
        issue_counts: Dict[str, int] = {}

        for row in journals:
            pnl = float(row.get("estimated_pnl_pct") or 0.0)
            journal_id = row.get("id")
            evidence_quality = row.get("evidence_quality") or {}
            entry_size_cap = row.get("entry_size_cap") or {}
            factor = self._coerce_float(evidence_quality.get("factor"))
            bucket_key = self._classify_evidence_quality_bucket(factor)
            bucket = buckets[bucket_key]
            regime_summary = self._extract_market_regime_summary(row.get("market_state") or {})
            self._update_quality_counter(bucket, pnl, journal_id, regime_summary)
            for issue in evidence_quality.get("issues") or []:
                issue_key = str(issue).strip()
                if issue_key:
                    issue_counts[issue_key] = issue_counts.get(issue_key, 0) + 1

            if self._is_adjusted_cap(entry_size_cap):
                self._update_quality_counter(adjusted_cap, pnl, journal_id, regime_summary)
            else:
                self._update_quality_counter(unadjusted_cap, pnl, journal_id, regime_summary)

        for bucket in buckets.values():
            bucket["avg_estimated_pnl_pct"] = self._finalize_avg(bucket)
        adjusted_cap["avg_estimated_pnl_pct"] = self._finalize_avg(adjusted_cap)
        unadjusted_cap["avg_estimated_pnl_pct"] = self._finalize_avg(unadjusted_cap)

        return {
            "factor_buckets": buckets,
            "issue_counts": issue_counts,
            "adjusted_cap": adjusted_cap,
            "unadjusted_cap": unadjusted_cap,
            "low_quality_threshold": 0.8,
        }

    def _build_quality_pattern_candidates(self, evidence_quality_patterns: Dict[str, Any]) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        factor_buckets = evidence_quality_patterns.get("factor_buckets") or {}
        low_quality = factor_buckets.get("low") or {}
        adjusted_cap = evidence_quality_patterns.get("adjusted_cap") or {}
        if (low_quality.get("count") or 0) >= 2 and (low_quality.get("loss_count") or 0) > (low_quality.get("win_count") or 0):
            regime_summary = low_quality.get("regime_summary") or {}
            candidates.append(
                {
                    "rule": (
                        "Treat low evidence-quality setups as risk-control cases; prefer reduced size "
                        "or HOLD until broader confirmation appears."
                    ),
                    "regime": self._derive_regime_label(regime_summary),
                    "category": "risk_control",
                    "support_count": int(low_quality.get("count") or 0),
                    "evidence_journal_ids": (low_quality.get("journal_ids") or [])[:10],
                    "hypothesis": int(low_quality.get("count") or 0) < 3,
                    "apply_when": self._derive_memory_condition(regime_summary).model_dump(),
                    "ignore_when": [
                        "Ignore when evidence quality returns to full coverage with diverse confirming signals."
                    ],
                    "seed_signal": "evidence_quality:low",
                    "seed_origin": "heuristic_quality_pattern",
                }
            )
        if (adjusted_cap.get("count") or 0) >= 2 and (adjusted_cap.get("loss_count") or 0) > (adjusted_cap.get("win_count") or 0):
            regime_summary = adjusted_cap.get("regime_summary") or {}
            candidates.append(
                {
                    "rule": (
                        "Keep reduced entry caps in place when evidence-quality adjustments are active; "
                        "those setups still underperform without stronger confirmation."
                    ),
                    "regime": self._derive_regime_label(regime_summary),
                    "category": "risk_control",
                    "support_count": int(adjusted_cap.get("count") or 0),
                    "evidence_journal_ids": (adjusted_cap.get("journal_ids") or [])[:10],
                    "hypothesis": int(adjusted_cap.get("count") or 0) < 3,
                    "apply_when": self._derive_memory_condition(regime_summary).model_dump(),
                    "ignore_when": [
                        "Ignore when entry caps are no longer reduced and evidence quality recovers to full strength."
                    ],
                    "seed_signal": "entry_size_cap:adjusted",
                    "seed_origin": "heuristic_quality_pattern",
                }
            )
        return candidates

    def _build_undertrading_candidate_lessons(
        self,
        *,
        hold_opportunity_summary: Dict[str, Any],
        journals: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        evaluation_count = int(hold_opportunity_summary.get("evaluation_count") or 0)
        missed_breakout_count = int(hold_opportunity_summary.get("missed_breakout_count") or 0)
        hold_opportunity_cost = float(hold_opportunity_summary.get("hold_opportunity_cost") or 0.0)
        if evaluation_count < 2 or missed_breakout_count < 2 or hold_opportunity_cost <= 0.5:
            return []
        journal_id_set = set(hold_opportunity_summary.get("evidence_journal_ids") or [])
        selected_journals = [
            row
            for row in journals
            if int(row.get("id") or row.get("journal_id") or 0) in journal_id_set
        ]
        regime_summary = {}
        for row in selected_journals:
            regime_summary = self._extract_market_regime_summary(row.get("market_state") or {})
            if regime_summary:
                break
        if not regime_summary:
            regime_label = str(hold_opportunity_summary.get("dominant_regime") or "general")
            trend, _, volatility = regime_label.partition("_")
            regime_summary = {
                "portfolio": {
                    "trend_alignment": trend or "mixed",
                    "volatility_regime": volatility or "unknown",
                }
            }
        dominant_trigger_context = str(hold_opportunity_summary.get("dominant_trigger_context") or "none")
        dominant_execution_context = str(hold_opportunity_summary.get("dominant_execution_context") or "unknown")
        dominant_trend_context = str(hold_opportunity_summary.get("dominant_trend_context") or "UNKNOWN|UNKNOWN")
        simple_rule_hits = int(hold_opportunity_summary.get("simple_rule_beats_hold_count") or 0)
        hold_quality_counts = hold_opportunity_summary.get("hold_quality_counts") or {}
        missed_quality = "missed_opportunity_hold" if hold_quality_counts.get("missed_opportunity_hold") else "probe_only_opportunity_hold"
        return [
            {
                "rule": (
                    "When "
                    f"{dominant_trigger_context} triggers appear with {dominant_execution_context} "
                    f"and trend context {dominant_trend_context}, avoid default HOLD and allow a small probe entry."
                ),
                "regime": self._derive_regime_label(regime_summary),
                "category": "opportunity",
                "support_count": missed_breakout_count,
                "evidence_journal_ids": list(journal_id_set)[:10],
                "hypothesis": evaluation_count < 4,
                "apply_when": self._derive_memory_condition(regime_summary).model_dump(),
                "ignore_when": [
                    "Ignore when the trigger mix changes materially from the stored context.",
                    "Ignore when spread/depth context degrades relative to the stored execution bucket.",
                    "Ignore when risk triggers dominate or the 1h trend no longer supports the probe direction.",
                ],
                "trigger_context": dominant_trigger_context,
                "execution_context": dominant_execution_context,
                "trend_context": dominant_trend_context,
                "hold_quality": missed_quality,
                "simple_rule_confirmation_count": simple_rule_hits,
                "seed_signal": "hold_opportunity_counterfactual",
                "seed_origin": "hold_opportunity_counterfactual",
            }
        ]

    def _build_overtrading_candidate_lessons(
        self,
        *,
        trading_balance_summary: Dict[str, Any],
        journals: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if str(trading_balance_summary.get("stance") or "") != "overtrading":
            return []
        false_entry_cost = float(trading_balance_summary.get("false_entry_cost") or 0.0)
        loss_count = int(trading_balance_summary.get("loss_count") or 0)
        if loss_count < 2 or false_entry_cost <= 0.5:
            return []
        regime_summary = {}
        for row in journals or []:
            regime_summary = self._extract_market_regime_summary(row.get("market_state") or {})
            if regime_summary:
                break
        if not regime_summary:
            regime_summary = {
                "portfolio": {
                    "trend_alignment": "mixed",
                    "volatility_regime": "unknown",
                }
            }
        return [
            {
                "rule": (
                    "When repeated entry attempts produce losses faster than missed-opportunity cost, "
                    "tighten entry filters and require stronger confirmation before opening new exposure."
                ),
                "regime": self._derive_regime_label(regime_summary),
                "category": "risk_control",
                "support_count": loss_count,
                "evidence_journal_ids": [],
                "hypothesis": loss_count < 4,
                "apply_when": self._derive_memory_condition(regime_summary).model_dump(),
                "ignore_when": [
                    "Ignore when missed-opportunity pressure exceeds false-entry cost and trigger quality remains strong."
                ],
                "seed_signal": "overtrading_balance_summary",
                "seed_origin": "trading_balance_summary",
            }
        ]

    def _classify_evidence_quality_bucket(self, factor: float | None) -> str:
        if factor is None:
            return "unknown"
        if factor < 0.8:
            return "low"
        if factor < 1.0:
            return "reduced"
        return "full"

    def _coerce_float(self, value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _update_quality_counter(
        self,
        bucket: Dict[str, Any],
        pnl: float,
        journal_id: Any,
        regime_summary: Dict[str, Any],
    ) -> None:
        bucket["count"] = int(bucket.get("count") or 0) + 1
        bucket["pnl_sum"] = float(bucket.get("pnl_sum") or 0.0) + pnl
        if pnl > 0:
            bucket["win_count"] = int(bucket.get("win_count") or 0) + 1
        elif pnl < 0:
            bucket["loss_count"] = int(bucket.get("loss_count") or 0) + 1
        if journal_id is not None and journal_id not in (bucket.get("journal_ids") or []):
            bucket.setdefault("journal_ids", []).append(journal_id)
        if regime_summary and not bucket.get("regime_summary"):
            bucket["regime_summary"] = regime_summary

    def _finalize_avg(self, bucket: Dict[str, Any]) -> float:
        count = int(bucket.get("count") or 0)
        avg = round(float(bucket.get("pnl_sum") or 0.0) / count, 4) if count else 0.0
        bucket.pop("pnl_sum", None)
        return avg

    def _is_adjusted_cap(self, entry_size_cap: Dict[str, Any]) -> bool:
        if not entry_size_cap:
            return False
        if entry_size_cap.get("adjusted") is True:
            return True
        base_cap = self._coerce_float(entry_size_cap.get("base_cap_pct"))
        effective_cap = self._coerce_float(entry_size_cap.get("effective_cap_pct"))
        if base_cap is None or effective_cap is None:
            return False
        return effective_cap < base_cap

    def _build_signal_examples(self, journals: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        examples: Dict[str, Dict[str, Any]] = {}
        for row in journals:
            regime_summary = self._extract_market_regime_summary(row.get("market_state") or {})
            journal_id = row.get("id")
            for signal_key in self._extract_evidence_keys(row.get("rationale_json") or []):
                item = examples.setdefault(
                    signal_key,
                    {"journal_ids": [], "regime_summary": regime_summary},
                )
                if journal_id is not None and journal_id not in item["journal_ids"]:
                    item["journal_ids"].append(journal_id)
        return examples

    def _build_candidate_lesson(
        self,
        signal_key: str,
        kind: str,
        support_count: int,
        evidence_journal_ids: List[int],
        regime_summary: Dict[str, Any],
    ) -> Dict[str, Any]:
        symbol, signal, timeframe = self._parse_signal_key(signal_key)
        symbol_label = symbol or "portfolio"
        regime = self._derive_regime_label(regime_summary)
        is_anti_pattern = kind == "anti_pattern"
        return {
            "rule": (
                f"Avoid setups driven by {signal} on {timeframe} for {symbol_label} "
                f"when the same evidence repeatedly underperforms."
                if is_anti_pattern
                else f"Favor setups confirmed by {signal} on {timeframe} for {symbol_label} "
                f"when the same evidence repeatedly outperforms."
            ),
            "regime": regime,
            "category": "anti_pattern" if is_anti_pattern else "opportunity",
            "support_count": support_count,
            "evidence_journal_ids": evidence_journal_ids[:10],
            "hypothesis": support_count < 3,
            "apply_when": self._derive_memory_condition(regime_summary).model_dump(),
            "ignore_when": [
                f"Ignore when {signal} on {timeframe} is present without confirming regime alignment."
            ],
            "seed_signal": signal_key,
            "seed_origin": "heuristic_candidate",
        }

    def _parse_signal_key(self, signal_key: str) -> tuple[str | None, str, str]:
        symbol = None
        remainder = signal_key
        if ":" in signal_key:
            symbol, remainder = signal_key.split(":", 1)
        if "@" in remainder:
            signal, timeframe = remainder.split("@", 1)
        else:
            signal, timeframe = remainder, "context"
        return symbol, signal, timeframe

    def _derive_regime_label(self, regime_summary: Dict[str, Any]) -> str:
        first = next(iter(regime_summary.values()), {})
        volatility = first.get("volatility_regime") or "general"
        trend = first.get("trend_alignment") or "mixed"
        return f"{trend}_{volatility}"

    def _derive_memory_condition(self, regime_summary: Dict[str, Any]) -> MemoryCondition:
        first = next(iter(regime_summary.values()), {})
        trend = first.get("trend_alignment") or "mixed"
        volatility = first.get("volatility_regime") or "unknown"
        return MemoryCondition(
            regime=[self._derive_regime_label(regime_summary)],
            volatility=[volatility],
            trend=[trend],
        )

    def _extract_evidence_keys(self, rationale_json: List[Dict[str, Any]]) -> List[str]:
        keys: List[str] = []
        seen = set()
        for item in rationale_json:
            for evidence in (item or {}).get("evidence") or []:
                signal = evidence.get("signal") or "signal"
                timeframe = evidence.get("timeframe") or "context"
                symbol = evidence.get("symbol")
                key = f"{symbol}:{signal}@{timeframe}" if symbol else f"{signal}@{timeframe}"
                if key in seen:
                    continue
                seen.add(key)
                keys.append(key)
        return keys

    def _extract_market_regime_summary(self, market_state: Dict[str, Any]) -> Dict[str, Any]:
        snapshots = (market_state or {}).get("market_snapshots") or {}
        summary = {}
        for symbol, snapshot in snapshots.items():
            regime = (snapshot or {}).get("regime") or {}
            summary[symbol] = {
                "trend_alignment": regime.get("trend_alignment"),
                "volatility_regime": regime.get("volatility_regime"),
                "opportunity_trigger_reasons": (snapshot or {}).get("opportunity_trigger_reasons") or [],
                "risk_trigger_reasons": (snapshot or {}).get("risk_trigger_reasons") or [],
            }
        return summary

    def _build_structured_evidence_summary(self, rationale_json: List[Dict[str, Any]]) -> List[str]:
        summaries: List[str] = []
        for item in rationale_json[:3]:
            for evidence in (item or {}).get("evidence") or []:
                signal = evidence.get("signal") or "signal"
                timeframe = evidence.get("timeframe") or "context"
                observed = evidence.get("observed_value")
                operator = evidence.get("operator") or "note"
                threshold = evidence.get("threshold")
                symbol = evidence.get("symbol")
                prefix = f"{symbol} " if symbol else ""
                if threshold is None:
                    summaries.append(f"{prefix}{signal}@{timeframe}={observed}")
                else:
                    summaries.append(f"{prefix}{signal}@{timeframe}={observed} {operator} {threshold}")
        return summaries[:6]

    def _create_prompt(self, stats_package: Dict[str, Any]) -> str:
        stats_json = json.dumps(stats_package, indent=2)
        return f"""
You are the long-term memory synthesizer for an automated trading system.
Your job is to convert computed trading statistics into grounded lessons.

Rules:
1. Use only the provided statistics and evidence journal IDs.
2. Do not invent profit factors, samples, or evidence.
3. A lesson with support_count < 3 must be marked hypothesis=true.
4. Favor anti-patterns and risk-control rules over generic advice.
5. Every lesson must include apply_when.regime, apply_when.volatility, and apply_when.trend.
6. Every lesson must include ignore_when with at least one concrete counterexample condition.
7. Return only JSON matching the schema.
8. All text must be in English.
9. Prefer lessons tied to repeated evidence signals when winning_evidence_signals or losing_evidence_signals clearly recur.
10. When using recent_examples, top_wins, or top_losses, use rationale_json evidence, evidence_summary, and market_regime only. Ignore free-form rationale text.
11. candidate_lessons are heuristic seeds. Refine, merge, or discard them, but do not invent unsupported lessons outside the supplied evidence.
12. When evidence_quality_patterns show low evidence quality or adjusted entry caps clustering with losses, prefer explicit risk_control lessons over generic commentary.
13. recent_trade_cases and trade_case_patterns are case-level evidence. Prefer them over free-form journal text when they show repeated failure reasons, repeated signal failures, or consistently poor slippage/excursion quality.
14. trade_case_clusters summarize repeated lifecycle outcomes under similar symbol/failure/regime conditions. Prefer cluster-backed risk_control lessons when a cluster has multiple losses with consistent slippage or MAE pressure.
15. If a candidate opportunity lesson includes trigger_context, execution_context, trend_context, or hold_quality, preserve them unless the supplied evidence clearly contradicts them.

Computed statistics:
{stats_json}
"""

    def _generate_lessons(self, stats_package: Dict[str, Any]) -> List[MemoryLesson]:
        if stats_package["journal_count"] == 0:
            return []

        candidate_lessons = [
            MemoryLesson.model_validate(self._normalize_memory_lesson_payload(item))
            for item in stats_package.get("candidate_lessons") or []
        ]
        try:
            provider = getattr(self, "llm_provider", None)
            if provider is None:
                raise RuntimeError("MemoryUpdater is missing llm_provider.")
            response = provider.generate_json(
                prompt=self._create_prompt(stats_package),
                model_name=self.model_name,
                schema=list[MemoryLesson],
                temperature=0.2,
            )
            raw_text = response.text
            lessons = json.loads(raw_text)
            if not isinstance(lessons, list):
                return candidate_lessons
            generated_lessons = [
                MemoryLesson.model_validate(self._normalize_memory_lesson_payload(item))
                for item in lessons
            ]
            return self._merge_candidate_lessons(candidate_lessons, generated_lessons)
        except Exception as e:
            logger.error(
                f"{getattr(self, 'provider_name', 'AI').upper()} failed to extract lessons: {e}"
            )
            return candidate_lessons

    def _find_matching_candidate_lesson(
        self,
        candidate_lessons: List[MemoryLesson],
        lesson: MemoryLesson,
    ) -> MemoryLesson | None:
        best_match = None
        best_score = 0.0
        lesson_evidence = set(int(item) for item in (lesson.evidence_journal_ids or []))
        lesson_apply = lesson.apply_when.model_dump()
        for candidate in candidate_lessons:
            score = self._memory_rule_similarity(candidate.rule, lesson.rule)
            candidate_evidence = set(int(item) for item in (candidate.evidence_journal_ids or []))
            if lesson_evidence and candidate_evidence and lesson_evidence.intersection(candidate_evidence):
                score += 0.35
            if candidate.category == lesson.category:
                score += 0.15
            if str(candidate.regime or "").strip().lower() == str(lesson.regime or "").strip().lower():
                score += 0.15
            candidate_apply = candidate.apply_when.model_dump()
            if candidate_apply.get("regime") == lesson_apply.get("regime"):
                score += 0.1
            if candidate.trigger_context and candidate.trigger_context == lesson.trigger_context:
                score += 0.1
            if candidate.execution_context and candidate.execution_context == lesson.execution_context:
                score += 0.1
            if candidate.trend_context and candidate.trend_context == lesson.trend_context:
                score += 0.1
            if score > best_score:
                best_score = score
                best_match = candidate
        return best_match if best_match is not None and best_score >= 0.45 else None

    def _constrain_generated_lesson(
        self,
        seed: MemoryLesson,
        generated: MemoryLesson,
    ) -> MemoryLesson:
        generated_apply = generated.apply_when.model_dump()
        seed_apply = seed.apply_when.model_dump()
        constrained_apply = {
            "regime": seed_apply.get("regime") or generated_apply.get("regime") or [],
            "volatility": seed_apply.get("volatility") or generated_apply.get("volatility") or [],
            "trend": seed_apply.get("trend") or generated_apply.get("trend") or [],
        }
        return generated.model_copy(
            update={
                "regime": seed.regime or generated.regime,
                "category": seed.category or generated.category,
                "apply_when": MemoryCondition.model_validate(constrained_apply),
                "ignore_when": self._merge_unique_list(seed.ignore_when, generated.ignore_when, limit=6),
                "evidence_journal_ids": self._merge_unique_list(
                    seed.evidence_journal_ids,
                    generated.evidence_journal_ids,
                    limit=20,
                ),
                "support_count": max(seed.support_count, generated.support_count),
                "trigger_context": seed.trigger_context or generated.trigger_context,
                "execution_context": seed.execution_context or generated.execution_context,
                "trend_context": seed.trend_context or generated.trend_context,
                "hold_quality": seed.hold_quality or generated.hold_quality,
                "hypothesis": bool(seed.hypothesis and generated.hypothesis),
            }
        )

    def _merge_candidate_lessons(
        self,
        candidate_lessons: List[MemoryLesson],
        generated_lessons: List[MemoryLesson],
    ) -> List[MemoryLesson]:
        merged_by_rule: Dict[str, MemoryLesson] = {}
        for lesson in candidate_lessons:
            key = lesson.rule.strip().lower()
            if key not in merged_by_rule:
                merged_by_rule[key] = lesson
        for lesson in generated_lessons:
            seed = self._find_matching_candidate_lesson(candidate_lessons, lesson)
            if candidate_lessons and seed is None:
                continue
            constrained = self._constrain_generated_lesson(seed, lesson) if seed is not None else lesson
            key = (seed.rule if seed is not None else constrained.rule).strip().lower()
            existing = merged_by_rule.get(key)
            if existing is None:
                merged_by_rule[key] = constrained
                continue
            merged_by_rule[key] = existing.model_copy(
                update={
                    "rule": existing.rule if seed is not None else (constrained.rule if len(constrained.rule) >= len(existing.rule) else existing.rule),
                    "support_count": max(existing.support_count, constrained.support_count),
                    "evidence_journal_ids": self._merge_unique_list(
                        existing.evidence_journal_ids,
                        constrained.evidence_journal_ids,
                        limit=20,
                    ),
                    "ignore_when": self._merge_unique_list(
                        existing.ignore_when,
                        constrained.ignore_when,
                        limit=6,
                    ),
                    "trigger_context": existing.trigger_context or constrained.trigger_context,
                    "execution_context": existing.execution_context or constrained.execution_context,
                    "trend_context": existing.trend_context or constrained.trend_context,
                    "hold_quality": existing.hold_quality or constrained.hold_quality,
                    "apply_when": existing.apply_when if seed is not None else constrained.apply_when,
                    "regime": existing.regime if seed is not None else (existing.regime or constrained.regime),
                    "category": existing.category if seed is not None else constrained.category,
                    "hypothesis": bool(existing.hypothesis and constrained.hypothesis),
                }
            )
        return list(merged_by_rule.values())

    def _normalize_memory_rule(self, rule: str) -> str:
        text = " ".join(str(rule or "").lower().split())
        normalized = "".join(char if char.isalnum() or char.isspace() else " " for char in text)
        parts = []
        for part in normalized.split():
            if len(part) > 4 and part.endswith("s"):
                part = part[:-1]
            parts.append(part)
        return " ".join(part for part in parts if part)

    def _tokenize_memory_rule(self, rule: str) -> set[str]:
        return {token for token in self._normalize_memory_rule(rule).split() if len(token) > 2}

    def _memory_rule_similarity(self, left: str, right: str) -> float:
        left_tokens = self._tokenize_memory_rule(left)
        right_tokens = self._tokenize_memory_rule(right)
        if not left_tokens or not right_tokens:
            return 0.0
        union = left_tokens | right_tokens
        if not union:
            return 0.0
        return len(left_tokens & right_tokens) / len(union)

    def _build_memory_canonical_key(self, rule: str, metrics: Dict[str, Any]) -> str:
        category = str(metrics.get("category") or "lesson").strip().lower()
        seed_signal = str(metrics.get("seed_signal") or "").strip().lower()
        apply_when = metrics.get("apply_when") or {}
        regime = str(metrics.get("regime") or "general").strip().lower()
        volatility = "-".join(sorted(str(item).strip().lower() for item in (apply_when.get("volatility") or []) if item))
        trend = "-".join(sorted(str(item).strip().lower() for item in (apply_when.get("trend") or []) if item))
        trigger_context = str(metrics.get("trigger_context") or "").strip().lower()
        execution_context = str(metrics.get("execution_context") or "").strip().lower()
        trend_context = str(metrics.get("trend_context") or "").strip().lower()
        normalized_rule = self._normalize_memory_rule(rule)
        rule_anchor = " ".join(normalized_rule.split()[:5])
        anchor = seed_signal or rule_anchor or "memory"
        return "|".join(
            part
            for part in (
                category,
                regime,
                volatility,
                trend,
                trigger_context,
                execution_context,
                trend_context,
                anchor,
            )
            if part
        )

    def _compute_memory_confidence_score(
        self,
        *,
        support_count: int,
        evidence_count: int,
        status: str,
        category: str,
        stats_package: Dict[str, Any],
        hypothesis: bool,
    ) -> float:
        support_component = min(1.0, max(0.0, support_count) / 6.0)
        evidence_component = min(1.0, max(0.0, evidence_count) / 6.0)
        status_component = 1.0 if status == "rule" else 0.55
        category_component = 1.0 if category in {"risk_control", "anti_pattern"} else 0.8
        cluster_quality = float((stats_package.get("trade_case_cluster_evaluation") or {}).get("avg_quality_score") or 0.5)
        retrieval_summary = stats_package.get("retrieval_counterfactual_summary") or {}
        with_better = float(retrieval_summary.get("with_retrieval_better_count") or 0.0)
        without_better = float(retrieval_summary.get("without_retrieval_better_count") or 0.0)
        tie_count = float(retrieval_summary.get("outcome_tie_count") or 0.0)
        retrieval_total = with_better + without_better + tie_count
        retrieval_component = (
            (with_better + (tie_count * 0.5)) / retrieval_total
            if retrieval_total
            else 0.5
        )
        calibration_summary = stats_package.get("confidence_calibration_summary") or {}
        calibration_component = float(calibration_summary.get("alignment_score") or 0.5)
        confidence = (
            support_component * 0.31
            + evidence_component * 0.15
            + status_component * 0.16
            + category_component * 0.08
            + cluster_quality * 0.14
            + retrieval_component * 0.10
            + calibration_component * 0.06
        )
        if hypothesis:
            confidence *= 0.78
        return round(max(0.05, min(0.99, confidence)), 4)

    def _merge_unique_list(self, *values: List[Any], limit: int | None = None) -> List[Any]:
        merged: List[Any] = []
        seen = set()
        for group in values:
            for item in group or []:
                key = json.dumps(item, sort_keys=True, default=str) if isinstance(item, (dict, list)) else str(item)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
                if limit is not None and len(merged) >= limit:
                    return merged
        return merged

    def _merge_memory_entry_pair(self, base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
        base_metrics = dict(base.get("metrics") or {})
        incoming_metrics = dict(incoming.get("metrics") or {})
        merged = dict(base if len(str(base.get("rule") or "")) >= len(str(incoming.get("rule") or "")) else incoming)
        merged_metrics = dict(base_metrics)
        evidence_ids = self._merge_unique_list(
            base_metrics.get("evidence_journal_ids") or [],
            incoming_metrics.get("evidence_journal_ids") or [],
            limit=20,
        )
        support_floor = max(
            int(base_metrics.get("support") or 0),
            int(incoming_metrics.get("support") or 0),
            len(evidence_ids),
        )
        merged_metrics.update(incoming_metrics)
        merged_metrics["support"] = support_floor
        merged_metrics["evidence_journal_ids"] = evidence_ids
        merged_metrics["ignore_when"] = self._merge_unique_list(
            base_metrics.get("ignore_when") or [],
            incoming_metrics.get("ignore_when") or [],
            limit=4,
        )
        merged_metrics["candidate_lessons"] = self._merge_unique_list(
            base_metrics.get("candidate_lessons") or [],
            incoming_metrics.get("candidate_lessons") or [],
            limit=6,
        )
        merged_metrics["merged_rules"] = self._merge_unique_list(
            base_metrics.get("merged_rules") or [base.get("rule")],
            incoming_metrics.get("merged_rules") or [incoming.get("rule")],
            limit=8,
        )
        merged_metrics["merge_count"] = max(
            int(base_metrics.get("merge_count") or 1) + int(incoming_metrics.get("merge_count") or 1),
            len(merged_metrics["merged_rules"]),
        )
        merged_metrics["confidence_score"] = round(
            max(
                float(base_metrics.get("confidence_score") or 0.0),
                float(incoming_metrics.get("confidence_score") or 0.0),
            ),
            4,
        )
        merged_metrics["validity_score"] = round(
            max(
                float(base_metrics.get("validity_score") or merged_metrics["confidence_score"]),
                float(incoming_metrics.get("validity_score") or merged_metrics["confidence_score"]),
            ),
            4,
        )
        merged_metrics["decay_score"] = round(
            max(
                float(base_metrics.get("decay_score") or 1.0),
                float(incoming_metrics.get("decay_score") or 1.0),
            ),
            4,
        )
        merged["metrics"] = merged_metrics
        merged["rule"] = str(merged.get("rule") or incoming.get("rule") or base.get("rule") or "")
        merged["version"] = max(int(base.get("version", 1) or 1), int(incoming.get("version", 1) or 1))
        return merged

    def _merge_similar_memory_entries(self, entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        for entry in entries:
            metrics = entry.setdefault("metrics", {})
            metrics.setdefault("canonical_key", self._build_memory_canonical_key(entry.get("rule") or "", metrics))
            candidate_category = str(metrics.get("category") or "lesson")
            candidate_regime = str(metrics.get("regime") or "general")
            matched_index = None
            for index, existing in enumerate(merged):
                existing_metrics = existing.get("metrics") or {}
                if metrics.get("canonical_key") == existing_metrics.get("canonical_key"):
                    matched_index = index
                    break
                if candidate_category != str(existing_metrics.get("category") or "lesson"):
                    continue
                if candidate_regime != str(existing_metrics.get("regime") or "general"):
                    continue
                if self._memory_rule_similarity(entry.get("rule") or "", existing.get("rule") or "") >= 0.55:
                    matched_index = index
                    break
            if matched_index is None:
                merged.append(entry)
            else:
                merged[matched_index] = self._merge_memory_entry_pair(merged[matched_index], entry)
        return merged

    def _safe_parse_timestamp(self, value: Any, fallback: datetime) -> datetime:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        text = str(value or "").strip()
        if not text:
            return fallback
        for candidate in (text, text.replace("Z", "+00:00"), text.replace(" ", "T")):
            try:
                parsed = datetime.fromisoformat(candidate)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                continue
        return fallback

    def _build_decayed_memory_entry(self, row: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        metrics = dict(row.get("metrics") or {})
        timestamp = self._safe_parse_timestamp(row.get("timestamp"), fallback=now)
        age_days = max(0.0, (now - timestamp).total_seconds() / 86400.0)
        confidence = float(metrics.get("confidence_score") or 0.0)
        prior_validity = float(metrics.get("validity_score") or confidence or 0.0)
        decay_score = round(max(0.12, 1.0 - min(1.0, age_days / 90.0)), 4)
        freshness_score = round(max(0.0, 1.0 - min(1.0, age_days / 45.0)), 4)
        validity_score = round(max(0.0, min(0.99, (prior_validity * 0.65) + (decay_score * 0.35))), 4)
        retired = bool(metrics.get("retired"))
        retirement_reason = metrics.get("retirement_reason")
        if not retired and age_days >= 90.0:
            retired = True
            retirement_reason = "stale_timeout"
        elif not retired and age_days >= 30.0 and (confidence < 0.4 or validity_score < 0.35):
            retired = True
            retirement_reason = "stale_low_confidence"
        metrics["confidence_score"] = round(confidence, 4)
        metrics["validity_score"] = validity_score
        metrics["decay_score"] = decay_score
        metrics["freshness_score"] = freshness_score
        metrics["lifecycle_status"] = "retired" if retired else "active"
        metrics["retired"] = retired
        metrics["retirement_reason"] = retirement_reason
        metrics["last_decay_at"] = now.isoformat()
        return {
            "id": row.get("id"),
            "rule": row.get("rule"),
            "version": int(row.get("version") or 1) + 1,
            "metrics": metrics,
        }

    def _merge_memory_entry_with_existing(
        self,
        entry: Dict[str, Any],
        existing: Dict[str, Any],
        now: datetime,
    ) -> Dict[str, Any]:
        existing_metrics = dict(existing.get("metrics") or {})
        merged = self._merge_memory_entry_pair(existing, entry)
        metrics = merged.get("metrics") or {}
        revived = bool(existing_metrics.get("retired"))
        prior_confidence = float(existing_metrics.get("confidence_score") or 0.0)
        new_confidence = float(metrics.get("confidence_score") or 0.0)
        prior_validity = float(existing_metrics.get("validity_score") or prior_confidence or 0.0)
        merged_validity = round(min(0.99, (prior_validity * 0.4) + (new_confidence * 0.6) + (0.05 if revived else 0.0)), 4)
        metrics["confidence_score"] = round(max(prior_confidence, new_confidence), 4)
        metrics["validity_score"] = merged_validity
        metrics["decay_score"] = 1.0
        metrics["freshness_score"] = 1.0
        metrics["lifecycle_status"] = "active"
        metrics["retired"] = False
        metrics["retirement_reason"] = None
        metrics["revive_count"] = int(existing_metrics.get("revive_count") or 0) + (1 if revived else 0)
        metrics["last_revalidated_at"] = now.isoformat()
        metrics["last_validated_at"] = now.isoformat()
        metrics["merged_memory_ids"] = self._merge_unique_list(
            existing_metrics.get("merged_memory_ids") or [existing.get("id")],
            metrics.get("merged_memory_ids") or [],
            limit=10,
        )
        merged["id"] = existing.get("id")
        merged["version"] = max(int(existing.get("version") or 1) + 1, int(entry.get("version") or 1))
        merged["metrics"] = metrics
        return merged

    def _reconcile_long_term_memory_entries(
        self,
        entries: List[Dict[str, Any]],
        existing_memories: List[Dict[str, Any]],
        now: datetime,
    ) -> List[Dict[str, Any]]:
        consolidated = self._merge_similar_memory_entries(entries)
        existing_rows = [row for row in (existing_memories or []) if row.get("rule")]
        matched_existing_ids: set[int] = set()
        reconciled: List[Dict[str, Any]] = []
        for entry in consolidated:
            metrics = entry.get("metrics") or {}
            candidate_key = str(metrics.get("canonical_key") or "")
            candidate_category = str(metrics.get("category") or "lesson")
            candidate_regime = str(metrics.get("regime") or "general")
            match = None
            for row in existing_rows:
                if row.get("id") in matched_existing_ids:
                    continue
                row_metrics = row.get("metrics") or {}
                if candidate_key and candidate_key == str(row_metrics.get("canonical_key") or ""):
                    match = row
                    break
                if candidate_category != str(row_metrics.get("category") or "lesson"):
                    continue
                if candidate_regime != str(row_metrics.get("regime") or "general"):
                    continue
                if self._memory_rule_similarity(entry.get("rule") or "", row.get("rule") or "") >= 0.65:
                    match = row
                    break
            if match is None:
                reconciled.append(entry)
            else:
                matched_existing_ids.add(match.get("id"))
                reconciled.append(self._merge_memory_entry_with_existing(entry, match, now))
        for row in existing_rows:
            row_id = row.get("id")
            if row_id in matched_existing_ids:
                continue
            reconciled.append(self._build_decayed_memory_entry(row, now))
        return reconciled

    def _prepare_memory_entries(
        self,
        stats_package: Dict[str, Any],
        lessons: List[MemoryLesson],
        existing_memories: List[Dict[str, Any]] | None = None,
    ) -> List[Dict[str, Any]]:
        entries = []
        ledger_stats = stats_package.get("ledger_stats", {})
        now = datetime.now(UTC)
        for lesson in lessons:
            support_count = min(
                lesson.support_count,
                max(1, len(lesson.evidence_journal_ids) or lesson.support_count),
            )
            status = "hypothesis" if lesson.hypothesis or support_count < 3 else "rule"
            confidence_score = self._compute_memory_confidence_score(
                support_count=support_count,
                evidence_count=len(lesson.evidence_journal_ids or []),
                status=status,
                category=lesson.category,
                stats_package=stats_package,
                hypothesis=bool(lesson.hypothesis or support_count < 3),
            )
            metrics = {
                "support": support_count,
                "window_days": round(stats_package["window_hours"] / 24, 2),
                "category": lesson.category,
                "regime": lesson.regime,
                "status": status,
                "apply_when": lesson.apply_when.model_dump(),
                "ignore_when": lesson.ignore_when,
                "trigger_context": lesson.trigger_context,
                "execution_context": lesson.execution_context,
                "trend_context": lesson.trend_context,
                "hold_quality": lesson.hold_quality,
                "win_rate": stats_package["win_rate"],
                "avg_estimated_pnl_pct": stats_package["avg_estimated_pnl_pct"],
                "ledger_net_realized_pnl": ledger_stats.get("net_realized_pnl", 0.0),
                "evidence_journal_ids": lesson.evidence_journal_ids,
                "source": "daily_batch",
                "candidate_lessons": stats_package.get("candidate_lessons", [])[:5],
                "journal_count": stats_package["journal_count"],
                "trade_case_count": stats_package.get("trade_case_count", 0),
                "retrieval_evaluation_count": stats_package.get("retrieval_evaluation_count", 0),
                "replay_run_count": stats_package.get("replay_run_count", 0),
                "execution_policy_evaluation_count": stats_package.get("execution_policy_evaluation_count", 0),
                "closed_trade_case_count": stats_package.get("closed_trade_case_count", 0),
                "evaluated_trade_count": stats_package["evaluated_trade_count"],
                "trade_case_patterns": stats_package.get("trade_case_patterns", {}),
                "trade_case_clusters": stats_package.get("trade_case_clusters", [])[:6],
                "trade_case_cluster_evaluation": stats_package.get("trade_case_cluster_evaluation", {}),
                "retrieval_evaluation_summary": stats_package.get("retrieval_evaluation_summary", {}),
                "retrieval_counterfactual_summary": stats_package.get("retrieval_counterfactual_summary", {}),
                "hold_opportunity_summary": stats_package.get("hold_opportunity_summary", {}),
                "trading_balance_summary": stats_package.get("trading_balance_summary", {}),
                "confidence_calibration_summary": stats_package.get("confidence_calibration_summary", {}),
                "execution_policy_summary": stats_package.get("execution_policy_summary", {}),
                "confidence_score": confidence_score,
                "validity_score": confidence_score,
                "decay_score": 1.0,
                "freshness_score": 1.0,
                "lifecycle_status": "active",
                "retired": False,
                "retirement_reason": None,
                "revive_count": 0,
                "merge_count": 1,
                "last_revalidated_at": now.isoformat(),
                "last_validated_at": now.isoformat(),
            }
            metrics["canonical_key"] = self._build_memory_canonical_key(lesson.rule, metrics)
            metrics["merged_rules"] = [lesson.rule]
            entries.append(
                {
                    "rule": lesson.rule,
                    "version": 1,
                    "metrics": metrics,
                }
            )
        return self._reconcile_long_term_memory_entries(entries, existing_memories or [], now)

    def _temporal_sort_key(self, row: Dict[str, Any]) -> tuple[Any, int]:
        timestamp = row.get("timestamp") or row.get("created_at") or row.get("closed_at") or row.get("opened_at") or ""
        return (str(timestamp), int(row.get("audit_log_id") or row.get("id") or 0))

    def _build_temporal_holdout_summary(self, values: List[float]) -> Dict[str, Any]:
        ordered = [float(value) for value in values]
        count = len(ordered)
        if count < 3:
            avg_score = round(sum(ordered) / max(1, count), 4) if ordered else 0.0
            return {
                "train_count": count,
                "holdout_count": 0,
                "train_score": avg_score,
                "holdout_score": None,
                "score_gap": None,
                "stability_score": 0.55 if count >= 2 else 0.4,
            }
        split_index = max(1, int(math.ceil(count * 0.7)))
        if split_index >= count:
            split_index = count - 1
        train_values = ordered[:split_index]
        holdout_values = ordered[split_index:]
        train_score = sum(train_values) / max(1, len(train_values))
        holdout_score = sum(holdout_values) / max(1, len(holdout_values))
        score_gap = abs(holdout_score - train_score)
        stability_score = max(
            0.0,
            1.0
            - min(0.6, score_gap * 0.8)
            - min(0.25, statistics.pstdev(ordered) if len(ordered) > 1 else 0.0),
        )
        return {
            "train_count": len(train_values),
            "holdout_count": len(holdout_values),
            "train_score": round(train_score, 4),
            "holdout_score": round(holdout_score, 4),
            "score_gap": round(score_gap, 4),
            "stability_score": round(stability_score, 4),
        }

    def _build_shadow_candidate_holdout_summary(
        self,
        rows: List[Dict[str, Any]],
        *,
        domain: str,
    ) -> Dict[str, Any]:
        ordered_rows = sorted(rows or [], key=self._temporal_sort_key)
        adjusted_values: List[float] = []
        for row in ordered_rows:
            outcome = self._coerce_float(row.get("outcome_estimated_pnl_pct"))
            if outcome is None:
                continue
            adjusted = outcome
            if domain == "alpha_1h" and str(row.get("divergence_type") or "") == "entry_block":
                adjusted = -outcome
            elif domain == "safety_5m":
                divergence_type = str(row.get("divergence_type") or "")
                if divergence_type == "risk_exit":
                    adjusted = -outcome
                elif divergence_type == "risk_reduce":
                    adjusted = -(outcome * 0.5)
            adjusted_values.append(adjusted)
        return self._build_temporal_holdout_summary(adjusted_values)

    def _passes_temporal_holdout_gate(self, summary: Dict[str, Any] | None) -> bool:
        payload = summary or {}
        holdout_count = int(payload.get("holdout_count") or 0)
        holdout_score = self._coerce_float(payload.get("holdout_score"))
        stability_score = self._coerce_float(payload.get("stability_score"))
        score_gap = self._coerce_float(payload.get("score_gap"))
        if holdout_count <= 0:
            return True
        if holdout_score is not None and holdout_score < -0.05:
            return False
        if stability_score is not None and stability_score < 0.2:
            return False
        if score_gap is not None and score_gap > 0.7:
            return False
        return True

    def _sync_candidate_policies(self, candidate_policies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        saved = []
        for candidate in candidate_policies or []:
            candidate_id = None
            if hasattr(self.state_store, "save_candidate_policy_version"):
                candidate_id = self.state_store.save_candidate_policy_version(candidate)
            saved.append({**candidate, "id": candidate_id})
        return saved

    def _evaluate_candidate_policies(
        self,
        candidates: List[Dict[str, Any]],
        shadow_decisions: List[Dict[str, Any]],
        replay_runs: List[Dict[str, Any]] | None = None,
        trading_balance_summary: Dict[str, Any] | None = None,
        confidence_calibration_summary: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        shadow_grouped: Dict[int, List[Dict[str, Any]]] = {}
        for row in shadow_decisions or []:
            try:
                candidate_id = int(row.get("candidate_policy_id"))
            except (TypeError, ValueError):
                continue
            shadow_grouped.setdefault(candidate_id, []).append(row)
        evaluations = []
        replay_summary = self._build_replay_execution_summary(replay_runs or [])
        trading_balance_summary = trading_balance_summary or {}
        confidence_calibration_summary = confidence_calibration_summary or {}
        raw_confidence_alignment = confidence_calibration_summary.get("alignment_score")
        confidence_alignment_score = (
            float(raw_confidence_alignment)
            if raw_confidence_alignment is not None
            else None
        )
        for candidate in candidates or []:
            candidate_id = candidate.get("id")
            domain = str(candidate.get("domain") or "").strip()
            metrics = dict(candidate.get("metrics") or {})
            support_count = int(metrics.get("sample_count") or metrics.get("support_count") or 0)
            score = float(metrics.get("utility_score") or 0.0)
            sample_count = support_count
            scope = "seed"
            temporal_holdout_summary = self._build_temporal_holdout_summary([])
            if candidate_id and candidate_id in shadow_grouped:
                rows = shadow_grouped[candidate_id]
                realized = []
                for row in rows:
                    outcome = self._coerce_float(row.get("outcome_estimated_pnl_pct"))
                    if outcome is None:
                        continue
                    adjusted = outcome
                    if domain == "alpha_1h" and str(row.get("divergence_type") or "") == "entry_block":
                        adjusted = -outcome
                    elif domain == "safety_5m":
                        divergence_type = str(row.get("divergence_type") or "")
                        if divergence_type == "risk_exit":
                            adjusted = -outcome
                        elif divergence_type == "risk_reduce":
                            adjusted = -(outcome * 0.5)
                    realized.append(adjusted)
                sample_count = len(rows)
                score = round(sum(realized) / max(1, len(realized)), 4) if realized else 0.0
                scope = "shadow"
                temporal_holdout_summary = self._build_shadow_candidate_holdout_summary(rows, domain=domain)
            if domain == "alpha_1h":
                score += min(
                    0.25,
                    (float(replay_summary.get("hold_opportunity_cost") or 0.0) * 0.05)
                    + (int(replay_summary.get("missed_breakout_count") or 0) * 0.02),
                )
                score -= min(
                    0.25,
                    float(trading_balance_summary.get("false_entry_cost") or 0.0) * 0.05,
                )
            score += min(0.12, float(trading_balance_summary.get("portfolio_utility_score") or 0.0) * 0.12)
            if confidence_alignment_score is not None:
                score += min(0.08, confidence_alignment_score * 0.08)
            holdout_score = self._coerce_float(temporal_holdout_summary.get("holdout_score"))
            if holdout_score is not None:
                score += max(-0.12, min(0.12, holdout_score * 0.12))
            evaluation = {
                "candidate_policy_id": candidate_id,
                "evaluation_scope": scope,
                "sample_count": sample_count,
                "score": round(score, 4),
                "metrics": {
                    **metrics,
                    "domain": candidate.get("domain"),
                    "policy_type": candidate.get("policy_type"),
                    "replay_execution_summary": replay_summary,
                    "trading_balance_summary": trading_balance_summary,
                    "confidence_calibration_summary": confidence_calibration_summary,
                    "confidence_alignment_score": round(float(confidence_alignment_score or 0.0), 4),
                    "temporal_holdout_summary": temporal_holdout_summary,
                },
            }
            evaluations.append(evaluation)
            if candidate_id and hasattr(self.state_store, "save_policy_evaluation"):
                self.state_store.save_policy_evaluation(evaluation)
        return evaluations

    def _promote_candidate_policies(self, candidates: List[Dict[str, Any]], evaluations: List[Dict[str, Any]]):
        if not self._learning_promotions_allowed():
            logger.warning("Skipping candidate policy promotion because learning liveness is stale.")
            return
        best_by_domain: Dict[tuple[str, str], Dict[str, Any]] = {}
        for candidate in candidates or []:
            candidate_id = candidate.get("id")
            if not candidate_id:
                continue
            evaluation = next((row for row in evaluations if row.get("candidate_policy_id") == candidate_id), None)
            if not evaluation:
                continue
            domain = str(candidate.get("domain") or "").strip()
            regime_key = str(candidate.get("regime_key") or ((evaluation.get("metrics") or {}).get("regime_key")) or "").strip()
            key = (domain, regime_key)
            current = best_by_domain.get(key)
            if current is None or float(evaluation.get("score") or 0.0) > float(current["evaluation"].get("score") or 0.0):
                best_by_domain[key] = {"candidate": candidate, "evaluation": evaluation}

        for (domain, _), bundle in best_by_domain.items():
            candidate = bundle["candidate"]
            evaluation = bundle["evaluation"]
            sample_count = int(evaluation.get("sample_count") or 0)
            score = float(evaluation.get("score") or 0.0)
            evaluation_scope = str(evaluation.get("evaluation_scope") or "seed")
            if domain == "alpha_1h":
                trading_balance_summary = (evaluation.get("metrics") or {}).get("trading_balance_summary") or {}
                hold_cost = float(trading_balance_summary.get("hold_opportunity_cost") or 0.0)
                false_entry_cost = float(trading_balance_summary.get("false_entry_cost") or 0.0)
                balance_score = float(trading_balance_summary.get("balance_score") or 0.0)
                portfolio_utility_score = float(trading_balance_summary.get("portfolio_utility_score") or 0.0)
                pressure_ratio = self._coerce_float(trading_balance_summary.get("pressure_ratio"))
                confidence_alignment_score = float((evaluation.get("metrics") or {}).get("confidence_alignment_score") or 0.5)
                temporal_holdout_summary = (evaluation.get("metrics") or {}).get("temporal_holdout_summary") or {}
                if (
                    hold_cost > 3.5
                    or false_entry_cost > 3.5
                    or balance_score < 0.3
                    or portfolio_utility_score < 0.25
                    or confidence_alignment_score < 0.3
                    or (pressure_ratio is not None and (pressure_ratio < 0.55 or pressure_ratio > 1.85))
                    or not self._passes_temporal_holdout_gate(temporal_holdout_summary)
                ):
                    continue
                if evaluation_scope == "shadow":
                    if sample_count < 2 or score <= 0:
                        continue
                else:
                    if sample_count < 4 or score < 0.15:
                        continue
            elif sample_count < 3 or score <= 0:
                continue
            regime_key = str(candidate.get("regime_key") or ((evaluation.get("metrics") or {}).get("regime_key")) or "").strip()
            policy_key = (
                self.state_store.build_policy_key(domain, regime_key)
                if hasattr(self.state_store, "build_policy_key")
                else (f"champion:{domain}:{regime_key}" if regime_key else f"champion:{domain}")
            )
            existing = self.state_store.get_policy_registry_entry(policy_key) if hasattr(self.state_store, "get_policy_registry_entry") else {}
            existing_score = float(((existing.get("metrics") or {}).get("score")) or 0.0) if existing else 0.0
            if existing and score <= existing_score:
                continue
            version = int(existing.get("version") or 0) + 1 if existing else 1
            registry_entry = {
                "policy_key": policy_key,
                "domain": domain,
                "policy_type": candidate.get("policy_type"),
                "regime_key": regime_key or None,
                "active_candidate_policy_id": candidate.get("id"),
                "version": version,
                "config": candidate.get("config") or {},
                "metrics": {
                    "score": score,
                    "sample_count": sample_count,
                    "candidate_policy_id": candidate.get("id"),
                    "rule_text": candidate.get("rule_text"),
                    "regime_key": regime_key or "",
                    "temporal_holdout_summary": (evaluation.get("metrics") or {}).get("temporal_holdout_summary") or {},
                    "confidence_alignment_score": (evaluation.get("metrics") or {}).get("confidence_alignment_score") or 0.5,
                    "portfolio_utility_score": (
                        ((evaluation.get("metrics") or {}).get("trading_balance_summary") or {}).get("portfolio_utility_score")
                        or 0.0
                    ),
                },
            }
            if hasattr(self.state_store, "upsert_policy_registry"):
                self.state_store.upsert_policy_registry(registry_entry)
            if hasattr(self.state_store, "save_policy_promotion"):
                self.state_store.save_policy_promotion(
                    {
                        "domain": domain,
                        "regime_key": regime_key or None,
                        "previous_candidate_policy_id": existing.get("active_candidate_policy_id") if existing else None,
                        "new_candidate_policy_id": candidate.get("id"),
                        "reason": f"Auto-promoted from {evaluation.get('evaluation_scope')} evaluation.",
                        "metrics": registry_entry["metrics"],
                    }
                )

    def _build_replay_execution_summary(self, replay_runs: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not replay_runs:
            return {
                "count": 0,
                "avg_fill_ratio": 0.0,
                "avg_slippage_bps": 0.0,
                "avg_latency_seconds": 0.0,
                "hold_opportunity_evaluation_count": 0,
                "hold_baseline_beats_hold_count": 0,
                "hold_simple_rule_beats_hold_count": 0,
                "missed_breakout_count": 0,
                "missed_move_median_pct": 0.0,
                "hold_opportunity_cost": 0.0,
            }
        fill_ratios = []
        slippages = []
        latencies = []
        hold_opportunity_evaluation_count = 0
        hold_baseline_beats_hold_count = 0
        hold_simple_rule_beats_hold_count = 0
        missed_breakout_count = 0
        missed_moves: List[float] = []
        hold_opportunity_cost = 0.0
        for row in replay_runs:
            payload = row.get("payload") or {}
            fill_ratio = self._coerce_float(payload.get("simulated_fill_ratio"))
            slippage = self._coerce_float(payload.get("simulated_slippage_bps"))
            latency = self._coerce_float(payload.get("simulated_latency_seconds"))
            if fill_ratio is not None:
                fill_ratios.append(fill_ratio)
            if slippage is not None:
                slippages.append(slippage)
            if latency is not None:
                latencies.append(latency)
            hold_counterfactual = payload.get("hold_opportunity_counterfactual") or {}
            if hold_counterfactual:
                hold_opportunity_evaluation_count += 1
                if bool(hold_counterfactual.get("baseline_beats_hold")):
                    hold_baseline_beats_hold_count += 1
                if bool(hold_counterfactual.get("simple_rule_baseline_beats_hold")):
                    hold_simple_rule_beats_hold_count += 1
                missed_breakout_count += int(hold_counterfactual.get("missed_breakout_count") or 0)
                move_value = self._coerce_float(hold_counterfactual.get("missed_move_median_pct"))
                if move_value is not None and move_value > 0:
                    missed_moves.append(move_value)
                cost_value = self._coerce_float(hold_counterfactual.get("hold_opportunity_cost"))
                if cost_value is not None:
                    hold_opportunity_cost += cost_value
        missed_moves_sorted = sorted(missed_moves)
        return {
            "count": len(replay_runs),
            "avg_fill_ratio": round(sum(fill_ratios) / max(1, len(fill_ratios)), 4) if fill_ratios else 0.0,
            "avg_slippage_bps": round(sum(slippages) / max(1, len(slippages)), 4) if slippages else 0.0,
            "avg_latency_seconds": round(sum(latencies) / max(1, len(latencies)), 4) if latencies else 0.0,
            "hold_opportunity_evaluation_count": hold_opportunity_evaluation_count,
            "hold_baseline_beats_hold_count": hold_baseline_beats_hold_count,
            "hold_simple_rule_beats_hold_count": hold_simple_rule_beats_hold_count,
            "missed_breakout_count": missed_breakout_count,
            "missed_move_median_pct": (
                round(missed_moves_sorted[len(missed_moves_sorted) // 2], 4)
                if missed_moves_sorted
                else 0.0
            ),
            "hold_opportunity_cost": round(hold_opportunity_cost, 4),
        }

    def _build_policy_bundles(self) -> List[Dict[str, Any]]:
        if not hasattr(self.state_store, "list_policy_registry"):
            return []
        registry_entries = self.state_store.list_policy_registry()
        grouped: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for row in registry_entries:
            regime_key = str(row.get("regime_key") or "").strip()
            grouped.setdefault(regime_key, {})[str(row.get("domain") or "")] = row
        bundles = []
        for regime_key, domains in grouped.items():
            bundle_key = f"bundle:{regime_key or 'global'}"
            bundles.append(
                {
                    "bundle_key": bundle_key,
                    "regime_key": regime_key or None,
                    "status": "candidate",
                    "alpha_policy_key": (domains.get("alpha_1h") or {}).get("policy_key"),
                    "position_policy_key": (domains.get("position_15m") or {}).get("policy_key"),
                    "safety_policy_key": (domains.get("safety_5m") or {}).get("policy_key"),
                    "execution_policy_key": (domains.get("execution_5m") or {}).get("policy_key"),
                    "metrics": {
                        "component_scores": {
                            domain: float(((row.get("metrics") or {}).get("score")) or 0.0)
                            for domain, row in domains.items()
                        },
                        "component_holdout_summaries": {
                            domain: ((row.get("metrics") or {}).get("temporal_holdout_summary") or {})
                            for domain, row in domains.items()
                        },
                    },
                }
            )
        return bundles

    def _evaluate_policy_bundles(
        self,
        bundles: List[Dict[str, Any]],
        replay_runs: List[Dict[str, Any]],
        trading_balance_summary: Dict[str, Any] | None = None,
        confidence_calibration_summary: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        replay_summary = self._build_replay_execution_summary(replay_runs)
        trading_balance_summary = trading_balance_summary or {}
        confidence_calibration_summary = confidence_calibration_summary or {}
        confidence_alignment_score = float(confidence_calibration_summary.get("alignment_score") or 0.5)
        evaluations = []
        for bundle in bundles or []:
            component_scores = (bundle.get("metrics") or {}).get("component_scores") or {}
            component_holdouts = (bundle.get("metrics") or {}).get("component_holdout_summaries") or {}
            holdout_scores = [
                float(summary.get("holdout_score"))
                for summary in component_holdouts.values()
                if self._coerce_float((summary or {}).get("holdout_score")) is not None
            ]
            stability_scores = [
                float(summary.get("stability_score"))
                for summary in component_holdouts.values()
                if self._coerce_float((summary or {}).get("stability_score")) is not None
            ]
            score = 0.0
            if component_scores:
                score = sum(float(value or 0.0) for value in component_scores.values()) / max(1, len(component_scores))
            score += max(0.0, (1.0 - float(replay_summary.get("avg_slippage_bps") or 0.0) / 25.0) * 0.1)
            score += min(0.1, float(replay_summary.get("avg_fill_ratio") or 0.0) * 0.1)
            score -= min(
                0.2,
                (float(replay_summary.get("hold_opportunity_cost") or 0.0) * 0.05)
                + (int(replay_summary.get("missed_breakout_count") or 0) * 0.02),
            )
            score -= min(0.2, float(trading_balance_summary.get("false_entry_cost") or 0.0) * 0.04)
            score += min(0.1, float(trading_balance_summary.get("balance_score") or 0.0) * 0.1)
            score += min(0.12, float(trading_balance_summary.get("portfolio_utility_score") or 0.0) * 0.12)
            score += min(0.08, confidence_alignment_score * 0.08)
            if holdout_scores:
                score += max(-0.12, min(0.12, (sum(holdout_scores) / len(holdout_scores)) * 0.12))
            evaluation = {
                "bundle_key": bundle.get("bundle_key"),
                "evaluation_scope": "bundle_batch",
                "sample_count": len(component_scores),
                "score": round(score, 4),
                "metrics": {
                    "regime_key": bundle.get("regime_key"),
                    "component_scores": component_scores,
                    "component_holdout_summaries": component_holdouts,
                    "replay_execution_summary": replay_summary,
                    "trading_balance_summary": trading_balance_summary,
                    "confidence_calibration_summary": confidence_calibration_summary,
                    "confidence_alignment_score": round(confidence_alignment_score, 4),
                    "avg_component_holdout_score": round(sum(holdout_scores) / len(holdout_scores), 4) if holdout_scores else None,
                    "avg_component_holdout_stability": round(sum(stability_scores) / len(stability_scores), 4) if stability_scores else None,
                },
            }
            evaluations.append(evaluation)
            if hasattr(self.state_store, "save_bundle_evaluation"):
                self.state_store.save_bundle_evaluation(evaluation)
        return evaluations

    def _promote_policy_bundles(self, bundles: List[Dict[str, Any]], evaluations: List[Dict[str, Any]]):
        if not self._learning_promotions_allowed():
            logger.warning("Skipping bundle promotion because learning liveness is stale.")
            return
        best_by_regime: Dict[str, Dict[str, Any]] = {}
        for bundle in bundles or []:
            evaluation = next((row for row in evaluations if row.get("bundle_key") == bundle.get("bundle_key")), None)
            if not evaluation:
                continue
            regime_key = str(bundle.get("regime_key") or "").strip()
            current = best_by_regime.get(regime_key)
            if current is None or float(evaluation.get("score") or 0.0) > float(current["evaluation"].get("score") or 0.0):
                best_by_regime[regime_key] = {"bundle": bundle, "evaluation": evaluation}
        for regime_key, payload in best_by_regime.items():
            bundle = payload["bundle"]
            evaluation = payload["evaluation"]
            if int(evaluation.get("sample_count") or 0) < 2 or float(evaluation.get("score") or 0.0) <= 0:
                continue
            trading_balance_summary = (evaluation.get("metrics") or {}).get("trading_balance_summary") or {}
            hold_cost = float(trading_balance_summary.get("hold_opportunity_cost") or 0.0)
            false_entry_cost = float(trading_balance_summary.get("false_entry_cost") or 0.0)
            balance_score = float(trading_balance_summary.get("balance_score") or 0.0)
            portfolio_utility_score = float(trading_balance_summary.get("portfolio_utility_score") or 0.0)
            confidence_alignment_score = float((evaluation.get("metrics") or {}).get("confidence_alignment_score") or 0.5)
            pressure_ratio = self._coerce_float(trading_balance_summary.get("pressure_ratio"))
            component_holdout_summaries = (evaluation.get("metrics") or {}).get("component_holdout_summaries") or {}
            if (
                hold_cost > 3.5
                or false_entry_cost > 3.5
                or balance_score < 0.3
                or portfolio_utility_score < 0.25
                or confidence_alignment_score < 0.3
                or (pressure_ratio is not None and (pressure_ratio < 0.55 or pressure_ratio > 1.85))
            ):
                continue
            if any(not self._passes_temporal_holdout_gate(summary) for summary in component_holdout_summaries.values()):
                continue
            existing = self.state_store.get_active_policy_bundle(regime_key=regime_key) if hasattr(self.state_store, "get_active_policy_bundle") else {}
            existing_score = float(((existing.get("metrics") or {}).get("score")) or 0.0) if existing else 0.0
            if existing and float(evaluation.get("score") or 0.0) <= existing_score:
                continue
            upsert_payload = dict(bundle)
            upsert_payload["status"] = "active"
            upsert_payload["metrics"] = {**(bundle.get("metrics") or {}), "score": evaluation.get("score")}
            if hasattr(self.state_store, "upsert_policy_bundle"):
                self.state_store.upsert_policy_bundle(upsert_payload)
            if hasattr(self.state_store, "save_bundle_promotion"):
                self.state_store.save_bundle_promotion(
                    {
                        "regime_key": regime_key or None,
                        "previous_bundle_key": existing.get("bundle_key") if existing else None,
                        "new_bundle_key": bundle.get("bundle_key"),
                        "reason": "Auto-promoted regime bundle from component champion scores.",
                        "metrics": upsert_payload["metrics"],
                    }
                )

    def _rollback_degraded_bundles(self, shadow_decisions: List[Dict[str, Any]]):
        if not hasattr(self.state_store, "get_recent_bundle_promotions") or not hasattr(self.state_store, "upsert_policy_bundle"):
            return
        active_global = self.state_store.get_active_policy_bundle(regime_key="")
        bundle_candidates = [active_global] if active_global else []
        seen_regimes = {str((active_global or {}).get("regime_key") or "")} if active_global else set()
        for bundle in self.state_store.get_policy_bundles(statuses=["active"], limit=20) if hasattr(self.state_store, "get_policy_bundles") else []:
            regime_key = str(bundle.get("regime_key") or "")
            if regime_key in seen_regimes:
                continue
            bundle_candidates.append(bundle)
            seen_regimes.add(regime_key)
        overall_shadow_score = 0.0
        shadow_count = 0
        for row in shadow_decisions or []:
            outcome = self._coerce_float(row.get("outcome_estimated_pnl_pct"))
            if outcome is None:
                continue
            overall_shadow_score += outcome
            shadow_count += 1
        avg_shadow_outcome = overall_shadow_score / max(1, shadow_count) if shadow_count else 0.0
        if avg_shadow_outcome >= -0.5:
            return
        for bundle in bundle_candidates:
            regime_key = str(bundle.get("regime_key") or "")
            promotions = self.state_store.get_recent_bundle_promotions(regime_key=regime_key or None, limit=2)
            if len(promotions) < 1:
                continue
            previous_bundle_key = str((promotions[0].get("previous_bundle_key")) or "").strip()
            if not previous_bundle_key:
                continue
            previous_bundle = self.state_store.get_policy_bundle(previous_bundle_key) if hasattr(self.state_store, "get_policy_bundle") else {}
            if not previous_bundle:
                continue
            self.state_store.upsert_policy_bundle({**bundle, "status": "shadow"})
            self.state_store.upsert_policy_bundle({**previous_bundle, "status": "active"})

    def run(self, window_hours: int = 24):
        logger.info("Starting Daily Memory Update Batch with model: {}", self.model_name)
        journals = self._fetch_window_entries(window_hours=window_hours)
        if not journals:
            logger.info("No journals found in the selected window. Skipping.")
            return
        journals, excluded_journals = self._filter_learning_eligible_journals(journals)
        if excluded_journals:
            logger.info(
                "Excluding {} system-error journal(s) from memory/replay learning inputs.",
                len(excluded_journals),
            )
        if not journals:
            logger.info("No learning-eligible journals found in the selected window. Skipping.")
            return
        eligible_audit_ids = {
            int(row.get("audit_log_id") or 0)
            for row in journals
            if int(row.get("audit_log_id") or 0) > 0
        }

        symbol_journals = self._fetch_symbol_window_entries(window_hours=window_hours)
        trade_cases = self._fetch_trade_case_window_entries(window_hours=window_hours)
        retrieval_evaluations = self._fetch_retrieval_window_entries(window_hours=window_hours)
        replay_runs = self._fetch_replay_window_entries(window_hours=window_hours)
        execution_policy_evaluations = self._fetch_execution_policy_window_entries(window_hours=window_hours)
        shadow_decisions = self._fetch_shadow_decision_window_entries(window_hours=window_hours)
        symbol_journals = self._filter_audit_linked_rows(symbol_journals, audit_ids=eligible_audit_ids)
        trade_cases = self._filter_trade_cases_for_learning(trade_cases, audit_ids=eligible_audit_ids)
        retrieval_evaluations = self._filter_audit_linked_rows(
            retrieval_evaluations,
            audit_ids=eligible_audit_ids,
        )
        replay_runs = self._filter_audit_linked_rows(replay_runs, audit_ids=eligible_audit_ids)
        execution_policy_evaluations = self._filter_audit_linked_rows(
            execution_policy_evaluations,
            audit_ids=eligible_audit_ids,
        )
        shadow_decisions = self._filter_audit_linked_rows(shadow_decisions, audit_ids=eligible_audit_ids)
        stats_package = self._build_stats_package(
            journals,
            window_hours=window_hours,
            symbol_journals=symbol_journals,
            trade_cases=trade_cases,
            retrieval_evaluations=retrieval_evaluations,
            replay_runs=replay_runs,
            execution_policy_evaluations=execution_policy_evaluations,
            shadow_decisions=shadow_decisions,
        )
        stats_package["excluded_system_error_journal_count"] = len(excluded_journals)
        logger.info(
            "Prepared stats for memory update: journals={}, symbol_rows={}, trade_cases={}, evaluated={}, win_rate={:.2f}%",
            stats_package["journal_count"],
            stats_package["symbol_journal_count"],
            stats_package["trade_case_count"],
            stats_package["evaluated_trade_count"],
            stats_package["win_rate"],
        )
        journal_reranker_profile = self._build_recent_journal_reranker_profile(journals)
        similar_case_reranker_profile = self._build_similar_case_reranker_profile(
            retrieval_evaluations,
            replay_runs,
        )
        shadow_dataset_rows = self._build_shadow_model_dataset(journals, replay_runs, shadow_decisions)
        shadow_dataset_summary = self._summarize_shadow_model_dataset(shadow_dataset_rows)
        shadow_dataset_path = self._export_shadow_model_dataset(shadow_dataset_rows)
        if hasattr(self.state_store, "upsert_learning_artifact"):
            self.state_store.upsert_learning_artifact(
                "journal_reranker_profile",
                journal_reranker_profile,
                artifact_type="retrieval_model",
            )
            self.state_store.upsert_learning_artifact(
                "similar_case_reranker_profile",
                similar_case_reranker_profile,
                artifact_type="retrieval_model",
            )
            self.state_store.upsert_learning_artifact(
                "shadow_model_dataset",
                {
                    **shadow_dataset_summary,
                    "dataset_path": shadow_dataset_path,
                },
                artifact_type="shadow_dataset",
            )
            liveness_payload = self._build_learning_liveness_payload(
                window_hours=window_hours,
                replay_runs=replay_runs,
                shadow_dataset_summary=shadow_dataset_summary,
            )
            self.state_store.upsert_learning_artifact(
                "learning_liveness",
                {
                    **liveness_payload,
                    "reranker_ready": {
                        "recent_journal": int(journal_reranker_profile.get("sample_count") or 0) > 0,
                        "similar_cases": int(similar_case_reranker_profile.get("sample_count") or 0) > 0,
                    },
                },
                artifact_type="learning_liveness",
            )
            self.state_store.upsert_learning_artifact(
                "memory_updater_heartbeat",
                {
                    "window_hours": int(window_hours or 24),
                    "journal_count": stats_package["journal_count"],
                    "trade_case_count": stats_package["trade_case_count"],
                    "replay_run_count": len(replay_runs),
                    "shadow_dataset_path": shadow_dataset_path,
                },
                artifact_type="heartbeat",
            )
        lessons = self._generate_lessons(stats_package)
        existing_memories = self.state_store.get_long_term_memory_pool()
        entries = self._prepare_memory_entries(stats_package, lessons, existing_memories=existing_memories)

        if entries:
            self.state_store.save_long_term_memory_entries(entries)
            logger.info(f"Saved {len(entries)} grounded long-term memory entries.")
        else:
            logger.info("No actionable lessons extracted.")
        execution_profiles = stats_package.get("execution_policy_recommendations") or []
        if execution_profiles and hasattr(self.state_store, "save_execution_policy_profiles"):
            self.state_store.save_execution_policy_profiles(execution_profiles)
            logger.info("Saved {} execution policy profile recommendations.", len(execution_profiles))
        saved_candidates = self._sync_candidate_policies(stats_package.get("candidate_policies") or [])
        candidate_evaluations = self._evaluate_candidate_policies(
            saved_candidates,
            shadow_decisions,
            replay_runs=replay_runs,
            trading_balance_summary=stats_package.get("trading_balance_summary") or {},
            confidence_calibration_summary=stats_package.get("confidence_calibration_summary") or {},
        )
        self._promote_candidate_policies(saved_candidates, candidate_evaluations)
        bundles = self._build_policy_bundles()
        if bundles and hasattr(self.state_store, "upsert_policy_bundle"):
            for bundle in bundles:
                self.state_store.upsert_policy_bundle(bundle)
        bundle_evaluations = self._evaluate_policy_bundles(
            bundles,
            replay_runs,
            trading_balance_summary=stats_package.get("trading_balance_summary") or {},
            confidence_calibration_summary=stats_package.get("confidence_calibration_summary") or {},
        )
        self._promote_policy_bundles(bundles, bundle_evaluations)
        self._rollback_degraded_bundles(shadow_decisions)
        logger.info("Daily Memory Update Completed.")


if __name__ == "__main__":
    updater = MemoryUpdater()
    updater.run()
