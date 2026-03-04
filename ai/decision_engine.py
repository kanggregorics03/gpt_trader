from dataclasses import dataclass
import hashlib
import json
import os
from typing import Optional

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

    def _build_market_payload(self, request: DecisionRequest):
        if request.market_snapshots:
            return {
                symbol: snapshot.model_dump(mode="json")
                for symbol, snapshot in request.market_snapshots.items()
            }
        if request.market_snapshot is not None:
            return request.market_snapshot.model_dump(mode="json")
        return {}

    def _build_recent_journal_context_payload(self, request: DecisionRequest):
        payload = []
        for item in request.recent_journal_context:
            if hasattr(item, "model_dump"):
                payload.append(item.model_dump(mode="json"))
            else:
                payload.append(dict(item))
        return payload
        
    def _create_prompt(self, request: DecisionRequest) -> str:
        memory_payload = [
            item.model_dump() if isinstance(item, LongTermMemoryRecord) else item
            for item in request.long_term_memory
        ]
        market_payload = self._build_market_payload(request)
        prompt = f"""
You are an expert crypto trading strategist.
You are ONLY a strategist. Do not bypass risk limits.
Return ONLY valid JSON matching the TradeIntent schema.

INPUTS
policy_summary:
{request.policy_summary}
account_state:
{request.account_state.model_dump_json()}
market_context:
{self._compact_json(market_payload)}
risk_state:
{request.risk_state.model_dump_json()}
risk_mode:
{request.risk_mode}
risk_profile:
{self._compact_json(request.risk_profile)}
recent_journal_context:
{self._compact_json(self._build_recent_journal_context_payload(request))}
long_term_memory:
{self._compact_json(memory_payload)}
similar_past_cases:
{self._compact_json(request.similar_past_cases)}
trigger_mode_active:
{str(request.is_trigger_mode).lower()}

RULES
1. Analyze trend, ATR, trigger context, and recent performance.
2. Decide one of HOLD, BUY, SELL, REDUCE, EXIT at the portfolio level.
3. If entering or reducing, populate orders and include take_profit / stop_loss when appropriate.
4. Distinguish opportunity triggers vs risk triggers. Opportunity triggers mean closer monitoring; risk triggers mean more conservative execution.
5. Review existing open orders and avoid duplicate or conflicting orders for the same symbol.
6. If liquidation risk, cooldown pressure, drawdown pressure, wide spread, or unfavorable funding is elevated, prefer REDUCE or EXIT over new exposure.
7. In trigger mode, prioritize risk reduction. Avoid new positions unless uniquely supported by long_term_memory.
8. Consider idempotency and avoid repeating a failed request from recent_journal_context.
9. Put identified risks in risk_notes.
10. All text fields must be English.
11. When market_context contains multiple symbols, compare them and only place orders on the strongest justified symbols.
12. rationale must stay concise for humans, but rationale_items must carry the machine-checkable evidence.
13. Every rationale_item must include at least one evidence row with a numeric observed_value and a valid source path from the DecisionRequest.
14. source must point to an existing field such as market_snapshots.BTCUSDT.timeframes.15m.rsi_14 or risk_state.cooldown_remaining_minutes.
15. If operator is not note, threshold is required. If operator is note, threshold must be null.
16. Do not use vague words such as strong, weak, likely, maybe unless the same rationale_item contains a numeric comparison.

OUTPUT EXAMPLE
{{
  "decision":"BUY",
  "orders":[{{"symbol":"BTCUSDT","action":"BUY","order_type":"MARKET","quantity":0.01,"price":null,"reduce_only":false,"take_profit":31200.0,"stop_loss":29850.0}}],
  "rationale":["BTC 15m momentum and volume confirm continuation above the hourly range."],
  "rationale_items":[{{"summary":"BTC momentum confirmation on 15m breakout","why_it_matters":"Momentum and participation are aligned with continuation risk/reward.","trade_implication":"Allows one small long entry with defined exit levels.","failure_mode":"Invalidate if the breakout loses volume or falls back inside the 1h range.","evidence":[{{"signal":"volume_ratio","symbol":"BTCUSDT","timeframe":"15m","operator":"gte","observed_value":2.4,"threshold":1.8,"units":"ratio","source":"market_snapshots.BTCUSDT.timeframes.15m.volume_ratio","confidence_contribution":0.3}},{{"signal":"last_close","symbol":"BTCUSDT","timeframe":"1h","operator":"cross_up","observed_value":30120.0,"threshold":30000.0,"units":"usdt","source":"market_snapshots.BTCUSDT.timeframes.1h.last_close","confidence_contribution":0.2}}]}}],
  "risk_notes":["Wide spread or negative follow-through would invalidate the setup."],
  "confidence":0.62
}}
"""
        return prompt

    def _build_candidate_input_hash(self, request: DecisionRequest) -> str:
        payload = {
            "policy_summary": request.policy_summary,
            "account_state": request.account_state.model_dump(mode="json"),
            "market_context": self._build_market_payload(request),
            "risk_state": request.risk_state.model_dump(mode="json"),
            "risk_mode": request.risk_mode,
            "risk_profile": request.risk_profile,
            "recent_journal_context": self._build_recent_journal_context_payload(request),
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
