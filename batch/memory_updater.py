import json
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

    def _build_stats_package(
        self,
        journals: List[Dict[str, Any]],
        window_hours: int,
        symbol_journals: List[Dict[str, Any]] | None = None,
    ) -> Dict[str, Any]:
        symbol_journals = symbol_journals or []
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
        merged_candidate_lessons = self._merge_candidate_lesson_dicts(
            self._build_candidate_lessons(
                journals=journals,
                winning_evidence_signals=winning_evidence_signals,
                losing_evidence_signals=losing_evidence_signals,
                evidence_quality_patterns=evidence_quality_patterns,
            ),
            symbol_candidate_lessons,
        )

        return {
            "window_hours": window_hours,
            "journal_count": len(journals),
            "symbol_journal_count": len(symbol_journals),
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
                "evaluated_trade_count": stats_package["evaluated_trade_count"],
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
        stats_package = self._build_stats_package(
            journals,
            window_hours=window_hours,
            symbol_journals=symbol_journals,
        )
        logger.info(
            "Prepared stats for memory update: journals={}, symbol_rows={}, evaluated={}, win_rate={:.2f}%",
            stats_package["journal_count"],
            stats_package["symbol_journal_count"],
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
