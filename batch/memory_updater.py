import json
import math
import os
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Literal

from dotenv import load_dotenv
from google import genai
from google.genai import types
from loguru import logger
from pydantic import BaseModel, Field

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


class MemoryUpdater:
    def __init__(self, db_path: str = "data/gpt_trader.db"):
        self.db_path = db_path
        self.state_store = StateStore(db_path)
        self.api_key = os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            logger.error("Missing GEMINI_API_KEY")
            raise SystemExit(1)
        self.client = genai.Client(api_key=self.api_key)
        self.model_name = self._resolve_model_name()

    def _resolve_model_name(self) -> str:
        legacy_model = os.getenv("GEMINI_MODEL", "").strip()
        return (
            os.getenv("GEMINI_MEMORY_MODEL", "").strip()
            or os.getenv("GEMINI_TRIGGER_MODEL", "").strip()
            or os.getenv("GEMINI_DEFAULT_MODEL", "").strip()
            or legacy_model
            or "gemini-3.0-pro"
        )

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

    def _build_stats_package(
        self,
        journals: List[Dict[str, Any]],
        window_hours: int,
        symbol_journals: List[Dict[str, Any]] | None = None,
        trade_cases: List[Dict[str, Any]] | None = None,
        retrieval_evaluations: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        symbol_journals = symbol_journals or []
        trade_cases = trade_cases or []
        retrieval_evaluations = retrieval_evaluations or []
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
        trade_case_candidate_lessons = self._build_trade_case_candidate_lessons(trade_case_patterns)
        trade_case_cluster_candidate_lessons = self._build_trade_case_cluster_candidate_lessons(trade_case_clusters)
        merged_candidate_lessons = self._merge_candidate_lesson_dicts(
            self._build_candidate_lessons(
                journals=journals,
                winning_evidence_signals=winning_evidence_signals,
                losing_evidence_signals=losing_evidence_signals,
                evidence_quality_patterns=evidence_quality_patterns,
            ),
            symbol_candidate_lessons,
            trade_case_candidate_lessons,
            trade_case_cluster_candidate_lessons,
        )

        return {
            "window_hours": window_hours,
            "journal_count": len(journals),
            "symbol_journal_count": len(symbol_journals),
            "trade_case_count": len(trade_cases),
            "retrieval_evaluation_count": len(retrieval_evaluations),
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
        }

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
                pnl_value = float(pnl) if pnl is not None else None
            except (TypeError, ValueError):
                pnl_value = None
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
            "avg_similarity": round(similarity_sum / similarity_count, 4) if similarity_count else 0.0,
            "avg_final_score": round(final_score_sum / final_score_count, 4) if final_score_count else 0.0,
            "avg_matched_band_count": round(matched_band_sum / total, 4) if total else 0.0,
            "win_rate_pct": round((win_count / total) * 100, 2) if total else 0.0,
            "aligned_win_rate_pct": round((aligned_win_count / aligned_total) * 100, 2) if aligned_total else 0.0,
            "unaligned_win_rate_pct": round((unaligned_win_count / unaligned_total) * 100, 2) if unaligned_total else 0.0,
            "source_counts": source_counts,
        }

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

