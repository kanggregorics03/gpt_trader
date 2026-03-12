
import hashlib
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from ai.decision_engine import DecisionEngine, DecisionResult
from ai.llm_provider import normalize_provider_name, resolve_api_key, resolve_decision_models
from ai.rag_engine import RagEngine
from core.alpha_policy import AlphaPolicyEngine
from core.execution_policy import ExecutionPolicyEngine
from core.models import (
    DecisionRequest,
    EvidenceItem,
    RationaleItem,
    RiskStateSnapshot,
    TradeIntent,
    coerce_risk_state_snapshot,
)
from core.position_policy import PositionPolicyEngine
from core.risk_engine import RiskEngine
from core.safety_policy import SafetyPolicyEngine
from core.snapshot_service import SnapshotService
from db.state_store import StateStore
from exchange.account_reader import AccountReader
from exchange.bybit_factory import (
    create_private_account_feed,
    create_private_exchange,
    create_public_exchange,
    create_public_market_feed,
    normalize_trading_mode,
)
from exchange.collector import MarketCollector
from exchange.execution_engine import ExecutionEngine
from utils.logger import log

class TradingCycleSupportMixin:
    def _summarize_trade_cases(self, symbol_journal_entries: list[dict]) -> tuple[list[str], dict[str, str], str]:
        trade_case_ids = []
        case_statuses = {}
        summary_parts = []
        for entry in symbol_journal_entries or []:
            symbol = str(entry.get("symbol") or "")
            trade_case_id = entry.get("trade_case_id")
            case_status = entry.get("case_status")
            if trade_case_id and trade_case_id not in trade_case_ids:
                trade_case_ids.append(trade_case_id)
            if symbol and case_status:
                case_statuses[symbol] = case_status
                summary_parts.append(f"{symbol}:{case_status}")
        return trade_case_ids, case_statuses, ", ".join(summary_parts[:6])

    def _get_deterministic_skip_reason(
        self,
        account_state,
        market_snapshots,
        risk_state,
        risk_mode: str,
        risk_profile,
    ):
        if account_state.positions or account_state.open_orders:
            return None

        risk_state = self._coerce_risk_state(risk_state)
        risk_limits = self.risk_engine.policy.get("risk_limits", {})
        daily_max_loss_pct = float(risk_limits.get("daily_max_loss_pct", 0.0) or 0.0)
        daily_pnl_pct = float(risk_state.daily_pnl_pct or 0.0)
        if daily_max_loss_pct > 0 and daily_pnl_pct <= -daily_max_loss_pct:
            return "daily_loss_kill_switch"

        max_consecutive_losses = int(risk_limits.get("max_consecutive_losses", 0) or 0)
        consecutive_losses = int(risk_state.consecutive_losses or 0)
        if max_consecutive_losses > 0 and consecutive_losses >= max_consecutive_losses:
            return "consecutive_loss_kill_switch"

        return None

    def _build_skip_rationale_items(self, skip_reason: str, risk_state, market_snapshots, risk_profile):
        risk_state_payload = (
            dict(risk_state)
            if isinstance(risk_state, dict)
            else self._serialize_risk_state(risk_state)
        )
        risk_state_snapshot = self._coerce_risk_state(risk_state)
        if skip_reason == "alpha_policy_filtered_all":
            blocked_symbols = risk_state_payload.get("alpha_policy_blocked_symbols") or {}
            symbol, reasons = next(iter(blocked_symbols.items()), ("portfolio", ["policy_filtered"]))
            return [
                RationaleItem(
                    summary="Alpha policy blocked all candidate entries.",
                    why_it_matters="The champion 1h entry filter judged every current setup as low quality.",
                    trade_implication="Stay flat until at least one symbol clears the alpha entry filter.",
                    failure_mode="A future 1h snapshot satisfies the active alpha policy thresholds.",
                    evidence=[
                        EvidenceItem(
                            signal="alpha_policy_block",
                            symbol=symbol if symbol != "portfolio" else None,
                            timeframe="1h",
                            operator="note",
                            observed_value=", ".join(str(reason) for reason in reasons[:3]),
                            threshold=None,
                            units="rule",
                            source="alpha_policy.blocked_symbols",
                        )
                    ],
                )
            ]
        if skip_reason == "cooldown_active":
            remaining = float(risk_state_snapshot.cooldown_remaining_minutes or 0.0)
            effective_minutes = float(risk_state_snapshot.cooldown_minutes_effective or 0.0)
            failure_pattern_score = float(risk_state_snapshot.failure_pattern_score or 0.0)
            dominant_failure_reason = str(risk_state_snapshot.dominant_failure_reason or "").strip()
            evidence = [
                EvidenceItem(
                    signal="cooldown_remaining_minutes",
                    timeframe="risk",
                    operator="gt",
                    observed_value=remaining,
                    threshold=0.0,
                    units="minutes",
                    source="risk_state.cooldown_remaining_minutes",
                )
            ]
            if effective_minutes > 0:
                evidence.append(
                    EvidenceItem(
                        signal="cooldown_minutes_effective",
                        timeframe="risk",
                        operator="gte",
                        observed_value=effective_minutes,
                        threshold=effective_minutes,
                        units="minutes",
                        source="risk_state.cooldown_minutes_effective",
                    )
                )
            if failure_pattern_score > 0:
                evidence.append(
                    EvidenceItem(
                        signal="failure_pattern_score",
                        timeframe="risk",
                        operator="gt",
                        observed_value=failure_pattern_score,
                        threshold=0.0,
                        units="score",
                        source="risk_state.failure_pattern_score",
                    )
                )
            why_it_matters = "The strategy is in mandatory cooldown after recent losses."
            if dominant_failure_reason:
                why_it_matters += f" Repeated failure pattern: {dominant_failure_reason}."
            return [
                RationaleItem(
                    summary="Cooldown blocks new entries.",
                    why_it_matters=why_it_matters,
                    trade_implication="Keep the portfolio flat until cooldown expires and risk pressure normalizes.",
                    failure_mode="Cooldown expires and no other blocking risk state remains.",
                    evidence=evidence,
                )
            ]
        if skip_reason in ("recent_symbol_close_guard", "recent_symbol_loss_guard"):
            remaining = float(risk_state_snapshot.reentry_guard_remaining_minutes or 0.0)
            symbol = str(risk_state_snapshot.reentry_guard_symbol or self._primary_symbol())
            last_pnl = risk_state_snapshot.reentry_guard_last_estimated_pnl_pct
            last_exit_reason = str(risk_state_snapshot.reentry_guard_last_exit_reason or "").strip()
            why_it_matters = (
                f"{symbol} was closed too recently, so immediate re-entry would likely be churn."
                if skip_reason == "recent_symbol_close_guard"
                else f"{symbol} recently closed at a loss, so the symbol-level re-entry guard is still active."
            )
            if last_exit_reason:
                why_it_matters += f" Last exit reason: {last_exit_reason}."
            evidence = [
                EvidenceItem(
                    signal="reentry_guard_remaining_minutes",
                    symbol=symbol,
                    timeframe="risk",
                    operator="gt",
                    observed_value=remaining,
                    threshold=0.0,
                    units="minutes",
                    source="risk_state.reentry_guard_remaining_minutes",
                )
            ]
            if last_pnl is not None:
                evidence.append(
                    EvidenceItem(
                        signal="reentry_guard_last_estimated_pnl_pct",
                        symbol=symbol,
                        timeframe="risk",
                        operator="note",
                        observed_value=float(last_pnl),
                        threshold=None,
                        units="pct",
                        source="risk_state.reentry_guard_last_estimated_pnl_pct",
                    )
                )
            return [
                RationaleItem(
                    summary="Recent symbol exit blocks immediate re-entry.",
                    why_it_matters=why_it_matters,
                    trade_implication="Stay flat on the configured symbol until the symbol-level re-entry window expires.",
                    failure_mode="The re-entry guard expires and no other blocking risk state remains.",
                    evidence=evidence,
                )
            ]
        if skip_reason.startswith("risk_profile_"):
            max_positions = float(risk_profile.get("max_open_positions", 0.0) or 0.0)
            return [
                RationaleItem(
                    summary="Active risk profile disallows new entries.",
                    why_it_matters="Current policy mode only permits defensive actions.",
                    trade_implication="Return HOLD until the risk profile re-enables BUY or SELL.",
                    failure_mode="Risk mode changes and entry actions are allowed again.",
                    evidence=[
                        EvidenceItem(
                            signal="max_open_positions",
                            timeframe="risk",
                            operator="note",
                            observed_value=max_positions,
                            threshold=None,
                            units="count",
                            source="risk_profile.max_open_positions",
                        )
                    ],
                )
            ]
        if skip_reason == "daily_loss_kill_switch":
            daily_pnl_pct = float(risk_state_snapshot.daily_pnl_pct or 0.0)
            limit = float(self.risk_engine.policy.get("risk_limits", {}).get("daily_max_loss_pct", 0.0) or 0.0)
            return [
                RationaleItem(
                    summary="Daily loss kill switch is active.",
                    why_it_matters="Further entries would violate the daily drawdown control.",
                    trade_implication="Stop initiating new risk for the rest of the daily window.",
                    failure_mode="A new trading day resets the daily loss state.",
                    evidence=[
                        EvidenceItem(
                            signal="daily_pnl_pct",
                            timeframe="risk",
                            operator="lte",
                            observed_value=daily_pnl_pct,
                            threshold=-limit,
                            units="pct",
                            source="risk_state.daily_pnl_pct",
                        )
                    ],
                )
            ]
        if skip_reason == "consecutive_loss_kill_switch":
            losses = float(risk_state_snapshot.consecutive_losses or 0.0)
            limit = float(self.risk_engine.policy.get("risk_limits", {}).get("max_consecutive_losses", 0) or 0.0)
            return [
                RationaleItem(
                    summary="Consecutive-loss kill switch is active.",
                    why_it_matters="The recent loss streak signals degraded strategy conditions.",
                    trade_implication="Pause new entries until the loss streak resets.",
                    failure_mode="The loss streak resets under the configured policy rules.",
                    evidence=[
                        EvidenceItem(
                            signal="consecutive_losses",
                            timeframe="risk",
                            operator="gte",
                            observed_value=losses,
                            threshold=limit,
                            units="count",
                            source="risk_state.consecutive_losses",
                        )
                    ],
                )
            ]
        return []
