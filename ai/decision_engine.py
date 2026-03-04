from google import genai
from google.genai import types
from pydantic import ValidationError
from dataclasses import dataclass
import json
from loguru import logger
from core.models import DecisionRequest, TradeIntent

@dataclass
class DecisionResult:
    intent: TradeIntent
    prompt: str
    raw_response: str
    model_name: str

class DecisionEngine:
    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)
        self.flash_model = "gemini-3.0-pro" # Upgraded to pro for max performance
        self.pro_model = "gemini-3.0-pro"
        
    def _create_prompt(self, request: DecisionRequest) -> str:
        prompt = f"""
You are an expert crypto trading strategist. Your goal is to analyze the market and account state to output a JSON object describing your intended trading actions.
WARNING: You are ONLY a strategist. Do not try to bypass risk limits. Your output must exactly match the TradeIntent JSON schema.

--- Input Data ---
Policy Summary (Risk Limits and Rules):
{request.policy_summary}

Account State (Truth):
{request.account_state.model_dump_json(indent=2)}

Market Snapshot (Closed Candles & Indicators):
{request.market_snapshot.model_dump_json(indent=2)}

Recent Journal Summaries (Short-term context):
{json.dumps(request.recent_journal_summaries, indent=2)}

Long Term Memory (Lessons & Rules):
{json.dumps(request.long_term_memory, indent=2)}

Similar Past Cases (RAG Context):
{json.dumps(request.similar_past_cases, indent=2)}

TRIGGER MODE ACTIVE: {request.is_trigger_mode}
If TRIGGER MODE ACTIVE is True, you MUST prioritize risk reduction. Avoid opening new positions unless uniquely supported by Long Term Memory.

--- Instructions ---
1. Analyze the trend, ATR, and recent performance.
2. Determine if the current state requires a HOLD, BUY, SELL, REDUCE, or EXIT operation.
3. If entering or reducing, populate the "orders" list. Provide take_profit and stop_loss targets where appropriate.
4. If risk_notes are identified (e.g., volume surge, low ATR, approaching daily loss limit), list them.
5. Provide a short rationale for your decision.
6. Make sure to consider idempotency and avoid repeating a failed request from the recent journal.
7. Return ONLY valid JSON matching the TradeIntent schema. No markdown wrapping.
8. IMPORTANT: All text fields (e.g., rationale, risk_notes) MUST be written in English to save token costs.
"""
        return prompt

    def generate_intent(self, request: DecisionRequest) -> TradeIntent:
        return self.generate_decision(request).intent

    def generate_decision(self, request: DecisionRequest) -> DecisionResult:
        prompt = self._create_prompt(request)
        model_to_use = self.pro_model if request.is_trigger_mode else self.flash_model
        logger.debug(f"Prompt generated for Gemini. Using model: {model_to_use}")
        
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
            logger.info(f"Gemini Decision: {intent.decision} (confidence: {intent.confidence})")
            return DecisionResult(
                intent=intent,
                prompt=prompt,
                raw_response=raw_text,
                model_name=model_to_use,
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
            )
        except Exception as e:
            logger.error(f"Failed to call Gemini API: {e}")
            raise
