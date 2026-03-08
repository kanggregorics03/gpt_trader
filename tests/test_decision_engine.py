import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai.decision_engine import DecisionEngine
from core.models import AccountState, DecisionRequest, MarketSnapshot, RiskStateSnapshot
from db.state_store import StateStore


class TestDecisionEngine(unittest.TestCase):
    def test_candidate_input_hash_ignores_account_snapshot_timestamp(self):
        engine = DecisionEngine.__new__(DecisionEngine)
        base_request = DecisionRequest(
            policy_summary="risk: normal",
            account_state=AccountState(
                snapshot_timestamp=1111111111,
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
            long_term_memory=[],
        )
        changed_request = base_request.model_copy(
            update={
                "account_state": base_request.account_state.model_copy(
                    update={"snapshot_timestamp": 2222222222}
                )
            }
        )

        base_hash = engine._build_candidate_input_hash(base_request)
        changed_hash = engine._build_candidate_input_hash(changed_request)

        self.assertEqual(base_hash, changed_hash)

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

    def test_risk_state_prompt_and_hash_include_dynamic_risk_fields(self):
        engine = DecisionEngine.__new__(DecisionEngine)
        request = DecisionRequest(
            policy_summary="risk: adaptive",
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
            risk_state={
                "daily_start_balance": 1000.0,
                "daily_pnl_pct": -1.2,
                "drawdown_active": False,
                "consecutive_losses": 2,
                "last_loss_estimated_pnl_pct": -0.9,
                "cooldown_minutes_effective": 45,
                "cooldown_components": ["medium_loss", "repeated_failure"],
                "cooldown_active": True,
                "cooldown_remaining_minutes": 20,
                "failure_pattern_score": 0.7,
                "dominant_failure_reason": "late_breakout_failure",
                "dominant_failure_count": 3,
            },
            long_term_memory=[],
        )

        prompt = engine._create_prompt(request)
        input_hash = engine._build_candidate_input_hash(request)
        changed_hash = engine._build_candidate_input_hash(
            request.model_copy(update={"risk_state": request.risk_state.model_copy(update={"failure_pattern_score": 0.2})})
        )

        self.assertIn('"failure_pattern_score":0.7', prompt)
        self.assertIn('"dominant_failure_reason":"late_breakout_failure"', prompt)
        self.assertNotEqual(input_hash, changed_hash)
        self.assertEqual(request.risk_state.cooldown_minutes_effective, 45)
        self.assertEqual(request.risk_state.dominant_failure_count, 3)

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

    def test_generate_decision_cache_hit_when_only_snapshot_timestamp_changes(self):
        tmpdir = tempfile.TemporaryDirectory()
        store = StateStore(str(Path(tmpdir.name) / "state.db"))
        try:
            engine = DecisionEngine.__new__(DecisionEngine)
            engine.client = None
            engine.flash_model = "gemini-3.0-pro"
            engine.pro_model = "gemini-3.0-pro"
            engine.cache_store = store

            base_request = DecisionRequest(
                policy_summary="risk: normal",
                account_state=AccountState(
                    snapshot_timestamp=1111111111,
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
                    "account_state": base_request.account_state.model_copy(
                        update={"snapshot_timestamp": 2222222222}
                    )
                }
            )

            prompt = engine._create_prompt(base_request)
            prompt_hash = engine._build_prompt_cache_key(prompt, "gemini-3.0-pro")
            input_hash = engine._build_candidate_input_hash(base_request)
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

            result = engine.generate_decision(changed_request)

            self.assertTrue(result.cache_hit)
            self.assertEqual(result.intent.decision, "HOLD")
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

    def test_long_term_memory_is_included_in_candidate_hash(self):
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
            long_term_memory=[
                {
                    "rule": "Avoid low-quality breakout chases.",
                    "category": "anti_pattern",
                    "status": "rule",
                    "support": 4,
                    "regime": "high_volatility",
                }
            ],
        )
        changed_request = base_request.model_copy(
            update={
                "long_term_memory": [
                    {
                        "rule": "Favor confirmed breakout retests.",
                        "category": "opportunity",
                        "status": "rule",
                        "support": 5,
                        "regime": "high_volatility",
                    }
                ]
            }
        )

        self.assertNotEqual(
            engine._build_candidate_input_hash(base_request),
            engine._build_candidate_input_hash(changed_request),
        )

    def test_resolve_models_uses_explicit_models_first(self):
        engine = DecisionEngine.__new__(DecisionEngine)
        engine.provider_name = "gemini"

        default_model, trigger_model = engine._resolve_models(
            default_model="gemini-2.5-flash",
            trigger_model="gemini-2.5-pro",
        )

        self.assertEqual(default_model, "gemini-2.5-flash")
        self.assertEqual(trigger_model, "gemini-2.5-pro")

    def test_resolve_models_uses_environment_and_legacy_fallback(self):
        engine = DecisionEngine.__new__(DecisionEngine)
        engine.provider_name = "gemini"

        with patch.dict(
            "os.environ",
            {
                "AI_DEFAULT_MODEL": "",
                "AI_TRIGGER_MODEL": "",
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
                "AI_DEFAULT_MODEL": "",
                "AI_TRIGGER_MODEL": "",
                "GEMINI_DEFAULT_MODEL": "",
                "GEMINI_TRIGGER_MODEL": "",
                "GEMINI_MODEL": "gemini-legacy-model",
            },
            clear=False,
        ):
            default_model, trigger_model = engine._resolve_models()
            self.assertEqual(default_model, "gemini-legacy-model")
            self.assertEqual(trigger_model, "gemini-legacy-model")

    def test_resolve_models_supports_openai_style_envs(self):
        engine = DecisionEngine.__new__(DecisionEngine)
        engine.provider_name = "openai"

        with patch.dict(
            "os.environ",
            {
                "AI_DEFAULT_MODEL": "",
                "AI_TRIGGER_MODEL": "",
                "OPENAI_DEFAULT_MODEL": "gpt-4.1-mini",
                "OPENAI_TRIGGER_MODEL": "gpt-4.1",
            },
            clear=False,
        ):
            default_model, trigger_model = engine._resolve_models()
            self.assertEqual(default_model, "gpt-4.1-mini")
            self.assertEqual(trigger_model, "gpt-4.1")

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

    def test_prompt_describes_derived_market_fields_and_trigger_state(self):
        engine = DecisionEngine.__new__(DecisionEngine)
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
                recent_volume_surge=True,
                volume_ratio_15m=2.1,
                volume_surge_15m_now=True,
                volume_surge_trigger_active=True,
                oi_change_15m=3.4,
                spread_percentile_1h=42.0,
                distance_to_invalid_level=0.9,
            ),
            risk_state=RiskStateSnapshot(
                daily_start_balance=1000.0,
                daily_pnl_pct=0.0,
                drawdown_active=False,
                consecutive_losses=0,
                cooldown_active=False,
            ),
            long_term_memory=[],
        )

        prompt = engine._create_prompt(request)

        self.assertIn("Prefer derived fields such as change, percentile, distance, and trigger-state", prompt)
        self.assertIn("volume_surge_15m_now is the immediate current-bar condition", prompt)
        self.assertIn('"volume_surge_15m_now":true', prompt)
        self.assertIn('"spread_percentile_1h":42.0', prompt)
        self.assertIn("market_snapshots.BTCUSDT.oi_change_15m", prompt)

    def test_prompt_uses_compact_market_and_memory_payloads(self):
        engine = DecisionEngine.__new__(DecisionEngine)
        request = DecisionRequest(
            policy_summary="risk: trigger",
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
                recent_volume_surge=True,
                volume_ratio_15m=2.1,
                volume_surge_15m_now=True,
                fast_move_trigger_active=True,
                volume_surge_trigger_active=True,
                range_break_trigger_active=True,
                oi_change_15m=3.4,
                spread_percentile_1h=42.0,
                distance_to_invalid_level=0.9,
                timeframes={
                    "15m": {
                        "timeframe": "15m",
                        "last_close": 30000.0,
                        "atr_pct": 0.4,
                        "rsi_14": 63.0,
                        "change_1bar_pct": 0.8,
                        "volume_ratio": 2.1,
                        "breakout_up": True,
                        "previous_values": {"rsi_14": 58.0, "volume_ratio": 1.2},
                    }
                },
                order_book={
                    "best_bid": 29999.5,
                    "best_ask": 30000.5,
                    "spread_bps": 0.33,
                    "mid_price": 30000.0,
                    "imbalance_top5": 0.54,
                    "previous_values": {"spread_bps": 0.45},
                },
                derivatives={
                    "basis_pct": 0.03,
                    "funding_rate": 0.0001,
                    "open_interest": 12345.0,
                    "previous_values": {"open_interest": 12000.0},
                },
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
                    "decision": "BUY",
                    "summary": "Bought BTC breakout retest.",
                    "rationale": ["Bought breakout retest with rising volume."],
                    "risk_reason": "Approved after review.",
                    "entry_size_cap": {"adjusted": True, "base_cap_pct": 30.0, "effective_cap_pct": 20.0},
                    "evidence_quality": {"factor": 0.85, "issues": ["low_signal_diversity", "thin_depth"]},
                }
            ],
            long_term_memory=[
                {
                    "rule": "Avoid low-quality breakout chases when execution quality deteriorates.",
                    "category": "anti_pattern",
                    "status": "rule",
                    "support": 4,
                    "regime": "high_volatility",
                    "apply_when": {"regime": ["high_volatility"], "volatility": ["high"], "trend": ["up"]},
                    "ignore_when": ["Ignore when retest holds with strong follow-through.", "Ignore when trend is flat."],
                    "metrics": {
                        "confidence_score": 0.81,
                        "validity_score": 0.77,
                        "freshness_score": 0.92,
                        "lifecycle_status": "active",
                        "candidate_lessons": [{"rule": "verbose"}],
                    },
                }
            ],
            similar_past_cases=[
                "BTCUSDT: Similarity 0.88; status=closed; pnl=-1.4; slippage_bps=7.2; failure_reason=execution_quality_degraded"
            ],
        )

        prompt = engine._create_prompt(request)

        self.assertIn('"confidence_score":0.81', prompt)
        self.assertIn('"entry_size_cap":{"adjusted":true,"effective_cap_pct":20.0,"base_cap_pct":30.0}', prompt)
        self.assertNotIn("previous_values", prompt)
        self.assertNotIn("candidate_lessons", prompt)
        self.assertNotIn("long detail", prompt)


if __name__ == "__main__":
    unittest.main()
