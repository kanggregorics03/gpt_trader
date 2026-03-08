import unittest
import time
from unittest.mock import patch

from main import TradingOrchestrator
from core.alpha_policy import AlphaPolicyEngine
from core.execution_policy import ExecutionPolicyEngine
from core.models import AccountState, MarketSnapshot, OpenOrderState, OrderIntent, PositionState, RiskStateSnapshot, TradeIntent
from core.position_policy import PositionPolicyEngine
from core.safety_policy import SafetyPolicyEngine


class DummyRiskEngine:
    policy = {
        "intervals": {
            "strategy_loop_minutes": 60,
            "position_loop_minutes": 15,
            "trigger_loop_minutes": 5,
        },
        "adaptive_loop": {
            "trigger_clear_cycles": 2,
            "position_min_duration_minutes": 30,
            "flat_clear_cycles": 4,
            "recent_execution_window_minutes": 20,
            "recent_close_window_minutes": 20,
            "poll_seconds": 1,
        },
        "risk_limits": {
            "daily_max_loss_pct": 3.0,
            "max_consecutive_losses": 3,
        },
        "execution_guards": {
            "loop_lock_ttl_seconds": 90,
            "decision_cooldown_minutes": 7,
            "entry_mutex_ttl_seconds": 120,
            "unknown_order_block_minutes": 25,
            "recovery_sweep_minutes": 3,
        },
        "context_budget": {
            "journal_fetch_limit": 20,
            "journal_keep_limit": 4,
            "memory_keep_limit": 5,
            "similar_case_keep_limit": 3,
            "max_rationale_items_per_journal": 1,
            "max_evidence_per_rationale_item": 1,
            "max_ignore_when_per_memory": 1,
        },
        "market_data_cache": {
            "ticker_ttl_ms": 7000,
            "order_book_ttl_ms": 8000,
            "funding_ttl_ms": 180000,
            "open_interest_ttl_ms": 45000,
        },
    }


