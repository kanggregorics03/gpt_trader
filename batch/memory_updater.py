import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Literal

from dotenv import load_dotenv
from google import genai
from google.genai import types
from loguru import logger
from pydantic import BaseModel, Field

from db.state_store import StateStore

load_dotenv()


class MemoryLesson(BaseModel):
    rule: str = Field(description="Concrete lesson or anti-pattern in English.")
    regime: str = Field(description="Applicable regime such as trending_up or high_volatility.")
    category: Literal["lesson", "anti_pattern", "risk_control", "opportunity"]
    support_count: int = Field(ge=1)
    evidence_journal_ids: List[int] = Field(default_factory=list)
    hypothesis: bool = False


class MemoryUpdater:
    def __init__(self, db_path: str = "data/gpt_trader.db"):
        self.db_path = db_path
        self.state_store = StateStore(db_path)
        self.api_key = os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            logger.error("Missing GEMINI_API_KEY")
            raise SystemExit(1)
        self.client = genai.Client(api_key=self.api_key)
        self.model_name = "gemini-3.0-pro"

    def _fetch_window_entries(self, window_hours: int = 24) -> List[Dict[str, Any]]:
        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=window_hours)
        return self.state_store.get_journal_entries_since(since)

    def _build_stats_package(self, journals: List[Dict[str, Any]], window_hours: int) -> Dict[str, Any]:
        ledger_stats = self.state_store.get_position_ledger_stats(hours=window_hours)
        symbol_report = self.state_store.get_symbol_performance_report(hours=window_hours)
        daily_report = self.state_store.get_daily_ledger_report(days=max(1, round(window_hours / 24)))
        pnl_rows = [row for row in journals if row.get("estimated_pnl_pct") is not None]
        wins = [row for row in pnl_rows if float(row["estimated_pnl_pct"]) > 0]
        losses = [row for row in pnl_rows if float(row["estimated_pnl_pct"]) < 0]
        flat = [row for row in pnl_rows if float(row["estimated_pnl_pct"]) == 0]

        decision_breakdown: Dict[str, int] = {}
        reject_reason_counts: Dict[str, int] = {}
        for row in journals:
            decision = row.get("decision") or "UNKNOWN"
            decision_breakdown[decision] = decision_breakdown.get(decision, 0) + 1
            risk_reason = (row.get("risk_reason") or "").strip()
            if risk_reason and risk_reason not in ("Approved", "Safe action."):
                reject_reason_counts[risk_reason] = reject_reason_counts.get(risk_reason, 0) + 1

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

        return {
            "window_hours": window_hours,
            "journal_count": len(journals),
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
            "top_losses": [
                {
                    "id": row["id"],
                    "decision": row["decision"],
                    "estimated_pnl_pct": row["estimated_pnl_pct"],
                    "rationale": row["rationale"],
                    "risk_reason": row["risk_reason"],
                }
                for row in top_losses
            ],
            "top_wins": [
                {
                    "id": row["id"],
                    "decision": row["decision"],
                    "estimated_pnl_pct": row["estimated_pnl_pct"],
                    "rationale": row["rationale"],
                    "risk_reason": row["risk_reason"],
                }
                for row in top_wins
            ],
            "recent_examples": [
                {
                    "id": row["id"],
                    "decision": row["decision"],
                    "estimated_pnl_pct": row["estimated_pnl_pct"],
                    "loss_signal": row["loss_signal"],
                    "rationale": row["rationale"],
                    "risk_reason": row["risk_reason"],
                }
                for row in journals[-10:]
            ],
        }

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
5. Return only JSON matching the schema.
6. All text must be in English.

Computed statistics:
{stats_json}
"""

    def _generate_lessons(self, stats_package: Dict[str, Any]) -> List[MemoryLesson]:
        if stats_package["journal_count"] == 0:
            return []

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
                return []
            return [MemoryLesson.model_validate(item) for item in lessons]
        except Exception as e:
            logger.error(f"Gemini failed to extract lessons: {e}")
            return []

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
                "win_rate": stats_package["win_rate"],
                "avg_estimated_pnl_pct": stats_package["avg_estimated_pnl_pct"],
                "ledger_net_realized_pnl": ledger_stats.get("net_realized_pnl", 0.0),
                "evidence_journal_ids": lesson.evidence_journal_ids,
                "source": "daily_batch",
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
        logger.info("Starting Daily Memory Update Batch...")
        journals = self._fetch_window_entries(window_hours=window_hours)
        if not journals:
            logger.info("No journals found in the selected window. Skipping.")
            return

        stats_package = self._build_stats_package(journals, window_hours=window_hours)
        logger.info(
            "Prepared stats for memory update: journals={}, evaluated={}, win_rate={:.2f}%",
            stats_package["journal_count"],
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
