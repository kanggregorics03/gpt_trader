import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai.decision_engine import DecisionEngine
from core.models import AccountState, DecisionRequest, MarketSnapshot, RiskStateSnapshot
from db.state_store import StateStore


class TestDecisionEngine(unittest.TestCase):
    def test_candidate_input_hash_changes_with_risk_state(self):
        engine = DecisionEngine.__new__(DecisionEngine)
        base_request = DecisionRequest(
            policy_summary="risk: normal",
            account_state=AccountState(
                total_balance=1000.0,
                available_balance=900.0,
            ),
            market_snapshot=MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=120.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
            ),
            risk_state=RiskStateSnapshot(
                daily_start_balance=1000.0,
                daily_pnl_pct=-0.5,
                drawdown_active=False,
                consecutive_losses=1,
                cooldown_active=False,
            ),
            long_term_memory=[],
        )
        changed_request = base_request.model_copy(
            update={
                "risk_state": RiskStateSnapshot(
                    daily_start_balance=1000.0,
                    daily_pnl_pct=-2.5,
                    drawdown_active=True,
                    drawdown_activate_pct=1.5,
                    consecutive_losses=3,
                    cooldown_active=True,
                    cooldown_remaining_minutes=12,
                )
            }
        )

        base_hash = engine._build_candidate_input_hash(base_request)
        changed_hash = engine._build_candidate_input_hash(changed_request)

        self.assertNotEqual(base_hash, changed_hash)

    def test_generate_decision_uses_exact_match_cache(self):
        tmpdir = tempfile.TemporaryDirectory()
        store = StateStore(str(Path(tmpdir.name) / "state.db"))
        try:
            engine = DecisionEngine.__new__(DecisionEngine)
            engine.client = None
            engine.flash_model = "gemini-3.0-pro"
            engine.pro_model = "gemini-3.0-pro"
            engine.cache_store = store

            request = DecisionRequest(
                policy_summary="risk: normal",
                account_state=AccountState(
                    total_balance=1000.0,
                    available_balance=900.0,
                ),
                market_snapshot=MarketSnapshot(
                    symbol="BTCUSDT",
                    current_price=30000.0,
                    atr_15m=120.0,
                    atr_1h=240.0,
                    trend_1h="UP",
                    recent_volume_surge=False,
                ),
                risk_state=RiskStateSnapshot(
                    daily_start_balance=1000.0,
                    daily_pnl_pct=-0.5,
                    drawdown_active=False,
                    consecutive_losses=1,
                    cooldown_active=False,
                ),
                long_term_memory=[],
            )

            prompt = engine._create_prompt(request)
            prompt_hash = engine._build_prompt_cache_key(prompt, "gemini-3.0-pro")
            input_hash = engine._build_candidate_input_hash(request)
            store.save_cached_decision(
                prompt_hash=prompt_hash,
                model_name="gemini-3.0-pro",
                input_hash=input_hash,
                raw_response='{"decision":"HOLD","orders":[],"rationale":["Cached"],"risk_notes":[],"confidence":0.4}',
                intent={
                    "decision": "HOLD",
                    "orders": [],
                    "rationale": ["Cached"],
                    "risk_notes": [],
                    "confidence": 0.4,
                },
            )

            result = engine.generate_decision(request)

            self.assertTrue(result.cache_hit)
            self.assertEqual(result.intent.decision, "HOLD")
            self.assertEqual(result.raw_response, '{"decision":"HOLD","orders":[],"rationale":["Cached"],"risk_notes":[],"confidence":0.4}')
        finally:
            store.close()
            tmpdir.cleanup()

    def test_market_snapshots_are_included_in_candidate_hash(self):
        engine = DecisionEngine.__new__(DecisionEngine)
        base_request = DecisionRequest(
            policy_summary="risk: normal",
            account_state=AccountState(
                total_balance=1000.0,
                available_balance=900.0,
            ),
            market_snapshots={
                "BTCUSDT": MarketSnapshot(
                    symbol="BTCUSDT",
                    current_price=30000.0,
                    atr_15m=120.0,
                    atr_1h=240.0,
                    trend_1h="UP",
                    recent_volume_surge=False,
                ),
                "ETHUSDT": MarketSnapshot(
                    symbol="ETHUSDT",
                    current_price=2000.0,
                    atr_15m=20.0,
                    atr_1h=45.0,
                    trend_1h="FLAT",
                    recent_volume_surge=False,
                ),
            },
            risk_state=RiskStateSnapshot(
                daily_start_balance=1000.0,
                daily_pnl_pct=0.0,
                drawdown_active=False,
                consecutive_losses=0,
                cooldown_active=False,
            ),
            long_term_memory=[],
        )
        changed_request = base_request.model_copy(
            update={
                "market_snapshots": {
                    **base_request.market_snapshots,
                    "ETHUSDT": MarketSnapshot(
                        symbol="ETHUSDT",
                        current_price=2100.0,
                        atr_15m=25.0,
                        atr_1h=55.0,
                        trend_1h="UP",
                        recent_volume_surge=True,
                    ),
                }
            }
        )

        base_hash = engine._build_candidate_input_hash(base_request)
        changed_hash = engine._build_candidate_input_hash(changed_request)

        self.assertNotEqual(base_hash, changed_hash)

    def test_recent_journal_context_is_included_in_candidate_hash(self):
        engine = DecisionEngine.__new__(DecisionEngine)
        base_request = DecisionRequest(
            policy_summary="risk: normal",
            account_state=AccountState(
                total_balance=1000.0,
                available_balance=900.0,
            ),
            market_snapshot=MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=120.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
            ),
            risk_state=RiskStateSnapshot(
                daily_start_balance=1000.0,
                daily_pnl_pct=0.0,
                drawdown_active=False,
                consecutive_losses=0,
                cooldown_active=False,
            ),
            recent_journal_context=[
                {
                    "journal_id": 1,
                    "decision": "HOLD",
                    "rationale": ["Waiting"],
                    "risk_reason": "Safe action.",
                    "summary": "HOLD: Waiting",
                }
            ],
            long_term_memory=[],
        )
        changed_request = base_request.model_copy(
            update={
                "recent_journal_context": [
                    {
                        "journal_id": 2,
                        "decision": "BUY",
                        "rationale": ["Momentum"],
                        "risk_reason": "Approved",
                        "summary": "BUY: Momentum",
                    }
                ]
            }
        )

        base_hash = engine._build_candidate_input_hash(base_request)
        changed_hash = engine._build_candidate_input_hash(changed_request)

        self.assertNotEqual(base_hash, changed_hash)

    def test_resolve_models_uses_explicit_models_first(self):
        engine = DecisionEngine.__new__(DecisionEngine)

        default_model, trigger_model = engine._resolve_models(
            default_model="gemini-2.5-flash",
            trigger_model="gemini-2.5-pro",
        )

        self.assertEqual(default_model, "gemini-2.5-flash")
        self.assertEqual(trigger_model, "gemini-2.5-pro")

    def test_resolve_models_uses_environment_and_legacy_fallback(self):
        engine = DecisionEngine.__new__(DecisionEngine)

        with patch.dict(
            "os.environ",
            {
                "GEMINI_DEFAULT_MODEL": "gemini-2.5-flash",
                "GEMINI_TRIGGER_MODEL": "gemini-2.5-pro",
                "GEMINI_MODEL": "gemini-legacy-model",
            },
            clear=False,
        ):
            default_model, trigger_model = engine._resolve_models()
            self.assertEqual(default_model, "gemini-2.5-flash")
            self.assertEqual(trigger_model, "gemini-2.5-pro")

        with patch.dict(
            "os.environ",
            {
                "GEMINI_DEFAULT_MODEL": "",
                "GEMINI_TRIGGER_MODEL": "",
                "GEMINI_MODEL": "gemini-legacy-model",
            },
            clear=False,
        ):
            default_model, trigger_model = engine._resolve_models()
            self.assertEqual(default_model, "gemini-legacy-model")
            self.assertEqual(trigger_model, "gemini-legacy-model")

    def test_extract_usage_metadata_from_response_object(self):
        class Usage:
            prompt_token_count = 123
            candidates_token_count = 45
            total_token_count = 168

        class Response:
            usage_metadata = Usage()

        engine = DecisionEngine.__new__(DecisionEngine)

        usage = engine._extract_usage_metadata(Response())

        self.assertEqual(usage["prompt_token_count"], 123)
        self.assertEqual(usage["candidates_token_count"], 45)
        self.assertEqual(usage["total_token_count"], 168)


if __name__ == "__main__":
    unittest.main()