class TestTradingOrchestrator(unittest.TestCase):
    class DummyRagEngine:
        def build_market_vectors(self, market_snapshots, account_state=None, risk_state=None):
            vectors = {}
            for symbol, snapshot in (market_snapshots or {}).items():
                vectors[str(symbol)] = [
                    float(snapshot.get("current_price") or 0.0),
                    float(snapshot.get("atr_15m") or 0.0),
                    float(snapshot.get("atr_1h") or 0.0),
                ]
            return vectors

        def _cosine_similarity(self, v1, v2):
            if not v1 or not v2:
                return 0.0
            length = max(len(v1), len(v2))
            vv1 = list(v1) + [0.0] * (length - len(v1))
            vv2 = list(v2) + [0.0] * (length - len(v2))
            dot = sum(a * b for a, b in zip(vv1, vv2))
            mag1 = sum(a * a for a in vv1) ** 0.5
            mag2 = sum(a * a for a in vv2) ** 0.5
            if mag1 == 0 or mag2 == 0:
                return 0.0
            return dot / mag1 / mag2

    class DummyCaseStore:
        def __init__(self):
            self.active_cases = {}
            self.trade_cases = {}
            self.recent_closed_cases = {}
            self.trade_case_snapshots = []
            self.execution_fills_by_audit = {}
            self.retrieval_evaluations = []
            self.execution_policy_evaluations = []
            self.execution_policy_profiles = {}
            self.policy_registry = {}
            self.candidate_policy_versions = []
            self.shadow_decisions = []
            self.policy_bundles = {}
            self.bundle_promotions = []

        def get_active_trade_case(self, symbol):
            return dict(self.active_cases.get(symbol, {}))

        def get_all_active_trade_cases(self):
            return [dict(row) for row in self.active_cases.values()]

        def upsert_active_trade_case(
            self,
            symbol,
            trade_case_id,
            opened_at,
            entry_decision=None,
            thesis_id=None,
            management_template_id=None,
            last_decision=None,
            last_audit_log_id=None,
            payload=None,
        ):
            self.active_cases[symbol] = {
                "symbol": symbol,
                "trade_case_id": trade_case_id,
                "opened_at": opened_at,
                "entry_decision": entry_decision,
                "thesis_id": thesis_id,
                "management_template_id": management_template_id,
                "last_decision": last_decision,
                "last_audit_log_id": last_audit_log_id,
                "payload": dict(payload or {}),
            }

        def close_active_trade_case(
            self,
            symbol,
            closed_at=None,
            last_decision=None,
            last_audit_log_id=None,
        ):
            existing = dict(self.active_cases.pop(symbol, {}))
            if existing:
                payload = dict(existing.get("payload") or {})
                if closed_at:
                    payload["closed_at"] = closed_at
                existing["payload"] = payload
            return existing

        def get_trade_case(self, trade_case_id):
            return dict(self.trade_cases.get(trade_case_id, {}))

        def get_recent_closed_trade_case_for_symbol(
            self,
            symbol,
            close_cooldown_minutes=0,
            loss_cooldown_minutes=0,
            loss_threshold_pct=0.0,
            require_no_active_case=False,
        ):
            if require_no_active_case and self.get_active_trade_case(symbol):
                return {}
            return dict(self.recent_closed_cases.get(symbol, {}))

        def upsert_trade_case(self, record):
            trade_case_id = record.get("trade_case_id")
            if not trade_case_id:
                return
            self.trade_cases[trade_case_id] = dict(record)

        def save_trade_case_snapshots(self, entries):
            self.trade_case_snapshots.extend(dict(entry) for entry in (entries or []))

        def get_trade_case_snapshots(self, trade_case_id):
            return [
                dict(entry)
                for entry in self.trade_case_snapshots
                if entry.get("trade_case_id") == trade_case_id
            ]

        def get_trade_case_snapshots_between(self, trade_case_id, start_timestamp=None, end_timestamp=None, limit=None):
            rows = self.get_trade_case_snapshots(trade_case_id)
            if start_timestamp:
                rows = [row for row in rows if str(row.get("timestamp") or "") >= str(start_timestamp)]
            if end_timestamp:
                rows = [row for row in rows if str(row.get("timestamp") or "") <= str(end_timestamp)]
            if limit is not None:
                rows = rows[:limit]
            return rows

        def save_execution_fills(self, audit_log_id, fills):
            self.execution_fills_by_audit[audit_log_id] = [dict(fill) for fill in (fills or [])]

        def get_execution_fills(self, audit_log_id):
            rows = []
            for fill in self.execution_fills_by_audit.get(audit_log_id, []):
                item = dict(fill)
                if "fill_timestamp" not in item and "timestamp" in item:
                    item["fill_timestamp"] = item.get("timestamp")
                rows.append(item)
            return rows

        def save_retrieval_evaluations(self, audit_log_id, entries):
            for entry in (entries or []):
                self.retrieval_evaluations.append(
                    {
                        "audit_log_id": audit_log_id,
                        **dict(entry),
                    }
                )

        def save_execution_policy_evaluations(self, audit_log_id, entries):
            for entry in (entries or []):
                self.execution_policy_evaluations.append(
                    {
                        "audit_log_id": audit_log_id,
                        **dict(entry),
                    }
                )

        def get_execution_policy_profile(
            self,
            symbol,
            loop_mode,
            action_type,
            urgency_bucket,
            liquidity_bucket,
            risk_mode,
        ):
            key = (symbol, loop_mode, action_type, urgency_bucket, liquidity_bucket, risk_mode)
            return dict(self.execution_policy_profiles.get(key, {}))

        def upsert_policy_registry(self, record):
            policy_key = str(record.get("policy_key") or "")
            if policy_key:
                self.policy_registry[policy_key] = dict(record)

        def get_policy_registry_entry(self, policy_key):
            return dict(self.policy_registry.get(policy_key, {}))

        def get_policy_registry_entry_for_regime(self, domain, regime_key=None):
            regime_text = str(regime_key or "")
            scoped_key = f"champion:{domain}:{regime_text}" if regime_text else f"champion:{domain}"
            return dict(self.policy_registry.get(scoped_key, self.policy_registry.get(f"champion:{domain}", {})))

        def get_candidate_policy_versions(self, domain=None, statuses=None, limit=50):
            rows = [dict(row) for row in self.candidate_policy_versions]
            if domain:
                rows = [row for row in rows if row.get("domain") == domain]
            clean_statuses = set(statuses or [])
            if clean_statuses:
                rows = [row for row in rows if row.get("status") in clean_statuses]
            return rows[:limit]

        def save_shadow_decisions(self, entries):
            self.shadow_decisions.extend(dict(entry) for entry in (entries or []))

        def upsert_policy_bundle(self, record):
            bundle_key = str(record.get("bundle_key") or "")
            if bundle_key:
                self.policy_bundles[bundle_key] = dict(record)

        def get_active_policy_bundle(self, regime_key=None):
            regime_text = str(regime_key or "")
            for bundle in self.policy_bundles.values():
                if str(bundle.get("status") or "") != "active":
                    continue
                if str(bundle.get("regime_key") or "") == regime_text:
                    return dict(bundle)
            return {}

        def get_policy_bundle(self, bundle_key):
            return dict(self.policy_bundles.get(bundle_key, {}))

        def get_policy_bundles(self, regime_key=None, statuses=None, limit=50):
            rows = [dict(row) for row in self.policy_bundles.values()]
            if regime_key is not None:
                rows = [row for row in rows if str(row.get("regime_key") or "") == str(regime_key or "")]
            clean_statuses = set(statuses or [])
            if clean_statuses:
                rows = [row for row in rows if row.get("status") in clean_statuses]
            return rows[:limit]

        def save_bundle_promotion(self, record):
            self.bundle_promotions.append(dict(record))

        def get_recent_bundle_promotions(self, regime_key=None, limit=20):
            rows = [dict(row) for row in self.bundle_promotions]
            if regime_key is not None:
                rows = [row for row in rows if str(row.get("regime_key") or "") == str(regime_key or "")]
            return rows[:limit]

        def get_trade_case_vector_candidates(self, limit=200, symbols=None, statuses=None, since=None):
            symbols = set(symbols or [])
            rows = []
            for trade_case in self.trade_cases.values():
                if symbols and trade_case.get("symbol") not in symbols:
                    continue
                rows.append(dict(trade_case))
            rows.sort(key=lambda row: str(row.get("trade_case_id") or ""), reverse=True)
            return rows[:limit]

        def get_trade_case_vector_band_candidates(self, current_feature_vectors, limit=200, statuses=None, since=None):
            symbols = set((current_feature_vectors or {}).keys())
            rows = []
            for trade_case in self.trade_cases.values():
                if symbols and trade_case.get("symbol") not in symbols:
                    continue
                item = dict(trade_case)
                item["matched_band_count"] = 4
                rows.append(item)
            rows.sort(key=lambda row: str(row.get("trade_case_id") or ""), reverse=True)
            return rows[:limit]

    class DummyAlertManager:
        def __init__(self):
            self.calls = []

        def send(self, title, message, level="warning"):
            self.calls.append(
                {
                    "title": title,
                    "message": message,
                    "level": level,
                }
            )

    class DummyDiagnosticsFeed:
        def __init__(self, diagnostics):
            self.diagnostics = dict(diagnostics)

        def get_diagnostics(self):
            return dict(self.diagnostics)

    def _make_orchestrator(self):
        orchestrator = TradingOrchestrator.__new__(TradingOrchestrator)
        orchestrator.risk_engine = DummyRiskEngine()
        orchestrator.rag_engine = self.DummyRagEngine()
        orchestrator.symbols = ["BTCUSDT"]
        orchestrator.state_store = self.DummyCaseStore()
        orchestrator.alert_manager = self.DummyAlertManager()
        orchestrator.public_feed = None
        orchestrator.private_account_feed = None
        orchestrator.execution_policy_engine = ExecutionPolicyEngine(policy=orchestrator.risk_engine.policy, state_store=orchestrator.state_store)
        orchestrator.alpha_policy_engine = AlphaPolicyEngine(policy=orchestrator.risk_engine.policy, state_store=orchestrator.state_store)
        orchestrator.position_policy_engine = PositionPolicyEngine(policy=orchestrator.risk_engine.policy, state_store=orchestrator.state_store)
        orchestrator.safety_policy_engine = SafetyPolicyEngine(policy=orchestrator.risk_engine.policy, state_store=orchestrator.state_store)
        orchestrator._initialize_runtime_state()
        return orchestrator

    def test_build_execution_guards_reads_policy_overrides(self):
        orchestrator = self._make_orchestrator()

        guards = orchestrator._build_execution_guards()

        self.assertEqual(guards["loop_lock_ttl_seconds"], 90)
        self.assertEqual(guards["decision_cooldown_minutes"], 7)
        self.assertEqual(guards["entry_mutex_ttl_seconds"], 120)
        self.assertEqual(guards["unknown_order_block_minutes"], 25)
        self.assertEqual(guards["recovery_sweep_minutes"], 3)

    def test_build_context_budget_reads_policy_overrides(self):
        orchestrator = self._make_orchestrator()

        budget = orchestrator._build_context_budget()

        self.assertEqual(budget["journal_fetch_limit"], 20)
        self.assertEqual(budget["journal_keep_limit"], 4)
        self.assertEqual(budget["memory_keep_limit"], 5)
        self.assertEqual(budget["similar_case_keep_limit"], 3)
        self.assertEqual(budget["max_rationale_items_per_journal"], 1)
        self.assertEqual(budget["max_evidence_per_rationale_item"], 1)
        self.assertEqual(budget["max_ignore_when_per_memory"], 1)

    def test_build_market_data_cache_config_maps_policy_keys_to_collector_keys(self):
        orchestrator = self._make_orchestrator()

        cache_config = orchestrator._build_market_data_cache_config()

        self.assertEqual(
            cache_config,
            {
                "ticker": 7000,
                "order_book": 8000,
                "funding_rate": 180000,
                "open_interest": 45000,
            },
        )

    def test_serialize_json_payload_converts_pydantic_models(self):
        orchestrator = self._make_orchestrator()

        payload = {
            "risk_state": RiskStateSnapshot(
                daily_start_balance=1000.0,
                daily_pnl_pct=0.0,
                drawdown_active=False,
                consecutive_losses=0,
                cooldown_active=False,
            ),
            "post_account_state": AccountState(
                total_balance=1000.0,
                available_balance=900.0,
                positions=[],
                open_orders=[],
                open_orders_count=0,
            ),
        }

        serialized = orchestrator._serialize_json_payload(payload)

        self.assertIsInstance(serialized["risk_state"], dict)
        self.assertIsInstance(serialized["post_account_state"], dict)
        self.assertEqual(serialized["risk_state"]["daily_pnl_pct"], 0.0)
        self.assertEqual(serialized["post_account_state"]["available_balance"], 900.0)

    def test_validate_ai_configuration_uses_gemini_default_and_requires_provider_key(self):
        orchestrator = TradingOrchestrator.__new__(TradingOrchestrator)
        orchestrator.ai_provider = "gemini"
        orchestrator.ai_api_key = None
        orchestrator.ai_default_model = "gemini-2.5-flash"
        orchestrator.ai_trigger_model = "gemini-2.5-pro"

        with patch.dict("os.environ", {"GEMINI_API_KEY": "gemini-key"}, clear=False):
            orchestrator._validate_ai_configuration()

        with patch.dict("os.environ", {"GEMINI_API_KEY": "", "AI_API_KEY": ""}, clear=False):
            with self.assertRaises(ValueError):
                orchestrator._validate_ai_configuration()

    def test_select_recent_journal_context_prioritizes_relevant_rows_and_compacts(self):
        orchestrator = self._make_orchestrator()
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=120.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
            )
        }
        rows = [
            {
                "journal_id": 1,
                "decision": "HOLD",
                "rationale": ["Waiting"],
                "rationale_items": [],
                "risk_reason": "Safe action.",
                "risk_mode": "normal",
                "loss_signal": False,
                "trigger_symbols": [],
                "risk_trigger_symbols": [],
                "summary": "HOLD: Waiting",
            },
            {
                "journal_id": 2,
                "decision": "BUY",
                "rationale": ["Momentum long"],
                "rationale_items": [
                    {
                        "summary": "BTC trigger aligned",
                        "why_it_matters": "Momentum+volume",
                        "trade_implication": "Enter long",
                        "failure_mode": "Breakdown",
                        "evidence": [
                            {
                                "signal": "volume_ratio",
                                "symbol": "BTCUSDT",
                                "timeframe": "15m",
                                "operator": "gte",
                                "observed_value": 2.1,
                                "threshold": 1.8,
                                "units": "ratio",
                                "source": "market_snapshots.BTCUSDT.timeframes.15m.volume_ratio",
                            },
                            {
                                "signal": "rsi_14",
                                "symbol": "BTCUSDT",
                                "timeframe": "15m",
                                "operator": "gte",
                                "observed_value": 58.0,
                                "threshold": 50.0,
                                "units": "index",
                                "source": "market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                            },
                        ],
                    }
                ],
                "risk_reason": "Approved",
                "risk_mode": "trigger",
                "loss_signal": True,
                "estimated_pnl_pct": -0.7,
                "trigger_symbols": ["BTCUSDT"],
                "risk_trigger_symbols": [],
                "summary": "BUY: Momentum long",
                "trade_case_ids": ["BTCUSDT-case-2"],
            },
        ]
        trade_cases = [
            {
                "trade_case_id": "BTCUSDT-case-2",
                "symbol": "BTCUSDT",
                "risk_mode": "trigger",
                "status": "closed",
                "estimated_pnl_pct": -0.7,
                "feature_vector": [30000.0, 120.0, 240.0],
                "trigger_reasons": ["fast_price_move_15m"],
                "risk_trigger_reasons": [],
                "payload": {"regime_tags": ["trend:aligned_up", "volatility:unknown"]},
            }
        ]
        orchestrator.state_store.upsert_trade_case(trade_cases[0])

        selected = orchestrator._select_recent_journal_context(
            rows=rows,
            market_snapshots=market_snapshots,
            risk_mode="trigger",
            limit=1,
            max_rationale_items=1,
            max_evidence_per_item=1,
            trade_cases=trade_cases,
            account_state=AccountState(total_balance=1000.0, available_balance=1000.0, positions=[], open_orders=[]),
            risk_state={"daily_pnl_pct": 0.0},
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["journal_id"], 2)
        self.assertEqual(len(selected[0]["rationale_items"]), 1)
        self.assertEqual(len(selected[0]["rationale_items"][0]["evidence"]), 1)
        self.assertIsNotNone(selected[0]["similarity_score"])
        self.assertTrue(selected[0]["matched_features"])
        self.assertTrue(any(str(item).startswith("vector_similarity:") for item in selected[0]["matched_features"]))
        self.assertTrue(any(str(item).startswith("vector_source:band_index") for item in selected[0]["matched_features"]))

    def test_build_similar_past_cases_prefers_trade_case_vector_matches(self):
        orchestrator = self._make_orchestrator()
        orchestrator.state_store = self.DummyCaseStore()
        orchestrator.state_store.upsert_trade_case(
            {
                "trade_case_id": "BTCUSDT-case-9",
                "symbol": "BTCUSDT",
                "status": "closed",
                "risk_mode": "trigger",
                "estimated_pnl_pct": -0.8,
                "slippage_bps": 7.5,
                "thesis_failure_reason": "execution_quality_degraded:wide_spread",
                "feature_vector": [30000.0, 120.0, 240.0],
                "payload": {},
            }
        )
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=120.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
                is_opportunity_trigger=True,
            )
        }

        cases = orchestrator._build_similar_past_cases(
            market_snapshots=market_snapshots,
            account_state=AccountState(total_balance=1000.0, available_balance=1000.0, positions=[], open_orders=[]),
            risk_state={"daily_pnl_pct": 0.0},
            is_trigger_mode=True,
            limit=2,
        )

        self.assertEqual(len(cases), 1)
        self.assertIn("Similarity", cases[0])
        self.assertIn("failure_reason=execution_quality_degraded:wide_spread", cases[0])
        self.assertIn("source=band_index", cases[0])
        self.assertEqual(orchestrator.last_similar_case_matches[0]["trade_case_id"], "BTCUSDT-case-9")

    def test_save_retrieval_evaluations_for_audit_persists_aligned_flags(self):
        orchestrator = self._make_orchestrator()
        orchestrator.state_store = self.DummyCaseStore()
        orchestrator.last_similar_case_matches = [
            {
                "context_type": "similar_past_cases",
                "rank_index": 1,
                "trade_case_id": "BTCUSDT-case-9",
                "case_symbol": "BTCUSDT",
                "matched_symbol": "BTCUSDT",
                "similarity": 0.91,
                "final_score": 1.42,
                "retrieval_source": "band_index",
                "matched_band_count": 4,
                "payload": {"thesis_failure_reason": "execution_quality_degraded:wide_spread"},
            }
        ]
        intent = type(
            "Intent",
            (),
            {
                "decision": "BUY",
                "orders": [type("Order", (), {"symbol": "BTCUSDT"})()],
            },
        )()

        orchestrator._save_retrieval_evaluations_for_audit(
            audit_log_id=501,
            intent=intent,
            estimated_pnl_pct=0.8,
            risk_mode="trigger",
        )

        self.assertEqual(len(orchestrator.state_store.retrieval_evaluations), 1)
        self.assertEqual(orchestrator.state_store.retrieval_evaluations[0]["trade_case_id"], "BTCUSDT-case-9")
        self.assertTrue(orchestrator.state_store.retrieval_evaluations[0]["decision_symbol_aligned"])

    def test_select_long_term_memory_context_prioritizes_matching_rule_and_compacts(self):
        orchestrator = self._make_orchestrator()
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=120.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
            )
        }
        market_snapshots["BTCUSDT"].regime.trend_alignment = "aligned_up"
        market_snapshots["BTCUSDT"].regime.volatility_regime = "high"
        rows = [
            {
                "rule": "For BTCUSDT, avoid low-evidence breakout entries in high volatility.",
                "category": "risk_control",
                "status": "rule",
                "support": 6,
                "regime": "trigger",
                "apply_when": {
                    "regime": ["trigger"],
                    "volatility": ["high"],
                    "trend": ["aligned_up"],
                },
                "ignore_when": ["Ignore when confirmation quality is high.", "Ignore when drawdown is zero."],
                "evidence_journal_ids": [1, 2, 3, 4],
                "metrics": {
                    "candidate_lessons": [{"seed": "large payload should be dropped"}],
                    "support": 6,
                },
            },
            {
                "rule": "Generic observation with weak support.",
                "category": "lesson",
                "status": "hypothesis",
                "support": 1,
                "regime": "general",
                "apply_when": {
                    "regime": ["general"],
                    "volatility": ["normal"],
                    "trend": ["mixed"],
                },
                "ignore_when": ["n/a"],
                "evidence_journal_ids": [],
                "metrics": {},
            },
        ]

        selected = orchestrator._select_long_term_memory_context(
            rows=rows,
            market_snapshots=market_snapshots,
            risk_mode="trigger",
            limit=1,
            max_ignore_when=1,
        )

        self.assertEqual(len(selected), 1)
        self.assertIn("BTCUSDT", selected[0]["rule"])
        self.assertEqual(len(selected[0]["ignore_when"]), 1)
        self.assertNotIn("candidate_lessons", selected[0]["metrics"])

    def test_select_long_term_memory_context_prioritizes_matching_hold_contexts(self):
        orchestrator = self._make_orchestrator()
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=120.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=True,
                volume_surge_trigger_active=True,
                range_break_trigger_active=True,
                is_opportunity_trigger=True,
                spread_percentile_1h=48.0,
                depth_percentile_1h=72.0,
                distance_to_invalid_level=1.1,
            )
        }
        market_snapshots["BTCUSDT"].regime.trend_alignment = "aligned_up"
        market_snapshots["BTCUSDT"].regime.volatility_regime = "high"
        market_snapshots["BTCUSDT"].timeframes["15m"] = {"trend": "UP"}
        rows = [
            {
                "rule": "Allow a small probe when breakout pressure aligns with tradable execution.",
                "category": "opportunity",
                "status": "rule",
                "support": 4,
                "regime": "trigger",
                "apply_when": {
                    "regime": ["trigger"],
                    "volatility": ["high"],
                    "trend": ["aligned_up"],
                },
                "ignore_when": ["Ignore when spreads widen."],
                "metrics": {
                    "trigger_context": "volume_surge+range_break+opportunity",
                    "execution_context": "acceptable_spread|deep_book|manageable_invalidation",
                    "trend_context": "UP|UP",
                    "hold_quality": "missed_opportunity_hold",
                },
            },
            {
                "rule": "Allow a small probe when momentum appears eventually.",
                "category": "opportunity",
                "status": "rule",
                "support": 6,
                "regime": "trigger",
                "apply_when": {
                    "regime": ["trigger"],
                    "volatility": ["high"],
                    "trend": ["aligned_up"],
                },
                "ignore_when": ["Ignore when spreads widen."],
                "metrics": {
                    "trigger_context": "fast_move+opportunity",
                    "execution_context": "wide_spread|thin_book|wide_invalidation",
                    "trend_context": "DOWN|DOWN",
                    "hold_quality": "missed_opportunity_hold",
                },
            },
        ]

        selected = orchestrator._select_long_term_memory_context(
            rows=rows,
            market_snapshots=market_snapshots,
            risk_mode="trigger",
            limit=1,
            max_ignore_when=1,
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["trigger_context"], "volume_surge+range_break+opportunity")
        self.assertEqual(selected[0]["execution_context"], "acceptable_spread|deep_book|manageable_invalidation")

    def test_build_similar_past_cases_applies_limit(self):
        class DummyRagEngine:
            def get_similar_cases_batch(self, *_args, **_kwargs):
                return {
                    "BTCUSDT": ["c1", "c2", "c3"],
                    "ETHUSDT": ["c4", "c5"],
                }

        orchestrator = self._make_orchestrator()
        orchestrator.rag_engine = DummyRagEngine()
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=120.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
                is_opportunity_trigger=True,
            ),
            "ETHUSDT": MarketSnapshot(
                symbol="ETHUSDT",
                current_price=2000.0,
                atr_15m=20.0,
                atr_1h=40.0,
                trend_1h="UP",
                recent_volume_surge=False,
                is_opportunity_trigger=True,
            ),
        }

        cases = orchestrator._build_similar_past_cases(
            market_snapshots=market_snapshots,
            account_state=AccountState(total_balance=1000.0, available_balance=900.0),
            risk_state={},
            is_trigger_mode=True,
            limit=3,
        )

        self.assertEqual(len(cases), 3)

    def test_deterministic_skip_when_no_exposure_and_entries_blocked(self):
        orchestrator = self._make_orchestrator()

        reason = orchestrator._get_deterministic_skip_reason(
            account_state=AccountState(
                total_balance=1000.0,
                available_balance=1000.0,
                positions=[],
                open_orders=[],
                open_orders_count=0,
            ),
            market_snapshots={
                "BTCUSDT": MarketSnapshot(
                    symbol="BTCUSDT",
                    current_price=30000.0,
                    atr_15m=120.0,
                    atr_1h=240.0,
                    trend_1h="UP",
                    recent_volume_surge=False,
                )
            },
            risk_state={"cooldown_active": True, "cooldown_remaining_minutes": 10},
            risk_mode="cooldown",
            risk_profile={"allowed_actions": ["HOLD", "REDUCE", "EXIT"]},
        )

        self.assertEqual(reason, "cooldown_active")

    def test_deterministic_skip_does_not_block_only_wide_spread_context(self):
        orchestrator = self._make_orchestrator()

        reason = orchestrator._get_deterministic_skip_reason(
            account_state=AccountState(
                total_balance=1000.0,
                available_balance=1000.0,
                positions=[],
                open_orders=[],
                open_orders_count=0,
            ),
            market_snapshots={
                "BTCUSDT": MarketSnapshot(
                    symbol="BTCUSDT",
                    current_price=30000.0,
                    atr_15m=120.0,
                    atr_1h=240.0,
                    trend_1h="UP",
                    recent_volume_surge=False,
                    risk_trigger_reasons=["wide_spread"],
                ),
                "ETHUSDT": MarketSnapshot(
                    symbol="ETHUSDT",
                    current_price=2000.0,
                    atr_15m=20.0,
                    atr_1h=40.0,
                    trend_1h="UP",
                    recent_volume_surge=False,
                    risk_trigger_reasons=["wide_spread"],
                ),
            },
            risk_state={},
            risk_mode="normal",
            risk_profile={"allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"]},
        )

        self.assertIsNone(reason)

    def test_deterministic_skip_does_not_apply_when_position_exists(self):
        orchestrator = self._make_orchestrator()

        reason = orchestrator._get_deterministic_skip_reason(
            account_state=AccountState(
                total_balance=1000.0,
                available_balance=900.0,
                positions=[
                    PositionState(
                        symbol="BTCUSDT",
                        side="LONG",
                        size=0.1,
                        entry_price=30000.0,
                        unrealized_pnl_pct=0.0,
                    )
                ],
                open_orders=[],
                open_orders_count=0,
            ),
            market_snapshots={},
            risk_state={"cooldown_active": True},
            risk_mode="cooldown",
            risk_profile={"allowed_actions": ["HOLD", "REDUCE", "EXIT"]},
        )

        self.assertIsNone(reason)

    def test_prepare_decision_market_snapshots_applies_alpha_policy_when_flat(self):
        orchestrator = self._make_orchestrator()
        orchestrator.state_store.upsert_policy_registry(
            {
                "policy_key": "champion:alpha_1h",
                "domain": "alpha_1h",
                "policy_type": "entry_filter",
                "active_candidate_policy_id": 7,
                "version": 1,
                "config": {"max_spread_percentile_1h": 70.0},
                "metrics": {"score": 0.6},
            }
        )
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=1000.0,
            positions=[],
            open_orders=[],
            open_orders_count=0,
        )
        market_snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=120.0,
                atr_1h=240.0,
                trend_1h="UP",
                recent_volume_surge=False,
                spread_percentile_1h=85.0,
            ),
            "ETHUSDT": MarketSnapshot(
                symbol="ETHUSDT",
                current_price=2000.0,
                atr_15m=20.0,
                atr_1h=40.0,
                trend_1h="UP",
                recent_volume_surge=False,
                spread_percentile_1h=35.0,
            ),
        }

        filtered, summary = orchestrator._prepare_decision_market_snapshots(
            account_state=account_state,
            market_snapshots=market_snapshots,
            loop_mode="1h",
        )

        self.assertEqual(sorted(filtered.keys()), ["ETHUSDT"])
        self.assertTrue(summary["applied"])
        self.assertIn("BTCUSDT", summary["blocked_symbols"])

    def test_determine_desired_loop_mode_uses_5m_only_for_active_management(self):
        orchestrator = self._make_orchestrator()
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=900.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000.0,
                    unrealized_pnl_pct=0.0,
                )
            ],
            open_orders=[],
            open_orders_count=0,
        )

        desired_trigger = orchestrator._determine_desired_loop_mode(
            {"trigger_active": True, "final_account_state": account_state, "has_unknown_orders": False}
        )
        desired_flat_trigger = orchestrator._determine_desired_loop_mode(
            {
                "trigger_active": True,
                "final_account_state": AccountState(
                    total_balance=1000.0,
                    available_balance=1000.0,
                    positions=[],
                    open_orders=[],
                    open_orders_count=0,
                ),
                "has_unknown_orders": False,
            }
        )
        desired_management = orchestrator._determine_desired_loop_mode(
            {"trigger_active": False, "final_account_state": account_state, "has_unknown_orders": False}
        )
        desired_idle = orchestrator._determine_desired_loop_mode(
            {
                "trigger_active": False,
                "final_account_state": AccountState(
                    total_balance=1000.0,
                    available_balance=1000.0,
                    positions=[],
                    open_orders=[],
                    open_orders_count=0,
                ),
                "has_unknown_orders": False,
            }
        )

        self.assertEqual(desired_trigger, "5m")
        self.assertEqual(desired_flat_trigger, "1h")
        self.assertEqual(desired_management, "15m")
        self.assertEqual(desired_idle, "1h")

    def test_determine_desired_loop_mode_preserves_current_mode_when_lock_skipped(self):
        orchestrator = self._make_orchestrator()
        orchestrator.active_loop_mode = "1h"

        desired_mode = orchestrator._determine_desired_loop_mode(
            {
                "lock_skipped": True,
                "has_positions": True,
                "has_open_orders": True,
                "has_unknown_orders": True,
            }
        )

        self.assertEqual(desired_mode, "1h")

    def test_trigger_with_recent_close_but_no_management_target_prefers_15m(self):
        orchestrator = self._make_orchestrator()
        orchestrator.last_close_at = time.time()

        desired_mode = orchestrator._determine_desired_loop_mode(
            {
                "trigger_active": True,
                "final_account_state": AccountState(
                    total_balance=1000.0,
                    available_balance=1000.0,
                    positions=[],
                    open_orders=[],
                    open_orders_count=0,
                ),
                "has_unknown_orders": False,
            }
        )

        self.assertEqual(desired_mode, "15m")

    def test_trigger_mode_downgrade_requires_clear_streak_when_management_target_remains(self):
        orchestrator = self._make_orchestrator()
        orchestrator.active_loop_mode = "5m"
        orchestrator.active_loop_mode_since = time.time()
        cycle_result = {
            "final_account_state": AccountState(
                total_balance=1000.0,
                available_balance=900.0,
                positions=[],
                open_orders=[
                    OpenOrderState(symbol="BTCUSDT", side="BUY", order_type="LIMIT", quantity=0.1)
                ],
                open_orders_count=1,
            ),
            "has_unknown_orders": False,
        }

        mode_first = orchestrator._apply_adaptive_loop_mode("15m", time.time(), cycle_result=cycle_result)
        mode_second = orchestrator._apply_adaptive_loop_mode("15m", time.time(), cycle_result=cycle_result)

        self.assertEqual(mode_first, "5m")
        self.assertEqual(mode_second, "15m")

    def test_trigger_mode_downgrades_immediately_when_flat_after_position_close(self):
        orchestrator = self._make_orchestrator()
        orchestrator.active_loop_mode = "5m"
        orchestrator.active_loop_mode_since = time.time()

        mode = orchestrator._apply_adaptive_loop_mode(
            "15m",
            time.time(),
            cycle_result={
                "final_account_state": AccountState(
                    total_balance=1000.0,
                    available_balance=1000.0,
                    positions=[],
                    open_orders=[],
                    open_orders_count=0,
                ),
                "has_unknown_orders": False,
            },
        )

        self.assertEqual(mode, "15m")

    def test_annotate_trade_case_entries_opens_and_closes_case(self):
        orchestrator = self._make_orchestrator()
        orchestrator.state_store = self.DummyCaseStore()

        opened = orchestrator._annotate_trade_case_entries(
            audit_log_id=101,
            symbol_journal_entries=[
                {"symbol": "BTCUSDT", "decision": "BUY"},
            ],
            position_state_transitions=[
                {"symbol": "BTCUSDT", "from_state": "flat", "to_state": "open", "event_type": "intent_buy"},
            ],
            intent=type("Intent", (), {"orders": [], "rationale_items": []})(),
            before_state=AccountState(total_balance=1000.0, available_balance=1000.0, positions=[], open_orders=[]),
            after_state=AccountState(
                total_balance=1000.0,
                available_balance=900.0,
                positions=[
                    PositionState(
                        symbol="BTCUSDT",
                        side="LONG",
                        size=0.1,
                        entry_price=30000.0,
                        unrealized_pnl_pct=1.2,
                    )
                ],
                open_orders=[],
            ),
            market_snapshots={},
            fills=[],
            risk_mode="trigger",
            risk_state={"daily_pnl_pct": 0.0},
            observed_at="2026-03-07 10:00:00",
        )
        trade_case_id = opened[0]["trade_case_id"]
        orchestrator.state_store.save_execution_fills(
            101,
            [
                {
                    "fill_timestamp": "2026-03-07 10:00:05",
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "price": 30000.0,
                    "amount": 0.1,
                }
            ],
        )
        orchestrator.state_store.save_trade_case_snapshots(
            [
                {
                    "trade_case_id": trade_case_id,
                    "timestamp": "2026-03-07 09:58:00",
                    "symbol": "BTCUSDT",
                    "unrealized_pnl_pct": 7.5,
                },
                {
                    "trade_case_id": trade_case_id,
                    "timestamp": "2026-03-07 10:10:00",
                    "symbol": "BTCUSDT",
                    "unrealized_pnl_pct": 1.8,
                },
                {
                    "trade_case_id": trade_case_id,
                    "timestamp": "2026-03-07 10:20:00",
                    "symbol": "BTCUSDT",
                    "unrealized_pnl_pct": -0.9,
                },
                {
                    "trade_case_id": trade_case_id,
                    "timestamp": "2026-03-07 10:35:00",
                    "symbol": "BTCUSDT",
                    "unrealized_pnl_pct": -8.0,
                },
            ]
        )
        orchestrator.state_store.save_execution_fills(
            102,
            [
                {
                    "fill_timestamp": "2026-03-07 10:30:05",
                    "symbol": "BTCUSDT",
                    "side": "sell",
                    "price": 30120.0,
                    "amount": 0.1,
                }
            ],
        )

        closed = orchestrator._annotate_trade_case_entries(
            audit_log_id=102,
            symbol_journal_entries=[
                {"symbol": "BTCUSDT", "decision": "EXIT"},
            ],
            position_state_transitions=[
                {"symbol": "BTCUSDT", "from_state": "open", "to_state": "closed", "event_type": "intent_exit"},
            ],
            intent=type("Intent", (), {"orders": [], "rationale_items": []})(),
            before_state=AccountState(
                total_balance=1000.0,
                available_balance=950.0,
                positions=[
                    PositionState(
                        symbol="BTCUSDT",
                        side="LONG",
                        size=0.1,
                        entry_price=30000.0,
                        unrealized_pnl_pct=0.4,
                    )
                ],
                open_orders=[],
            ),
            after_state=AccountState(total_balance=1002.0, available_balance=1002.0, positions=[], open_orders=[]),
            market_snapshots={},
            fills=[],
            risk_mode="trigger",
            risk_state={"daily_pnl_pct": 0.0},
            observed_at="2026-03-07 10:30:00",
        )

        self.assertEqual(opened[0]["case_status"], "opened")
        self.assertEqual(opened[0]["case_opened_at"], "2026-03-07 10:00:00")
        self.assertEqual(closed[0]["trade_case_id"], trade_case_id)
        self.assertEqual(closed[0]["case_status"], "closed")
        self.assertEqual(closed[0]["case_closed_at"], "2026-03-07 10:30:00")
        self.assertEqual(closed[0]["holding_minutes"], 30.0)
        self.assertEqual(closed[0]["observed_mfe_pct"], 1.8)
        self.assertEqual(closed[0]["observed_mae_pct"], -0.9)
        self.assertEqual(orchestrator.state_store.get_trade_case(trade_case_id)["status"], "closed")
        self.assertEqual(orchestrator.state_store.get_active_trade_case("BTCUSDT"), {})

    def test_capture_active_trade_case_snapshots_uses_live_ws_state(self):
        orchestrator = self._make_orchestrator()
        orchestrator.state_store = self.DummyCaseStore()
        orchestrator.state_store.upsert_active_trade_case(
            symbol="BTCUSDT",
            trade_case_id="BTCUSDT-case-live",
            opened_at="2026-03-07 10:00:00",
            entry_decision="BUY",
            last_decision="BUY",
            last_audit_log_id=55,
            payload={},
        )

        class DummyPrivateFeed:
            def get_account_state(self, symbols):
                return AccountState(
                    total_balance=1000.0,
                    available_balance=900.0,
                    positions=[
                        PositionState(
                            symbol="BTCUSDT",
                            side="LONG",
                            size=0.1,
                            entry_price=30000.0,
                            mark_price=30040.0,
                            unrealized_pnl_pct=1.1,
                        )
                    ],
                    open_orders=[],
                )

        class DummyAccountReader:
            private_feed = DummyPrivateFeed()

            def get_account_state(self, symbols, force_refresh=False):
                return self.private_feed.get_account_state(symbols)

        class DummyPublicFeed:
            def get_ticker(self, symbol):
                return {"last": 30042.0, "markPrice": 30040.0}

            def get_order_book(self, symbol, limit=10):
                return {"bids": [[30041.0, 1.0]], "asks": [[30043.0, 1.0]]}

        orchestrator.account_reader = DummyAccountReader()
        orchestrator.public_feed = DummyPublicFeed()

        captured = orchestrator._capture_active_trade_case_snapshots(now_epoch=time.time())

        self.assertEqual(captured, 1)
        self.assertEqual(len(orchestrator.state_store.trade_case_snapshots), 1)
        self.assertEqual(orchestrator.state_store.trade_case_snapshots[0]["trade_case_id"], "BTCUSDT-case-live")

    def test_position_mode_downgrades_immediately_to_1h_when_fully_flat(self):
        orchestrator = self._make_orchestrator()
        orchestrator.active_loop_mode = "15m"
        orchestrator.active_loop_mode_since = time.time() - (31 * 60)

        observed = orchestrator._apply_adaptive_loop_mode(
            "1h",
            time.time(),
            cycle_result={
                "final_account_state": AccountState(
                    total_balance=1000.0,
                    available_balance=1000.0,
                    positions=[],
                    open_orders=[],
                    open_orders_count=0,
                ),
                "has_unknown_orders": False,
            },
        )

        self.assertEqual(observed, "1h")

    def test_position_management_cycle_profile_blocks_new_entries(self):
        orchestrator = self._make_orchestrator()
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=900.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000.0,
                    unrealized_pnl_pct=0.0,
                )
            ],
            open_orders=[],
            open_orders_count=0,
        )

        effective_profile = orchestrator._build_effective_risk_profile(
            "15m",
            account_state=account_state,
            risk_profile={"allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"]},
        )

        self.assertEqual(effective_profile["cycle_role"], "position_management")
        self.assertEqual(effective_profile["allowed_actions"], ["HOLD", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"])

    def test_trigger_cycle_profile_blocks_new_entries(self):
        orchestrator = self._make_orchestrator()
        account_state = AccountState(
            total_balance=1000.0,
            available_balance=900.0,
            positions=[
                PositionState(
                    symbol="BTCUSDT",
                    side="LONG",
                    size=0.1,
                    entry_price=30000.0,
                    unrealized_pnl_pct=0.0,
                )
            ],
            open_orders=[],
            open_orders_count=0,
        )

        effective_profile = orchestrator._build_effective_risk_profile(
            "5m",
            account_state=account_state,
            risk_profile={
                "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"],
                "block_additional_entries_when_position_exists": True,
            },
        )

        self.assertEqual(effective_profile["cycle_role"], "execution_safety")
        self.assertEqual(effective_profile["allowed_actions"], ["HOLD", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"])

    def test_trigger_cycle_profile_blocks_new_entries_even_when_flat(self):
        orchestrator = self._make_orchestrator()

        effective_profile = orchestrator._build_effective_risk_profile(
            "5m",
            account_state=AccountState(total_balance=1000.0, available_balance=1000.0, positions=[], open_orders=[]),
            risk_profile={"allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"]},
        )

        self.assertEqual(effective_profile["cycle_role"], "execution_safety")
        self.assertEqual(effective_profile["allowed_actions"], ["HOLD", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"])

    def test_effective_risk_profile_applies_exposure_policy_override_outside_drawdown(self):
        orchestrator = self._make_orchestrator()
        orchestrator.state_store.upsert_policy_registry(
            {
                "policy_key": "champion:exposure_budget:normal|aligned_up|high",
                "domain": "exposure_budget",
                "policy_type": "risk_profile_override",
                "regime_key": "normal|aligned_up|high",
                "active_candidate_policy_id": 12,
                "version": 1,
                "config": {
                    "max_order_notional_pct_balance": 82.0,
                    "entry_size_multiplier": 0.9,
                },
                "metrics": {"score": 0.4},
            }
        )
        effective_profile = orchestrator._build_effective_risk_profile(
            "1h",
            account_state=AccountState(total_balance=1000.0, available_balance=1000.0, positions=[], open_orders=[]),
            risk_profile={"allowed_actions": ["HOLD", "BUY", "SELL"], "max_order_notional_pct_balance": 100.0},
            risk_mode="normal",
            regime_key="aligned_up|high",
        )

        self.assertEqual(effective_profile["max_order_notional_pct_balance"], 82.0)
        self.assertEqual(effective_profile["entry_size_multiplier"], 0.9)
        self.assertEqual(effective_profile["exposure_policy_key"], "champion:exposure_budget:normal|aligned_up|high")

    def test_effective_risk_profile_leaves_drawdown_profile_unchanged(self):
        orchestrator = self._make_orchestrator()
        orchestrator.state_store.upsert_policy_registry(
            {
                "policy_key": "champion:exposure_budget:drawdown|aligned_up|high",
                "domain": "exposure_budget",
                "policy_type": "risk_profile_override",
                "regime_key": "drawdown|aligned_up|high",
                "active_candidate_policy_id": 13,
                "version": 1,
                "config": {"max_order_notional_pct_balance": 99.0, "entry_size_multiplier": 1.0},
                "metrics": {"score": 0.9},
            }
        )
        effective_profile = orchestrator._build_effective_risk_profile(
            "1h",
            account_state=AccountState(total_balance=1000.0, available_balance=1000.0, positions=[], open_orders=[]),
            risk_profile={"allowed_actions": ["HOLD", "BUY", "SELL"], "max_order_notional_pct_balance": 20.0},
            risk_mode="drawdown",
            regime_key="aligned_up|high",
        )

        self.assertEqual(effective_profile["max_order_notional_pct_balance"], 20.0)
        self.assertNotIn("exposure_policy_key", effective_profile)

    def test_risk_transition_alerts_cover_kill_switch_drawdown_and_cooldown_extension(self):
        orchestrator = self._make_orchestrator()

        orchestrator._maybe_alert_risk_state_transitions(
            {
                "daily_pnl_pct": -0.4,
                "drawdown_active": False,
                "drawdown_activate_pct": 1.5,
                "cooldown_active": False,
            }
        )
        self.assertEqual(orchestrator.alert_manager.calls, [])

        orchestrator._maybe_alert_risk_state_transitions(
            {
                "daily_pnl_pct": -3.4,
                "drawdown_active": True,
                "drawdown_activate_pct": 1.5,
                "cooldown_active": True,
                "cooldown_minutes_effective": 55,
                "cooldown_remaining_minutes": 42,
                "cooldown_components": ["large_loss", "repeated_failure"],
                "consecutive_losses": 3,
                "dominant_failure_reason": "late_entry",
                "last_loss_estimated_pnl_pct": -1.2,
            }
        )

        self.assertEqual(
            [call["title"] for call in orchestrator.alert_manager.calls],
            [
                "Daily Kill Switch Activated",
                "Drawdown Mode Entered",
                "Cooldown Extended",
            ],
        )

        orchestrator._maybe_alert_risk_state_transitions(
            {
                "daily_pnl_pct": -3.4,
                "drawdown_active": True,
                "drawdown_activate_pct": 1.5,
                "cooldown_active": True,
                "cooldown_minutes_effective": 55,
                "cooldown_remaining_minutes": 30,
                "cooldown_components": ["large_loss", "repeated_failure"],
                "consecutive_losses": 3,
                "dominant_failure_reason": "late_entry",
                "last_loss_estimated_pnl_pct": -1.2,
            }
        )
        self.assertEqual(len(orchestrator.alert_manager.calls), 3)

        orchestrator._maybe_alert_risk_state_transitions(
            {
                "daily_pnl_pct": -0.2,
                "drawdown_active": False,
                "drawdown_activate_pct": 1.5,
                "cooldown_active": False,
            }
        )
        self.assertEqual(orchestrator.alert_manager.calls[-1]["title"], "Drawdown Mode Cleared")

    def test_websocket_transition_alerts_cover_stale_and_recovered(self):
        orchestrator = self._make_orchestrator()
        orchestrator.public_feed = self.DummyDiagnosticsFeed(
            {
                "started": True,
                "available": True,
                "ws_stale_after_ms": 35_000,
                "last_message_age_ms": 2_000,
                "stale_symbols": [],
            }
        )
        orchestrator.private_account_feed = self.DummyDiagnosticsFeed(
            {
                "started": True,
                "available": True,
                "health_status": "healthy",
                "ws_stale_after_ms": 35_000,
                "last_ws_message_age_ms": 1_000,
                "last_private_event_age_ms": 1_000,
            }
        )

        orchestrator._maybe_alert_websocket_transitions()
        self.assertEqual(orchestrator.alert_manager.calls, [])

        orchestrator.public_feed.diagnostics.update(
            {
                "last_message_age_ms": 41_000,
                "stale_symbols": ["BTCUSDT"],
            }
        )
        orchestrator.private_account_feed.diagnostics.update(
            {
                "health_status": "stale",
                "last_ws_message_age_ms": 40_000,
                "last_private_event_age_ms": 42_000,
            }
        )

        orchestrator._maybe_alert_websocket_transitions()
        self.assertEqual(
            [call["title"] for call in orchestrator.alert_manager.calls],
            ["WebSocket Stale", "WebSocket Stale"],
        )

        orchestrator.public_feed.diagnostics.update(
            {
                "last_message_age_ms": 3_000,
                "stale_symbols": [],
            }
        )
        orchestrator.private_account_feed.diagnostics.update(
            {
                "health_status": "healthy",
                "last_ws_message_age_ms": 2_000,
                "last_private_event_age_ms": 2_000,
            }
        )

        orchestrator._maybe_alert_websocket_transitions()
        self.assertEqual(
            [call["title"] for call in orchestrator.alert_manager.calls[-2:]],
            ["WebSocket Recovered", "WebSocket Recovered"],
        )

    def test_recent_symbol_loss_guard_blocks_new_entries(self):
        orchestrator = self._make_orchestrator()
        orchestrator.state_store.recent_closed_cases["BTCUSDT"] = {
            "active": True,
            "reason": "recent_symbol_loss_guard",
            "remaining_minutes": 18,
            "symbol": "BTCUSDT",
            "closed_at": "2026-03-08 10:15:00",
            "estimated_pnl_pct": -0.9,
            "exit_reason": "thesis_break",
        }

        enriched = orchestrator._enrich_risk_state_for_cycle(
            RiskStateSnapshot(),
            account_state=AccountState(total_balance=1000.0, available_balance=1000.0, positions=[], open_orders=[]),
            market_snapshots={"BTCUSDT": MarketSnapshot(symbol="BTCUSDT", current_price=30000.0, atr_15m=100.0, atr_1h=200.0, trend_1h="UP", recent_volume_surge=False)},
            execution_guards=orchestrator._build_execution_guards(),
        )
        reason = orchestrator._get_deterministic_skip_reason(
            account_state=AccountState(total_balance=1000.0, available_balance=1000.0, positions=[], open_orders=[]),
            market_snapshots={"BTCUSDT": MarketSnapshot(symbol="BTCUSDT", current_price=30000.0, atr_15m=100.0, atr_1h=200.0, trend_1h="UP", recent_volume_surge=False)},
            risk_state=enriched,
            risk_mode="normal",
            risk_profile={"allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"]},
        )

        self.assertEqual(reason, "recent_symbol_loss_guard")
        rationale_items = orchestrator._build_skip_rationale_items(reason, enriched, {}, {})
        self.assertEqual(rationale_items[0].evidence[0].source, "risk_state.reentry_guard_remaining_minutes")

    def test_recent_symbol_loss_guard_is_cleared_when_position_is_still_live(self):
        orchestrator = self._make_orchestrator()
        orchestrator.state_store.active_cases["BTCUSDT"] = {"trade_case_id": "BTCUSDT-case-1"}
        orchestrator.state_store.recent_closed_cases["BTCUSDT"] = {
            "active": True,
            "reason": "recent_symbol_loss_guard",
            "remaining_minutes": 18,
            "symbol": "BTCUSDT",
            "closed_at": "2026-03-08 10:15:00",
            "estimated_pnl_pct": -0.9,
            "exit_reason": "thesis_break",
        }

        enriched = orchestrator._enrich_risk_state_for_cycle(
            RiskStateSnapshot(),
            account_state=AccountState(
                total_balance=1000.0,
                available_balance=900.0,
                positions=[PositionState(symbol="BTCUSDT", side="LONG", size=0.1, entry_price=30000.0, unrealized_pnl_pct=0.3)],
                open_orders=[],
            ),
            market_snapshots={"BTCUSDT": MarketSnapshot(symbol="BTCUSDT", current_price=30000.0, atr_15m=100.0, atr_1h=200.0, trend_1h="UP", recent_volume_surge=False)},
            execution_guards=orchestrator._build_execution_guards(),
        )

        self.assertFalse(enriched.reentry_guard_active)
        self.assertEqual(enriched.reentry_guard_symbol, "BTCUSDT")

    def test_execution_policy_evaluation_entries_include_cancel_orders(self):
        orchestrator = self._make_orchestrator()

        class DummyExecutionEngine:
            def _get_fills_for_receipt(self, _order, _receipt, _fills):
                return []

        orchestrator.execution_engine = DummyExecutionEngine()
        intent = TradeIntent(
            decision="REPLACE_ENTRY",
            orders=[
                OrderIntent(
                    symbol="BTCUSDT",
                    action="CANCEL",
                    order_type="MARKET",
                    quantity=0.0,
                    target_order_id="order-1",
                    target_client_order_id="client-1",
                    replace_group_id="replace-1",
                    lineage_role="cancel",
                    execution_context={"action_type": "cancel"},
                ),
                OrderIntent(
                    symbol="BTCUSDT",
                    action="BUY",
                    order_type="LIMIT",
                    quantity=0.01,
                    price=30000.0,
                    replace_group_id="replace-1",
                    lineage_role="replacement",
                    lineage_parent_order_id="order-1",
                    lineage_parent_client_order_id="client-1",
                    wait_for_cancel_confirmation=True,
                    execution_profile="limit_passive",
                    execution_context={"action_type": "replace_entry", "urgency_bucket": "low", "liquidity_bucket": "good"},
                ),
            ],
            rationale=["Reprice stale order"],
            risk_notes=[],
            confidence=0.8,
        )
        entries = orchestrator._build_execution_policy_evaluation_entries(
            audit_log_id=1,
            intent=intent,
            planned_orders=intent.orders,
            receipts=[
                {"symbol": "BTCUSDT", "status": "canceled"},
                {"symbol": "BTCUSDT", "status": "open"},
            ],
            fills=[],
            market_snapshots={},
            symbol_journal_entries=[],
            loop_mode="15m",
            risk_mode="normal",
            is_trigger_mode=False,
        )

        self.assertEqual(len(entries), 2)
        cancel_entry = entries[0]
        self.assertEqual(cancel_entry["action_type"], "cancel")
        self.assertEqual(cancel_entry["order_type"], "CANCEL")
        self.assertEqual(cancel_entry["fill_ratio"], 1.0)