Computed statistics:
{stats_json}
"""

    def _generate_lessons(self, stats_package: Dict[str, Any]) -> List[MemoryLesson]:
        if stats_package["journal_count"] == 0:
            return []

        candidate_lessons = [
            MemoryLesson.model_validate(item)
            for item in stats_package.get("candidate_lessons") or []
        ]
        try:
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=self._create_prompt(stats_package),
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=list[MemoryLesson],
                    temperature=0.2,
                ),
            )
            raw_text = response.text
            lessons = json.loads(raw_text)
            if not isinstance(lessons, list):
                return candidate_lessons
            generated_lessons = [MemoryLesson.model_validate(item) for item in lessons]
            return self._merge_candidate_lessons(candidate_lessons, generated_lessons)
        except Exception as e:
            logger.error(f"Gemini failed to extract lessons: {e}")
            return candidate_lessons

    def _merge_candidate_lessons(
        self,
        candidate_lessons: List[MemoryLesson],
        generated_lessons: List[MemoryLesson],
    ) -> List[MemoryLesson]:
        merged: List[MemoryLesson] = []
        seen_rules = set()
        for lesson in generated_lessons + candidate_lessons:
            key = lesson.rule.strip().lower()
            if key in seen_rules:
                continue
            seen_rules.add(key)
            merged.append(lesson)
        return merged

    def _prepare_memory_entries(
        self,
        stats_package: Dict[str, Any],
        lessons: List[MemoryLesson],
    ) -> List[Dict[str, Any]]:
        entries = []
        ledger_stats = stats_package.get("ledger_stats", {})
        for lesson in lessons:
            support_count = min(
                lesson.support_count,
                max(1, len(lesson.evidence_journal_ids) or lesson.support_count),
            )
            status = "hypothesis" if lesson.hypothesis or support_count < 3 else "rule"
            metrics = {
                "support": support_count,
                "window_days": round(stats_package["window_hours"] / 24, 2),
                "category": lesson.category,
                "regime": lesson.regime,
                "status": status,
                "apply_when": lesson.apply_when.model_dump(),
                "ignore_when": lesson.ignore_when,
                "win_rate": stats_package["win_rate"],
                "avg_estimated_pnl_pct": stats_package["avg_estimated_pnl_pct"],
                "ledger_net_realized_pnl": ledger_stats.get("net_realized_pnl", 0.0),
                "evidence_journal_ids": lesson.evidence_journal_ids,
                "source": "daily_batch",
                "candidate_lessons": stats_package.get("candidate_lessons", [])[:5],
                "journal_count": stats_package["journal_count"],
                "trade_case_count": stats_package.get("trade_case_count", 0),
                "retrieval_evaluation_count": stats_package.get("retrieval_evaluation_count", 0),
                "closed_trade_case_count": stats_package.get("closed_trade_case_count", 0),
                "evaluated_trade_count": stats_package["evaluated_trade_count"],
                "trade_case_patterns": stats_package.get("trade_case_patterns", {}),
                "trade_case_clusters": stats_package.get("trade_case_clusters", [])[:6],
                "trade_case_cluster_evaluation": stats_package.get("trade_case_cluster_evaluation", {}),
                "retrieval_evaluation_summary": stats_package.get("retrieval_evaluation_summary", {}),
            }
            entries.append(
                {
                    "rule": lesson.rule,
                    "version": 1,
                    "metrics": metrics,
                }
            )
        return entries

    def run(self, window_hours: int = 24):
        logger.info("Starting Daily Memory Update Batch with model: {}", self.model_name)
        journals = self._fetch_window_entries(window_hours=window_hours)
        if not journals:
            logger.info("No journals found in the selected window. Skipping.")
            return

        symbol_journals = self._fetch_symbol_window_entries(window_hours=window_hours)
        trade_cases = self._fetch_trade_case_window_entries(window_hours=window_hours)
        retrieval_evaluations = self._fetch_retrieval_window_entries(window_hours=window_hours)
        stats_package = self._build_stats_package(
            journals,
            window_hours=window_hours,
            symbol_journals=symbol_journals,
            trade_cases=trade_cases,
            retrieval_evaluations=retrieval_evaluations,
        )
        logger.info(
            "Prepared stats for memory update: journals={}, symbol_rows={}, trade_cases={}, evaluated={}, win_rate={:.2f}%",
            stats_package["journal_count"],
            stats_package["symbol_journal_count"],
            stats_package["trade_case_count"],
            stats_package["evaluated_trade_count"],
            stats_package["win_rate"],
        )
        lessons = self._generate_lessons(stats_package)
        entries = self._prepare_memory_entries(stats_package, lessons)

        if entries:
            self.state_store.save_long_term_memory_entries(entries)
            logger.info(f"Saved {len(entries)} grounded long-term memory entries.")
        else:
            logger.info("No actionable lessons extracted.")
        logger.info("Daily Memory Update Completed.")


if __name__ == "__main__":
    updater = MemoryUpdater()
    updater.run()
