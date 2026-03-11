from dataclasses import dataclass
import hashlib
import json
import os
from typing import Any, Optional

from loguru import logger
from pydantic import ValidationError

from ai.llm_provider import (
    create_llm_provider,
    normalize_provider_name,
    resolve_api_key,
    resolve_base_url,
    resolve_decision_models,
)
from core.intent_validator import StructuredIntentValidator
from core.models import AITradeIntent, DecisionRequest, LongTermMemoryRecord, TradeIntent

@dataclass
class DecisionResult:
    intent: TradeIntent
    prompt: str
    raw_response: str
    model_name: str
    input_hash: str
    cache_hit: bool = False
    usage_metadata: dict | None = None

class DecisionEngine:
    def __init__(
        self,
        api_key: Optional[str] = None,
        cache_store: Optional[object] = None,
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: Optional[str] = None,
        guarded_attention_model: Optional[str] = None,
    ):
        self.provider_name = normalize_provider_name(provider)
        resolved_api_key = resolve_api_key(self.provider_name, explicit_api_key=api_key)
        resolved_base_url = resolve_base_url(self.provider_name, explicit_base_url=base_url)
        self.llm_provider = create_llm_provider(
            self.provider_name,
            api_key=resolved_api_key,
            base_url=resolved_base_url,
        )
        self.client = getattr(self.llm_provider, "client", None)
        self.flash_model, self.pro_model = resolve_decision_models(
            self.provider_name,
            default_model=default_model,
            guarded_attention_model=guarded_attention_model,
        )
        self.cache_store = cache_store
        self.intent_validator = StructuredIntentValidator()

    def _resolve_models(
        self,
        default_model: Optional[str] = None,
        guarded_attention_model: Optional[str] = None,
    ) -> tuple[str, str]:
        provider_name = getattr(self, "provider_name", normalize_provider_name())
        return resolve_decision_models(
            provider_name,
            default_model=default_model,
            guarded_attention_model=guarded_attention_model,
        )

    def _compact_json(self, payload) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)

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

    def _normalize_string_list(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            normalized = [self._normalize_text(item, default="") for item in value]
            return [item for item in normalized if item]
        normalized = self._normalize_text(value, default="")
        return [normalized] if normalized else []

    def _coerce_float(self, value: Any, *, default: float | None = None) -> float | None:
        if value is None:
            return default
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return default
        return default

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

    def _normalize_evidence_scalar(self, value: Any) -> float | str | bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return float(value)
        text = self._normalize_text(value, default="")
        return text

    def _normalize_order_payload(
        self,
        payload: Any,
        *,
        issues: list[str],
        order_index: int,
    ) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            issues.append(f"orders[{order_index}] is not an object")
            return None
        action = self._normalize_text(payload.get("action"), default="").upper()
        if action not in {"BUY", "SELL", "CANCEL"}:
            issues.append(f"orders[{order_index}].action is invalid")
            return None
        order_type = self._normalize_text(payload.get("order_type"), default="").upper()
        if order_type not in {"MARKET", "LIMIT"}:
            issues.append(f"orders[{order_index}].order_type is invalid")
            return None
        entry_style = self._normalize_text(payload.get("entry_style"), default="").lower()
        reduce_only = self._coerce_bool(payload.get("reduce_only"), default=False)
        if action in {"BUY", "SELL"} and not reduce_only:
            if entry_style not in {"immediate", "retest", "value"}:
                issues.append(f"orders[{order_index}].entry_style is invalid or missing")
                return None
        else:
            entry_style = ""
        symbol = self._normalize_text(payload.get("symbol"), default="")
        if not symbol:
            issues.append(f"orders[{order_index}].symbol is missing")
            return None
        return {
            "symbol": symbol,
            "action": action,
            "order_type": order_type,
            "entry_style": entry_style if action in {"BUY", "SELL"} and not reduce_only else None,
            "quantity": self._coerce_float(payload.get("quantity"), default=0.0) or 0.0,
            "price": self._coerce_float(payload.get("price")),
            "reduce_only": reduce_only,
            "take_profit": self._coerce_float(payload.get("take_profit")),
            "stop_loss": self._coerce_float(payload.get("stop_loss")),
            "target_order_id": self._normalize_text(payload.get("target_order_id"), default="") or None,
            "target_client_order_id": self._normalize_text(payload.get("target_client_order_id"), default="") or None,
            "replace_group_id": self._normalize_text(payload.get("replace_group_id"), default="") or None,
            "lineage_role": self._normalize_text(payload.get("lineage_role"), default="") or None,
            "lineage_parent_order_id": self._normalize_text(payload.get("lineage_parent_order_id"), default="") or None,
            "lineage_parent_client_order_id": self._normalize_text(payload.get("lineage_parent_client_order_id"), default="") or None,
            "wait_for_cancel_confirmation": self._coerce_bool(
                payload.get("wait_for_cancel_confirmation"),
                default=False,
            ),
        }

    def _normalize_evidence_payload(self, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        operator = self._normalize_text(payload.get("operator"), default="note").lower()
        allowed = {"gte", "lte", "gt", "lt", "eq", "cross_up", "cross_down", "delta_abs_gte", "note"}
        if operator not in allowed:
            operator = "note"
        observed_value = self._normalize_evidence_scalar(payload.get("observed_value"))
        threshold = self._normalize_evidence_scalar(payload.get("threshold"))
        numeric_operator = operator in {"gte", "lte", "gt", "lt", "cross_up", "cross_down", "delta_abs_gte"}
        if numeric_operator:
            observed_float = self._coerce_float(observed_value)
            threshold_float = self._coerce_float(threshold)
            if observed_float is None or threshold_float is None:
                operator = "note"
                threshold = None
            else:
                observed_value = observed_float
                threshold = threshold_float
        elif operator == "note":
            threshold = None
        return {
            "signal": self._normalize_text(payload.get("signal"), default="note"),
            "symbol": self._normalize_text(payload.get("symbol"), default="") or None,
            "timeframe": self._normalize_text(payload.get("timeframe"), default="risk"),
            "operator": operator,
            "observed_value": observed_value,
            "threshold": threshold,
            "units": self._normalize_text(payload.get("units"), default="") or None,
            "source": self._normalize_text(payload.get("source"), default="policy_summary"),
            "confidence_contribution": self._coerce_float(payload.get("confidence_contribution")),
        }

    def _normalize_rationale_item_payload(self, payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        raw_evidence = payload.get("evidence")
        if isinstance(raw_evidence, dict):
            raw_evidence = [raw_evidence]
        evidence = []
        for item in raw_evidence or []:
            normalized = self._normalize_evidence_payload(item)
            if normalized is not None:
                evidence.append(normalized)
        return {
            "summary": self._normalize_text(payload.get("summary"), default="Structured rationale item."),
            "why_it_matters": self._normalize_text(
                payload.get("why_it_matters"),
                default="This context affects decision quality.",
            ),
            "trade_implication": self._normalize_text(
                payload.get("trade_implication"),
                default="Keep position management conservative.",
            ),
            "failure_mode": self._normalize_text(payload.get("failure_mode"), default="") or None,
            "evidence": evidence,
        }

    def _normalize_trade_intent_payload(self, payload: Any) -> tuple[dict[str, Any], list[str]]:
        if not isinstance(payload, dict):
            return {}, ["root payload is not an object"]
        raw_orders = payload.get("orders")
        if isinstance(raw_orders, dict):
            raw_orders = [raw_orders]
        orders = []
        issues: list[str] = []
        for raw_index, item in enumerate(raw_orders or []):
            normalized = self._normalize_order_payload(
                item,
                issues=issues,
                order_index=raw_index,
            )
            if normalized is not None:
                orders.append(normalized)

        raw_rationale_items = payload.get("rationale_items")
        if isinstance(raw_rationale_items, dict):
            raw_rationale_items = [raw_rationale_items]
        rationale_items = []
        for item in raw_rationale_items or []:
            normalized = self._normalize_rationale_item_payload(item)
            if normalized is not None:
                rationale_items.append(normalized)

        decision = self._normalize_text(payload.get("decision"), default="").upper()
        if decision not in {"HOLD", "BUY", "SELL", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"}:
            issues.append("decision is invalid")
        if decision in {"BUY", "SELL", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"} and not orders:
            issues.append(f"{decision} requires at least one valid order")
        normalized_payload = {
            "decision": decision,
            "orders": orders,
            "rationale": self._normalize_string_list(payload.get("rationale")),
            "rationale_items": rationale_items,
            "risk_notes": self._normalize_string_list(payload.get("risk_notes")),
            "confidence": self._coerce_float(payload.get("confidence"), default=0.0) or 0.0,
            "thesis_id": self._normalize_text(payload.get("thesis_id"), default="") or None,
            "thesis_summary": self._normalize_text(payload.get("thesis_summary"), default="") or None,
            "invalidation_reason": self._normalize_text(payload.get("invalidation_reason"), default="") or None,
            "management_template_id": self._normalize_text(payload.get("management_template_id"), default="") or None,
            "exit_reason": self._normalize_text(payload.get("exit_reason"), default="") or None,
        }
        return normalized_payload, issues

    def _round_compact_value(self, value: Any) -> Any:
        if isinstance(value, float):
            return round(value, 4)
        if isinstance(value, list):
            return [self._round_compact_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._round_compact_value(item) for key, item in value.items()}
        return value

    def _drop_empty(self, payload: Any) -> Any:
        if isinstance(payload, dict):
            cleaned = {}
            for key, value in payload.items():
                compacted = self._drop_empty(value)
                if compacted is None:
                    continue
                if compacted == {} or compacted == []:
                    continue
                cleaned[key] = compacted
            return cleaned
        if isinstance(payload, list):
            cleaned = [self._drop_empty(item) for item in payload]
            return [item for item in cleaned if item is not None and item != {} and item != []]
        return payload

    def _truncate_text(self, value: Any, limit: int = 220) -> str:
        text = " ".join(str(value or "").split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + "..."

    def _compact_timeframe_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        compact = {
            "timeframe": payload.get("timeframe"),
            "last_close": payload.get("last_close"),
            "atr_pct": payload.get("atr_pct"),
            "realized_volatility_pct": payload.get("realized_volatility_pct"),
            "rsi_14": payload.get("rsi_14"),
            "trend": payload.get("trend"),
            "change_1bar_pct": payload.get("change_1bar_pct"),
            "change_3bar_pct": payload.get("change_3bar_pct"),
            "change_6bar_pct": payload.get("change_6bar_pct"),
            "distance_to_range_high_pct": payload.get("distance_to_range_high_pct"),
            "distance_to_range_low_pct": payload.get("distance_to_range_low_pct"),
            "range_position_pct": payload.get("range_position_pct"),
            "breakout_up": payload.get("breakout_up"),
            "breakout_down": payload.get("breakout_down"),
            "volume_ratio": payload.get("volume_ratio"),
            "volume_surge": payload.get("volume_surge"),
            "close_vs_vwap_pct": payload.get("close_vs_vwap_pct"),
        }
        return self._drop_empty(self._round_compact_value(compact))

    def _compact_market_snapshot_payload(
        self,
        payload: dict[str, Any],
        *,
        include_signal_triggers: bool = True,
        allowed_timeframes: set[str] | None = None,
    ) -> dict[str, Any]:
        timeframe_keys = allowed_timeframes or {"5m", "15m", "1h", "4h"}
        compact = {
            "symbol": payload.get("symbol"),
            "current_price": payload.get("current_price"),
            "mark_price": payload.get("mark_price"),
            "index_price": payload.get("index_price"),
            "atr_15m": payload.get("atr_15m"),
            "atr_1h": payload.get("atr_1h"),
            "trend_1h": payload.get("trend_1h"),
            "price_change_24h_pct": payload.get("price_change_24h_pct"),
            "distance_from_24h_high_pct": payload.get("distance_from_24h_high_pct"),
            "distance_from_24h_low_pct": payload.get("distance_from_24h_low_pct"),
            "recent_volume_surge": payload.get("recent_volume_surge"),
            "volume_ratio_15m": payload.get("volume_ratio_15m"),
            "volume_surge_15m_now": payload.get("volume_surge_15m_now"),
            "oi_change_15m": payload.get("oi_change_15m"),
            "oi_change_1h": payload.get("oi_change_1h"),
            "funding_rate_change_1h": payload.get("funding_rate_change_1h"),
            "basis_change_1h": payload.get("basis_change_1h"),
            "spread_vs_1h_avg": payload.get("spread_vs_1h_avg"),
            "spread_percentile_1h": payload.get("spread_percentile_1h"),
            "depth_percentile_1h": payload.get("depth_percentile_1h"),
            "distance_to_15m_breakout_level": payload.get("distance_to_15m_breakout_level"),
            "distance_to_1h_breakout_level": payload.get("distance_to_1h_breakout_level"),
            "distance_to_invalid_level": payload.get("distance_to_invalid_level"),
            "is_risk_trigger": payload.get("is_risk_trigger"),
            "risk_trigger_reasons": payload.get("risk_trigger_reasons"),
            "trigger_reasons": payload.get("trigger_reasons"),
            "timeframes": {
                key: self._compact_timeframe_snapshot(value)
                for key, value in (payload.get("timeframes") or {}).items()
                if key in timeframe_keys and isinstance(value, dict)
            },
            "order_book": {
                "best_bid": ((payload.get("order_book") or {}).get("best_bid")),
                "best_ask": ((payload.get("order_book") or {}).get("best_ask")),
                "spread_bps": ((payload.get("order_book") or {}).get("spread_bps")),
                "mid_price": ((payload.get("order_book") or {}).get("mid_price")),
                "bid_depth_top5": ((payload.get("order_book") or {}).get("bid_depth_top5")),
                "ask_depth_top5": ((payload.get("order_book") or {}).get("ask_depth_top5")),
                "imbalance_top5": ((payload.get("order_book") or {}).get("imbalance_top5")),
                "microprice": ((payload.get("order_book") or {}).get("microprice")),
            },
            "derivatives": {
                "basis_pct": ((payload.get("derivatives") or {}).get("basis_pct")),
                "funding_rate": ((payload.get("derivatives") or {}).get("funding_rate")),
                "next_funding_rate": ((payload.get("derivatives") or {}).get("next_funding_rate")),
                "open_interest": ((payload.get("derivatives") or {}).get("open_interest")),
            },
            "regime": {
                "trend_alignment": ((payload.get("regime") or {}).get("trend_alignment")),
                "volatility_regime": ((payload.get("regime") or {}).get("volatility_regime")),
                "momentum_regime": ((payload.get("regime") or {}).get("momentum_regime")),
                "liquidity_regime": ((payload.get("regime") or {}).get("liquidity_regime")),
            },
        }
        if include_signal_triggers:
            compact.update(
                {
                    "fast_move_signal_active": payload.get("fast_move_signal_active"),
                    "volume_surge_signal_active": payload.get("volume_surge_signal_active"),
                    "range_break_signal_active": payload.get("range_break_signal_active"),
                }
            )
        return self._drop_empty(self._round_compact_value(compact))

    def _build_market_payload(self, request: DecisionRequest):
        loop_mode = str(request.loop_mode or "").strip().lower()
        include_signal_triggers = loop_mode != "1h"
        allowed_timeframes = {"15m", "1h", "4h"} if loop_mode == "1h" else {"5m", "15m", "1h", "4h"}
        if request.market_snapshots:
            return {
                symbol: self._compact_market_snapshot_payload(
                    snapshot.model_dump(mode="json"),
                    include_signal_triggers=include_signal_triggers,
                    allowed_timeframes=allowed_timeframes,
                )
                for symbol, snapshot in request.market_snapshots.items()
            }
        if request.market_snapshot is not None:
            return self._compact_market_snapshot_payload(
                request.market_snapshot.model_dump(mode="json"),
                include_signal_triggers=include_signal_triggers,
                allowed_timeframes=allowed_timeframes,
            )
        return {}

    def _build_recent_journal_context_payload(self, request: DecisionRequest):
        payload = []
        for item in request.recent_journal_context:
            raw = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            rationale = raw.get("rationale") or []
            evidence_quality = raw.get("evidence_quality") or {}
            entry_size_cap = raw.get("entry_size_cap") or {}
            rationale_items = []
            for rationale_item in (raw.get("rationale_items") or [])[:2]:
                evidence_payload = []
                for evidence in ((rationale_item or {}).get("evidence") or [])[:3]:
                    evidence_payload.append(
                        self._drop_empty(
                            {
                                "signal": evidence.get("signal"),
                                "symbol": evidence.get("symbol"),
                                "timeframe": evidence.get("timeframe"),
                                "operator": evidence.get("operator"),
                                "observed_value": evidence.get("observed_value"),
                                "threshold": evidence.get("threshold"),
                                "source": evidence.get("source"),
                            }
                        )
                    )
                rationale_items.append(
                    self._drop_empty(
                        {
                            "summary": self._truncate_text((rationale_item or {}).get("summary"), limit=140),
                            "failure_mode": self._truncate_text((rationale_item or {}).get("failure_mode"), limit=140),
                            "trade_implication": self._truncate_text((rationale_item or {}).get("trade_implication"), limit=120),
                            "evidence": evidence_payload,
                        }
                    )
                )
            payload.append(
                self._drop_empty(
                    {
                        "journal_id": raw.get("journal_id") or raw.get("id"),
                        "audit_log_id": raw.get("audit_log_id"),
                        "timestamp": raw.get("timestamp"),
                        "decision": raw.get("decision"),
                        "summary": self._truncate_text(raw.get("summary") or (rationale[0] if rationale else ""), limit=160),
                        "case_summary": self._truncate_text(raw.get("case_summary"), limit=180),
                        "confidence": raw.get("confidence"),
                        "risk_reason": self._truncate_text(raw.get("risk_reason"), limit=120),
                        "estimated_pnl_pct": raw.get("estimated_pnl_pct"),
                        "loss_signal": raw.get("loss_signal"),
                        "risk_mode": raw.get("risk_mode"),
                        "trigger_symbols": (raw.get("trigger_symbols") or [])[:3],
                        "risk_trigger_symbols": (raw.get("risk_trigger_symbols") or [])[:3],
                        "trade_case_ids": (raw.get("trade_case_ids") or [])[:3],
                        "case_statuses": raw.get("case_statuses") or {},
                        "evidence_quality": {
                            "factor": evidence_quality.get("factor"),
                            "issues": (evidence_quality.get("issues") or [])[:2],
                        },
                        "rationale_items": rationale_items,
                        "entry_size_cap": {
                            "adjusted": entry_size_cap.get("adjusted"),
                            "effective_cap_pct": entry_size_cap.get("effective_cap_pct"),
                            "base_cap_pct": entry_size_cap.get("base_cap_pct"),
                        },
                    }
                )
            )
        return payload

    def _build_long_term_memory_payload(self, request: DecisionRequest):
        payload = []
        for item in request.long_term_memory:
            if isinstance(item, LongTermMemoryRecord):
                raw = item.model_dump(mode="json")
            elif isinstance(item, dict):
                raw = dict(item)
            else:
                raw = {"rule": str(item)}
            metrics = raw.get("metrics") or {}
            payload.append(
                self._drop_empty(
                    self._round_compact_value(
                        {
                            "rule": self._truncate_text(raw.get("rule"), limit=220),
                            "category": raw.get("category"),
                            "status": raw.get("status"),
                            "support": raw.get("support"),
                            "regime": raw.get("regime"),
                            "apply_when": raw.get("apply_when") or {},
                            "ignore_when": (raw.get("ignore_when") or [])[:2],
                            "metrics": {
                                "confidence_score": metrics.get("confidence_score"),
                                "validity_score": metrics.get("validity_score"),
                                "freshness_score": metrics.get("freshness_score"),
                                "lifecycle_status": metrics.get("lifecycle_status"),
                            },
                        }
                    )
                )
            )
        return payload

    def _build_similar_past_cases_payload(self, request: DecisionRequest):
        payload = []
        for item in request.similar_past_cases:
            if isinstance(item, str):
                payload.append(self._truncate_text(item, limit=220))
            elif isinstance(item, dict):
                payload.append(
                    self._drop_empty(
                        {
                            "symbol": item.get("symbol"),
                            "summary": self._truncate_text(item.get("summary") or item.get("case"), limit=200),
                            "outcome": self._truncate_text(item.get("outcome"), limit=120),
                            "why_similar": self._truncate_text(item.get("why_similar"), limit=120),
                        }
                    )
                )
        return payload

    def _build_stable_account_payload(self, request: DecisionRequest) -> dict:
        # Exclude volatile fields so equivalent states can reuse cached AI decisions.
        payload = request.account_state.model_dump(mode="json")
        payload.pop("snapshot_timestamp", None)
        return payload

    def _build_compact_risk_state_payload(self, request: DecisionRequest) -> dict:
        raw = request.risk_state.model_dump(mode="json")
        compact = {
            "daily_pnl_pct": raw.get("daily_pnl_pct"),
            "drawdown_active": raw.get("drawdown_active"),
            "drawdown_activate_pct": raw.get("drawdown_activate_pct"),
            "cooldown_active": raw.get("cooldown_active"),
            "cooldown_remaining_minutes": raw.get("cooldown_remaining_minutes"),
            "cooldown_minutes_effective": raw.get("cooldown_minutes_effective"),
            "cooldown_components": raw.get("cooldown_components"),
            "consecutive_losses": raw.get("consecutive_losses"),
            "last_loss_estimated_pnl_pct": raw.get("last_loss_estimated_pnl_pct"),
            "failure_pattern_score": raw.get("failure_pattern_score"),
            "dominant_failure_reason": raw.get("dominant_failure_reason"),
            "dominant_failure_count": raw.get("dominant_failure_count"),
            "reentry_guard_active": raw.get("reentry_guard_active"),
            "reentry_guard_reason": raw.get("reentry_guard_reason"),
            "reentry_guard_remaining_minutes": raw.get("reentry_guard_remaining_minutes"),
            "alpha_policy_blocked_symbols": raw.get("alpha_policy_blocked_symbols"),
        }
        return self._drop_empty(self._round_compact_value(compact))

    def _build_entry_headroom_payload(self, request: DecisionRequest) -> dict:
        return self._drop_empty(self._round_compact_value(dict(request.entry_headroom or {})))
        
    def _create_prompt(self, request: DecisionRequest) -> str:
        market_payload = self._build_market_payload(request)
        memory_payload = self._build_long_term_memory_payload(request)
        recent_journal_payload = self._build_recent_journal_context_payload(request)
        similar_cases_payload = self._build_similar_past_cases_payload(request)
        compact_risk_state = self._build_compact_risk_state_payload(request)
        entry_headroom_payload = self._build_entry_headroom_payload(request)
        prompt = f"""
You are an expert crypto trading strategist.
You are ONLY a strategist. Do not bypass risk limits.
Return ONLY valid JSON matching the TradeIntent schema.

INPUTS
policy_summary:
{request.policy_summary}
account_state:
{self._compact_json(self._build_stable_account_payload(request))}
market_snapshots:
{self._compact_json(market_payload)}
risk_state:
{self._compact_json(compact_risk_state)}
entry_headroom:
{self._compact_json(entry_headroom_payload)}
risk_mode:
{request.risk_mode}
loop_mode:
{request.loop_mode}
cycle_role:
{request.cycle_role or ""}
risk_profile:
{self._compact_json(request.risk_profile)}
recent_journal_context:
{self._compact_json(recent_journal_payload)}
long_term_memory:
{self._compact_json(memory_payload)}
similar_past_cases:
{self._compact_json(similar_cases_payload)}
guarded_attention_active:
{str(request.is_guarded_attention).lower()}

MARKET INTERPRETATION
0. market_snapshots, recent_journal_context, long_term_memory, and similar_past_cases are compact decision views. They intentionally remove low-value duplication while preserving decision-critical structure, trigger, execution, and outcome information.
0a. The retrieved contexts are sampled aides, not ground truth. Do not follow their majority vote mechanically.
0b. If retrieved context conflicts with the current market_snapshots or risk_state, prefer the live snapshot evidence.
0c. Treat fallback, retired, and error-like context as cautionary metadata, not as direct trading instructions.
0d. policy_summary is an extracted policy digest, not a full document. Treat it as the binding summary of enforceable limits and execution guards.
1. Prefer derived fields such as change, percentile, distance, and stabilized trigger state over raw values when making decisions.
2. volume_surge_15m_now is the immediate current-bar condition. volume_surge_signal_active is the stabilized signal state after hysteresis. Do not treat them as interchangeable.
3. fast_move_signal_active, volume_surge_signal_active, and range_break_signal_active are short-horizon stabilization signals. Use them in 15m and 5m loops for cadence and follow-through. In the 1h loop, prioritize higher-timeframe structure, breakout distance, trend alignment, and participation instead.
3a. In the 1h loop, treat 15m context as entry confirmation only. Do not let 15m momentum or noise create a fresh thesis that is not already supported by the 1h structure.
4. spread_vs_1h_avg, spread_percentile_1h, and depth_percentile_1h describe execution quality. Poor execution quality should reduce aggressiveness even if direction looks favorable.
5. oi_change_15m, oi_change_1h, funding_rate_change_1h, and basis_change_1h describe change and participation, not just static state.
6. distance_to_15m_breakout_level, distance_to_1h_breakout_level, and distance_to_invalid_level describe structure quality. Smaller and clearer invalidation distance generally supports cleaner entries.
7. entry_headroom summarizes remaining entry budget and leverage room before this decision. Treat blocked or near-zero headroom as a real constraint, not a suggestion.

RULES
1. Analyze structure, participation, trigger state, execution quality, and recent performance.
2. Decide one of HOLD, BUY, SELL, REDUCE, EXIT, CANCEL, REPLACE_ENTRY at the portfolio level.
3. If entering or reducing, populate orders and include take_profit / stop_loss when appropriate.
3a. For non-reduce BUY/SELL entry orders, set entry_style to one of immediate, retest, or value. immediate means the thesis requires entering now. retest means wait for a breakout or reclaim retest near structure. value means wait for a general passive retrace away from the current price.
3c. Use retest only when there is explicit structure evidence such as breakout/reclaim context, nearby breakout distance, or a clearly defined retest level. If the setup is not anchored to structure, do not call it retest.
3d. Use value only for a passive retrace inside an already acceptable directional context. If higher-timeframe trend alignment is unclear or the invalidation distance is too wide, prefer HOLD instead of a value entry.
3b. If the decision is REDUCE or EXIT, use MARKET orders. Do not express exit or reduction intent as passive limit waiting.
4. Distinguish follow-through or breakout trigger state from risk triggers when they are present. Risk triggers mean more conservative execution and possible de-risking.
5. Review existing open orders and avoid duplicate or conflicting orders for the same symbol.
6. If liquidation risk, cooldown pressure, drawdown pressure, wide spread, weak depth, or unfavorable funding is elevated, prefer REDUCE or EXIT over new exposure.
7. If loop_mode is 5m or cycle_role is execution_safety, do not open new positions. Only HOLD, REDUCE, EXIT, or CANCEL are valid.
8. If loop_mode is 15m or cycle_role is position_management, avoid opening new positions and focus on managing live exposure and stale working orders conservatively. Prefer CANCEL over repricing.
9. Use REPLACE_ENTRY only when explicitly managing a working entry in the 1h strategy loop. Never use it in 5m safety or 15m position-management loops.
9a. entry_style and order_type must agree semantically: immediate should usually pair with MARKET or an aggressive limit, while retest and value should pair with LIMIT.
9b. Do not emit execution_profile, urgency_score, max_slippage_bps, time_in_force, post_only, or limit_offset_bps. The runtime execution layer computes those fields.
10. Only the 1h strategy loop should initiate fresh directional exposure unless the risk_profile explicitly blocks entries.
11. In guarded state, prioritize risk reduction and execution quality over new exposure.
12. Consider idempotency and avoid repeating a failed request from recent_journal_context.
13. Put identified risks in risk_notes.
14. All text fields must be English.
15. market_snapshots contains the configured trading symbol set. Do not invent or reference other symbols.
16. Do not force a trade just because trigger state is active.
17. A breakout without volume, open-interest support, or acceptable execution quality is weak.
18. If price is too extended relative to ATR or too far from a clean invalidation level, reduce confidence or prefer HOLD.
19. rationale must stay concise for humans, but rationale_items must carry the machine-checkable evidence.
20. Every rationale_item must include at least one evidence row with a valid source path from the DecisionRequest, and each rationale_item should include at least one numeric evidence row whenever possible.
21. source must point to an existing field such as market_snapshots.BTCUSDT.timeframes.15m.rsi_14, market_snapshots.BTCUSDT.oi_change_15m, risk_state.cooldown_remaining_minutes, or market_snapshots.BTCUSDT.timeframes.1h.trend.
22. Numeric operators gte/lte/gt/lt/cross_up/cross_down/delta_abs_gte require numeric observed_value and numeric threshold.
23. If you reference a categorical or boolean field, use operator eq or note only. For eq, observed_value and threshold must match the original categorical/bool value exactly, e.g. "FLAT" or true.
24. If operator is not note, threshold is required. If operator is note, threshold must be null.
25. Do not use vague words such as strong, weak, likely, maybe unless the same rationale_item contains a numeric comparison.

OUTPUT EXAMPLE
{{
  "decision":"BUY",
  "orders":[{{"symbol":"BTCUSDT","action":"BUY","order_type":"MARKET","entry_style":"immediate","quantity":0.01,"price":null,"reduce_only":false,"take_profit":31200.0,"stop_loss":29850.0}}],
  "rationale":["BTC breakout has volume and open-interest follow-through while execution quality remains acceptable."],
  "rationale_items":[{{"summary":"BTC breakout has participation and acceptable execution quality","why_it_matters":"Continuation setups are stronger when structure, participation, and execution quality align.","trade_implication":"Allows one small long entry with defined risk.","failure_mode":"Invalidate if the breakout loses participation or falls back through the invalidation level.","evidence":[{{"signal":"volume_ratio_15m","symbol":"BTCUSDT","timeframe":"15m","operator":"gte","observed_value":2.1,"threshold":1.8,"units":"ratio","source":"market_snapshots.BTCUSDT.volume_ratio_15m","confidence_contribution":0.22}},{{"signal":"oi_change_15m","symbol":"BTCUSDT","timeframe":"15m","operator":"gt","observed_value":3.4,"threshold":0.0,"units":"pct","source":"market_snapshots.BTCUSDT.oi_change_15m","confidence_contribution":0.18}},{{"signal":"spread_percentile_1h","symbol":"BTCUSDT","timeframe":"risk","operator":"lte","observed_value":42.0,"threshold":70.0,"units":"percentile","source":"market_snapshots.BTCUSDT.spread_percentile_1h","confidence_contribution":0.1}},{{"signal":"distance_to_invalid_level","symbol":"BTCUSDT","timeframe":"1h","operator":"lte","observed_value":0.9,"threshold":1.2,"units":"atr","source":"market_snapshots.BTCUSDT.distance_to_invalid_level","confidence_contribution":0.14}}]}}],
  "risk_notes":["Wide spread or negative follow-through would invalidate the setup."],
  "confidence":0.62
}}
"""
        return prompt

    def _build_candidate_input_hash(self, request: DecisionRequest) -> str:
        payload = {
            "policy_summary": request.policy_summary,
            "account_state": self._build_stable_account_payload(request),
            "market_snapshots": self._build_market_payload(request),
            "risk_state": self._build_compact_risk_state_payload(request),
            "entry_headroom": self._build_entry_headroom_payload(request),
            "risk_mode": request.risk_mode,
            "loop_mode": request.loop_mode,
            "cycle_role": request.cycle_role,
            "risk_profile": request.risk_profile,
            "recent_journal_context": self._build_recent_journal_context_payload(request),
            "long_term_memory": self._build_long_term_memory_payload(request),
            "similar_past_cases": self._build_similar_past_cases_payload(request),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _build_prompt_cache_key(self, prompt: str, model_name: str) -> str:
        payload = f"{model_name}\n{prompt}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _extract_usage_metadata(self, response) -> dict:
        usage = getattr(response, "usage_metadata", None) or getattr(response, "usageMetadata", None)
        if usage is None:
            return {}
        if isinstance(usage, dict):
            return dict(usage)

        extracted = {}
        candidate_fields = [
            "prompt_token_count",
            "candidates_token_count",
            "total_token_count",
            "cached_content_token_count",
            "thoughts_token_count",
        ]
        for field in candidate_fields:
            value = getattr(usage, field, None)
            if value is not None:
                extracted[field] = value
        return extracted

    def generate_intent(self, request: DecisionRequest) -> TradeIntent:
        return self.generate_decision(request).intent

    def generate_decision(self, request: DecisionRequest) -> DecisionResult:
        prompt = self._create_prompt(request)
        input_hash = self._build_candidate_input_hash(request)
        model_to_use = self.pro_model if request.is_guarded_attention else self.flash_model
        prompt_hash = self._build_prompt_cache_key(prompt, model_to_use)
        logger.debug(
            f"Prompt generated for {getattr(self, 'provider_name', 'unknown')}. "
            f"Using model: {model_to_use}, input_hash={input_hash}"
        )

        if self.cache_store is not None:
            cached = self.cache_store.get_cached_decision(prompt_hash)
            if cached and cached.get("model_name") == model_to_use:
                logger.info(f"Decision cache hit: {prompt_hash}")
                return DecisionResult(
                    intent=TradeIntent.model_validate(cached.get("intent") or {}),
                    prompt=prompt,
                    raw_response=cached.get("raw_response") or "",
                    model_name=model_to_use,
                    input_hash=input_hash,
                    cache_hit=True,
                    usage_metadata={"cache_hit": True, "model_name": model_to_use},
                )
        
        try:
            provider = getattr(self, "llm_provider", None)
            if provider is None:
                raise RuntimeError("DecisionEngine is missing llm_provider.")
            logger.info(
                "Calling {} API with model {} (guarded_attention={}, input_hash={})",
                getattr(self, "provider_name", "unknown"),
                model_to_use,
                bool(request.is_guarded_attention),
                input_hash,
            )
            response = provider.generate_json(
                prompt=prompt,
                model_name=model_to_use,
                schema=AITradeIntent,
                temperature=0.2,
            )
            logger.info(
                "{} API response received from model {} (input_hash={})",
                getattr(self, "provider_name", "unknown"),
                model_to_use,
                input_hash,
            )
            raw_text = response.text
            intent_payload, normalization_issues = self._normalize_trade_intent_payload(json.loads(raw_text))
            if normalization_issues:
                logger.warning(
                    "{} output failed semantic normalization: {}",
                    getattr(self, "provider_name", "unknown"),
                    ", ".join(normalization_issues),
                )
                return DecisionResult(
                    intent=TradeIntent(
                        decision="HOLD",
                        orders=[],
                        rationale=["Fallback due to invalid semantic contract from AI."],
                        rationale_items=[],
                        risk_notes=["Semantic contract validation failed."],
                        confidence=0.0,
                    ),
                    prompt=prompt,
                    raw_response=raw_text,
                    model_name=model_to_use,
                    input_hash=input_hash,
                    cache_hit=False,
                    usage_metadata={
                        "semantic_validation_error": True,
                        "model_name": model_to_use,
                        "issues": normalization_issues,
                    },
                )
            intent = TradeIntent.model_validate(intent_payload)
            validation_result = self.intent_validator.validate(request, intent)
            if not validation_result.is_valid:
                logger.warning(
                    "{} structured rationale validation failed: {}",
                    getattr(self, "provider_name", "unknown"),
                    ", ".join(validation_result.issues),
                )
                return DecisionResult(
                    intent=TradeIntent(
                        decision="HOLD",
                        orders=[],
                        rationale=["Fallback due to invalid structured rationale from AI."],
                        rationale_items=[],
                        risk_notes=["Structured rationale validation failed."],
                        confidence=0.0,
                    ),
                    prompt=prompt,
                    raw_response=raw_text,
                    model_name=model_to_use,
                    input_hash=input_hash,
                    cache_hit=False,
                    usage_metadata={
                        "semantic_validation_error": True,
                        "model_name": model_to_use,
                        "issues": validation_result.issues,
                    },
                )
            usage_metadata = dict(response.usage_metadata or {})
            if self.cache_store is not None:
                self.cache_store.save_cached_decision(
                    prompt_hash=prompt_hash,
                    model_name=model_to_use,
                    input_hash=input_hash,
                    raw_response=raw_text,
                    intent=intent.model_dump(mode="json"),
                )
            if usage_metadata:
                logger.info(
                    f"{getattr(self, 'provider_name', 'AI').upper()} Decision: "
                    f"{intent.decision} (confidence: {intent.confidence}, "
                    f"tokens={usage_metadata.get('total_token_count', 'unknown')})"
                )
            else:
                logger.info(
                    f"{getattr(self, 'provider_name', 'AI').upper()} Decision: "
                    f"{intent.decision} (confidence: {intent.confidence})"
                )
            return DecisionResult(
                intent=intent,
                prompt=prompt,
                raw_response=raw_text,
                model_name=model_to_use,
                input_hash=input_hash,
                cache_hit=False,
                usage_metadata=usage_metadata,
            )
             
        except (ValidationError, json.JSONDecodeError, TypeError) as ve:
            logger.error(
                f"{getattr(self, 'provider_name', 'AI').upper()} output failed validation: {ve}"
            )
            # Fallback safe intent
            return DecisionResult(
                intent=TradeIntent(
                    decision="HOLD",
                    rationale=["Fallback due to json validation error from AI."],
                    risk_notes=["System Error"],
                    confidence=0.0
                ),
                prompt=prompt,
                raw_response="",
                model_name=model_to_use,
                input_hash=input_hash,
                cache_hit=False,
                usage_metadata={"validation_error": True, "model_name": model_to_use},
            )
        except Exception as e:
            logger.error(
                f"Failed to call {getattr(self, 'provider_name', 'AI').upper()} API: {e}"
            )
            raise
