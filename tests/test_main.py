import unittest
import time

from main import TradingOrchestrator
from core.models import AccountState, MarketSnapshot, PositionState


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
            self.trade_case_snapshots = []
            self.execution_fills_by_audit = {}
            self.retrieval_evaluations = []

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
            last_decision=None,
            last_audit_log_id=None,
            payload=None,
        ):
            self.active_cases[symbol] = {
                "symbol": symbol,
                "trade_case_id": trade_case_id,
                "opened_at": opened_at,
                "entry_decision": entry_decision,
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

    def _make_orchestrator(self):
        orchestrator = TradingOrchestrator.__new__(TradingOrchestrator)
        orchestrator.risk_engine = DummyRiskEngine()
        orchestrator.rag_engine = self.DummyRagEngine()
        orchestrator.symbols = ["BTCUSDT"]
        orchestrator.state_store = self.DummyCaseStore()
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

    def test_determine_desired_loop_mode_prefers_trigger_then_management_then_idle(self):
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
        self.assertEqual(desired_management, "15m")
        self.assertEqual(desired_idle, "1h")

    def test_trigger_mode_downgrade_requires_clear_streak_only(self):
        orchestrator = self._make_orchestrator()
        orchestrator.active_loop_mode = "5m"
        orchestrator.active_loop_mode_since = time.time()

        mode_first = orchestrator._apply_adaptive_loop_mode("15m", time.time())
        mode_second = orchestrator._apply_adaptive_loop_mode("15m", time.time())

        self.assertEqual(mode_first, "5m")
        self.assertEqual(mode_second, "15m")

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

    def test_position_mode_downgrade_requires_min_hold_and_flat_streak(self):
        orchestrator = self._make_orchestrator()
        orchestrator.active_loop_mode = "15m"
        orchestrator.active_loop_mode_since = time.time() - (31 * 60)

        observed = [
            orchestrator._apply_adaptive_loop_mode("1h", time.time())
            for _ in range(4)
        ]

        self.assertEqual(observed[:3], ["15m", "15m", "15m"])
        self.assertEqual(observed[3], "1h")

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
        self.assertEqual(effective_profile["allowed_actions"], ["HOLD", "REDUCE", "EXIT"])

    def test_trigger_cycle_profile_blocks_additional_entries_when_position_exists(self):
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

        self.assertEqual(effective_profile["cycle_role"], "trigger_response")
        self.assertEqual(effective_profile["allowed_actions"], ["HOLD", "REDUCE", "EXIT"])
