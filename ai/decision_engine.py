from dataclasses import dataclass
import hashlib
import json
import os
from typing import Any, Optional

from google import genai
from google.genai import types
from loguru import logger
from pydantic import ValidationError

from core.intent_validator import StructuredIntentValidator
from core.models import DecisionRequest, LongTermMemoryRecord, TradeIntent

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
        api_key: str,
        cache_store: Optional[object] = None,
        default_model: Optional[str] = None,
        trigger_model: Optional[str] = None,
    ):
        self.client = genai.Client(api_key=api_key)
        self.flash_model, self.pro_model = self._resolve_models(
            default_model=default_model,
            trigger_model=trigger_model,
        )
        self.cache_store = cache_store
        self.intent_validator = StructuredIntentValidator()

    def _resolve_models(
        self,
        default_model: Optional[str] = None,
        trigger_model: Optional[str] = None,
    ) -> tuple[str, str]:
        legacy_model = os.getenv("GEMINI_MODEL", "").strip()
        resolved_default = (
            (default_model or "").strip()
            or (os.getenv("GEMINI_DEFAULT_MODEL", "").strip())
            or legacy_model
            or "gemini-3.0-pro"
        )
        resolved_trigger = (
            (trigger_model or "").strip()
            or (os.getenv("GEMINI_TRIGGER_MODEL", "").strip())
            or resolved_default
        )
        return resolved_default, resolved_trigger

    def _compact_json(self, payload) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)

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

    def _compact_market_snapshot_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
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
            "fast_move_trigger_active": payload.get("fast_move_trigger_active"),
            "volume_surge_trigger_active": payload.get("volume_surge_trigger_active"),
            "range_break_trigger_active": payload.get("range_break_trigger_active"),
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
            "is_opportunity_trigger": payload.get("is_opportunity_trigger"),
            "is_risk_trigger": payload.get("is_risk_trigger"),
            "opportunity_trigger_reasons": payload.get("opportunity_trigger_reasons"),
            "risk_trigger_reasons": payload.get("risk_trigger_reasons"),
            "trigger_reasons": payload.get("trigger_reasons"),
            "timeframes": {
                key: self._compact_timeframe_snapshot(value)
                for key, value in (payload.get("timeframes") or {}).items()
                if key in {"5m", "15m", "1h", "4h"} and isinstance(value, dict)
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
        return self._drop_empty(self._round_compact_value(compact))

    def _build_market_payload(self, request: DecisionRequest):
        if request.market_snapshots:
            return {
                symbol: self._compact_market_snapshot_payload(snapshot.model_dump(mode="json"))
                for symbol, snapshot in request.market_snapshots.items()
            }
        if request.market_snapshot is not None:
            return self._compact_market_snapshot_payload(request.market_snapshot.model_dump(mode="json"))
        return {}

    def _build_recent_journal_context_payload(self, request: DecisionRequest):
        payload = []
        for item in request.recent_journal_context:
            raw = item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
            rationale = raw.get("rationale") or []
            evidence_quality = raw.get("evidence_quality") or {}
            entry_size_cap = raw.get("entry_size_cap") or {}
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
                            "ignore_when": (raw.get("ignore_when") or [])[:1],
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
        
    def _create_prompt(self, request: DecisionRequest) -> str:
        market_payload = self._build_market_payload(request)
        memory_payload = self._build_long_term_memory_payload(request)
        recent_journal_payload = self._build_recent_journal_context_payload(request)
        similar_cases_payload = self._build_similar_past_cases_payload(request)
        prompt = f"""
You are an expert crypto trading strategist.
You are ONLY a strategist. Do not bypass risk limits.
Return ONLY valid JSON matching the TradeIntent schema.

INPUTS
policy_summary:
{request.policy_summary}
account_state:
{self._compact_json(self._build_stable_account_payload(request))}
market_context:
{self._compact_json(market_payload)}
risk_state:
{request.risk_state.model_dump_json()}
risk_mode:
{request.risk_mode}
risk_profile:
{self._compact_json(request.risk_profile)}
recent_journal_context:
{self._compact_json(recent_journal_payload)}
long_term_memory:
{self._compact_json(memory_payload)}
similar_past_cases:
{self._compact_json(similar_cases_payload)}
trigger_mode_active:
{str(request.is_trigger_mode).lower()}

MARKET INTERPRETATION
0. market_context, recent_journal_context, long_term_memory, and similar_past_cases are compact decision views. They intentionally remove low-value duplication while preserving decision-critical structure, trigger, execution, and outcome information.
1. Prefer derived fields such as change, percentile, distance, and trigger-state over raw values when making decisions.
2. volume_surge_15m_now is the immediate current-bar condition. volume_surge_trigger_active is the stabilized trigger state after hysteresis. Do not treat them as interchangeable.
3. fast_move_trigger_active, volume_surge_trigger_active, and range_break_trigger_active represent stabilized trigger states and matter more for cadence and follow-through than one-bar noise.
4. spread_vs_1h_avg, spread_percentile_1h, and depth_percentile_1h describe execution quality. Poor execution quality should reduce aggressiveness even if direction looks favorable.
5. oi_change_15m, oi_change_1h, funding_rate_change_1h, and basis_change_1h describe change and participation, not just static state.
6. distance_to_15m_breakout_level, distance_to_1h_breakout_level, and distance_to_invalid_level describe structure quality. Smaller and clearer invalidation distance generally supports cleaner entries.

RULES
1. Analyze structure, participation, trigger context, execution quality, and recent performance.
2. Decide one of HOLD, BUY, SELL, REDUCE, EXIT at the portfolio level.
3. If entering or reducing, populate orders and include take_profit / stop_loss when appropriate.
4. Distinguish opportunity triggers vs risk triggers. Opportunity triggers mean closer monitoring; risk triggers mean more conservative execution.
5. Review existing open orders and avoid duplicate or conflicting orders for the same symbol.
6. If liquidation risk, cooldown pressure, drawdown pressure, wide spread, weak depth, or unfavorable funding is elevated, prefer REDUCE or EXIT over new exposure.
7. In trigger mode, prioritize risk reduction. Avoid new positions unless uniquely supported by long_term_memory.
8. Consider idempotency and avoid repeating a failed request from recent_journal_context.
9. Put identified risks in risk_notes.
10. All text fields must be English.
11. When market_context contains multiple symbols, compare them and only place orders on the strongest justified symbols.
12. Do not force a trade just because an opportunity trigger exists.
13. A breakout without volume, open-interest support, or acceptable execution quality is weak.
14. If price is too extended relative to ATR or too far from a clean invalidation level, reduce confidence or prefer HOLD.
15. rationale must stay concise for humans, but rationale_items must carry the machine-checkable evidence.
16. Every rationale_item must include at least one evidence row with a numeric observed_value and a valid source path from the DecisionRequest.
17. source must point to an existing field such as market_snapshots.BTCUSDT.timeframes.15m.rsi_14, market_snapshots.BTCUSDT.oi_change_15m, or risk_state.cooldown_remaining_minutes.
18. If operator is not note, threshold is required. If operator is note, threshold must be null.
19. Do not use vague words such as strong, weak, likely, maybe unless the same rationale_item contains a numeric comparison.

OUTPUT EXAMPLE
{{
  "decision":"BUY",
  "orders":[{{"symbol":"BTCUSDT","action":"BUY","order_type":"MARKET","quantity":0.01,"price":null,"reduce_only":false,"take_profit":31200.0,"stop_loss":29850.0}}],
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
            "market_context": self._build_market_payload(request),
            "risk_state": request.risk_state.model_dump(mode="json"),
            "risk_mode": request.risk_mode,
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
        model_to_use = self.pro_model if request.is_trigger_mode else self.flash_model
        prompt_hash = self._build_prompt_cache_key(prompt, model_to_use)
        logger.debug(f"Prompt generated for Gemini. Using model: {model_to_use}, input_hash={input_hash}")

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
            response = self.client.models.generate_content(
                model=model_to_use,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TradeIntent,
                    temperature=0.2, # Low temp for consistency
                ),
            )
            raw_text = response.text
            intent = TradeIntent.model_validate_json(raw_text)
            validation_result = self.intent_validator.validate(request, intent)
            if not validation_result.is_valid:
                logger.warning(
                    "Gemini structured rationale validation failed: {}",
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
            usage_metadata = self._extract_usage_metadata(response)
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
                    f"Gemini Decision: {intent.decision} (confidence: {intent.confidence}, "
                    f"tokens={usage_metadata.get('total_token_count', 'unknown')})"
                )
            else:
                logger.info(f"Gemini Decision: {intent.decision} (confidence: {intent.confidence})")
            return DecisionResult(
                intent=intent,
                prompt=prompt,
                raw_response=raw_text,
                model_name=model_to_use,
                input_hash=input_hash,
                cache_hit=False,
                usage_metadata=usage_metadata,
            )
            
        except ValidationError as ve:
            logger.error(f"Gemini output failed validation: {ve}")
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
            logger.error(f"Failed to call Gemini API: {e}")
            raise
