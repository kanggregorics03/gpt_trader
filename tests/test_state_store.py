import os
import shutil
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from db.state_store import StateStore


class TestStateStore(unittest.TestCase):
    def setUp(self):
        base_tmp = Path("c:/gpt_trader/.tmp_tests")
        base_tmp.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(dir=base_tmp, suffix=".db")
        os.close(fd)
        self.tmpdir = base_tmp
        self.db_path = raw_path
        self.store = StateStore(self.db_path)

    def tearDown(self):
        self.store.close()
        try:
            Path(self.db_path).unlink(missing_ok=True)
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_monitoring_snapshot_counts_rejections_timeouts_and_memory(self):
        self.store.save_audit(
            account={"total_balance": 1000},
            market={"symbol": "BTCUSDT"},
            intent={"decision": "BUY"},
            risk_ok=False,
            reason="Cooldown active for another 10 minute(s).",
            receipts=[{"status": "timeout_unresolved"}],
            decision_input_hash="abc123",
            risk_mode="cooldown",
            risk_profile={"allowed_actions": ["HOLD", "REDUCE", "EXIT"]},
            execution_summary={"loop_type": "Trigger"},
        )
        self.store.save_audit(
            account={},
            market={},
            intent={"decision": "HOLD"},
            risk_ok=False,
            reason="Error in Trigger cycle: boom",
            receipts=[],
            execution_summary={"error": "boom"},
        )
        self.store.save_long_term_memory_entries(
            [
                {
                    "rule": "Avoid re-entry during cooldown.",
                    "version": 1,
                    "metrics": {
                        "status": "rule",
                        "support": 4,
                        "regime": "general",
                        "apply_when": {"regime": ["general"], "volatility": ["medium"], "trend": ["flat"]},
                        "ignore_when": ["Skip when a new confirmed trend starts."],
                    },
                }
            ]
        )

        snapshot = self.store.get_monitoring_snapshot()

        self.assertEqual(snapshot["total_audits_24h"], 2)
        self.assertEqual(snapshot["rejected_24h"], 2)
        self.assertEqual(snapshot["loop_errors_24h"], 1)
        self.assertEqual(snapshot["timeouts_unresolved_24h"], 1)
        self.assertEqual(snapshot["semantic_validation_summary"]["last_24h"]["failure_count"], 0)
        self.assertEqual(snapshot["semantic_validation_summary"]["latest_50"]["failure_count"], 0)
        self.assertEqual(len(snapshot["recent_memory"]), 1)
        self.assertEqual(snapshot["recent_memory"][0]["metrics"]["status"], "rule")
        self.assertEqual(snapshot["recent_symbol_journal"], [])
        hashed_log = next(log for log in snapshot["recent_logs"] if log["decision_input_hash"] == "abc123")
        self.assertEqual(hashed_log["risk_mode"], "cooldown")

    def test_monitoring_snapshot_summarizes_semantic_validation_issues(self):
        self.store.save_audit(
            account={"total_balance": 1000},
            market={"symbol": "BTCUSDT"},
            intent={"decision": "BUY"},
            risk_ok=True,
            reason="Approved",
            receipts=[],
            execution_summary={
                "ai_usage": {
                    "model_name": "gemini-2.5-flash",
                    "total_token_count": 123,
                }
            },
        )
        self.store.save_audit(
            account={"total_balance": 1000},
            market={"symbol": "BTCUSDT"},
            intent={"decision": "HOLD"},
            risk_ok=True,
            reason="Safe action.",
            receipts=[],
            execution_summary={
                "ai_usage": {
                    "semantic_validation_error": True,
                    "model_name": "gemini-2.5-flash",
                    "issues": [
                        "trade_implication_action_mismatch",
                        "operator_condition_failed",
                        "trade_implication_action_mismatch",
                    ],
                }
            },
        )

        snapshot = self.store.get_monitoring_snapshot()

        self.assertEqual(snapshot["semantic_validation_summary"]["last_24h"]["failure_count"], 1)
        self.assertEqual(snapshot["semantic_validation_summary"]["last_7d"]["failure_count"], 1)
        self.assertEqual(snapshot["semantic_validation_summary"]["latest_50"]["failure_count"], 1)
        self.assertEqual(
            snapshot["semantic_validation_summary"]["last_24h"]["top_issues"][0],
            {"issue": "trade_implication_action_mismatch", "count": 2},
        )
        self.assertEqual(
            snapshot["semantic_validation_summary"]["last_24h"]["top_models"][0],
            {
                "model_name": "gemini-2.5-flash",
                "count": 1,
                "total_calls": 2,
                "failure_rate": 0.5,
            },
        )

    def test_get_journal_entries_since_returns_new_fields(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000},
            market={"market_snapshots": {"BTCUSDT": {"regime": {"trend_alignment": "aligned_up"}}}},
            intent={"decision": "EXIT"},
            risk_ok=True,
            reason="Approved",
            receipts=[],
            risk_mode="drawdown",
            execution_summary={"ai_usage": {"total_token_count": 321}},
        )
        self.store.save_position_ledger_entries(
            audit_id,
            [
                {
                    "symbol": "BTCUSDT",
                    "decision": "EXIT",
                    "fill_count": 1,
                    "total_amount": 0.1,
                    "gross_realized_pnl": -15.0,
                    "fee_cost": 1.0,
                    "net_realized_pnl": -16.0,
                }
            ],
        )
        self.store.save_journal(
            decision="EXIT",
            rationale=["Closed position"],
            audit_log_id=audit_id,
            rationale_items=[
                {
                    "summary": "Position invalidated",
                    "why_it_matters": "Breakdown failed.",
                    "trade_implication": "Exit remaining exposure.",
                    "failure_mode": "Momentum reclaims prior support.",
                    "evidence": [
                        {
                            "signal": "rsi_14",
                            "symbol": "BTCUSDT",
                            "timeframe": "15m",
                            "operator": "lte",
                            "observed_value": 39.5,
                            "threshold": 40.0,
                            "units": "index",
                            "source": "market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                        }
                    ],
                }
            ],
            confidence=0.9,
            risk_reason="Approved",
            estimated_pnl_pct=-1.5,
            loss_signal=True,
            trade_case_ids=["BTCUSDT-case-1"],
            case_statuses={"BTCUSDT": "closed"},
            case_summary="BTCUSDT:closed",
        )

        rows = self.store.get_journal_entries_since(
            datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["decision"], "EXIT")
        self.assertEqual(rows[0]["loss_signal"], 1)
        self.assertAlmostEqual(rows[0]["estimated_pnl_pct"], -1.5)
        self.assertEqual(rows[0]["rationale_json"][0]["evidence"][0]["signal"], "rsi_14")
        self.assertEqual(rows[0]["risk_mode"], "drawdown")
        self.assertEqual(rows[0]["ledger_net_realized_pnl"], -16.0)
        self.assertEqual(rows[0]["execution_summary"]["ai_usage"]["total_token_count"], 321)
        self.assertEqual(rows[0]["trade_case_ids"], ["BTCUSDT-case-1"])
        self.assertEqual(rows[0]["case_statuses"]["BTCUSDT"], "closed")
        self.assertEqual(rows[0]["case_summary"], "BTCUSDT:closed")

    def test_retrieval_evaluations_round_trip(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000},
            market={"market_snapshots": {"BTCUSDT": {"symbol": "BTCUSDT"}}},
            intent={"decision": "BUY"},
            risk_ok=True,
            reason="Approved",
            receipts=[],
            risk_mode="trigger",
        )
        self.store.save_retrieval_evaluations(
            audit_id,
            [
                {
                    "context_type": "similar_past_cases",
                    "rank_index": 1,
                    "trade_case_id": "BTCUSDT-case-9",
                    "case_symbol": "BTCUSDT",
                    "matched_symbol": "BTCUSDT",
                    "decision": "BUY",
                    "decision_symbol_aligned": True,
                    "estimated_pnl_pct": 0.7,
                    "similarity": 0.93,
                    "final_score": 1.42,
                    "retrieval_source": "band_index",
                    "matched_band_count": 4,
                    "risk_mode": "trigger",
                    "payload": {"thesis_failure_reason": "execution_quality_degraded:wide_spread"},
                }
            ],
        )

        rows = self.store.get_retrieval_evaluations_since(
            datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audit_log_id"], audit_id)
        self.assertEqual(rows[0]["trade_case_id"], "BTCUSDT-case-9")
        self.assertEqual(rows[0]["retrieval_source"], "band_index")
        self.assertEqual(rows[0]["matched_band_count"], 4)
        self.assertTrue(rows[0]["decision_symbol_aligned"])
        self.assertEqual(rows[0]["payload"]["thesis_failure_reason"], "execution_quality_degraded:wide_spread")

        self.store.update_retrieval_evaluations_for_trade_case(
            trade_case_id="BTCUSDT-live-case-1",
            case_symbol="BTCUSDT",
            entry_audit_log_id=audit_id,
            outcome_status="closed",
            outcome_realized_pnl=12.5,
            outcome_estimated_pnl_pct=0.7,
            outcome_holding_minutes=45.0,
            outcome_observed_mfe_pct=1.8,
            outcome_observed_mae_pct=-0.4,
            outcome_slippage_bps=3.2,
            outcome_updated_at="2026-03-07 11:00:00",
        )

        updated = self.store.get_retrieval_evaluations_since(
            datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        )
        self.assertEqual(updated[0]["outcome_trade_case_id"], "BTCUSDT-live-case-1")
        self.assertEqual(updated[0]["outcome_status"], "closed")
        self.assertEqual(updated[0]["outcome_realized_pnl"], 12.5)
        self.assertEqual(updated[0]["outcome_holding_minutes"], 45.0)

    def test_monitoring_snapshot_includes_recent_symbol_journal(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000},
            market={"market_snapshots": {"ETHUSDT": {"symbol": "ETHUSDT"}}},
            intent={"decision": "SELL"},
            risk_ok=True,
            reason="Approved with risk advisories",
            receipts=[],
            risk_mode="trigger",
        )
        self.store.save_symbol_journal_entries(
            [
                {
                    "audit_log_id": audit_id,
                    "symbol": "ETHUSDT",
                    "decision": "SELL",
                    "risk_reason": "Approved with risk advisories",
                    "risk_mode": "trigger",
                    "risk_approved": True,
                    "triggered": True,
                    "risk_triggered": False,
                    "estimated_pnl_pct": 0.2,
                    "net_realized_pnl": 1.5,
                    "evidence_signals": ["rsi_14@15m"],
                }
            ]
        )

        snapshot = self.store.get_monitoring_snapshot()

        self.assertEqual(len(snapshot["recent_symbol_journal"]), 1)
        row = snapshot["recent_symbol_journal"][0]
        self.assertEqual(row["audit_log_id"], audit_id)
        self.assertEqual(row["symbol"], "ETHUSDT")
        self.assertEqual(row["evidence_signals"], ["rsi_14@15m"])

    def test_get_recent_journals_includes_structured_evidence_summary(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000},
            market={
                "trigger_symbols": ["BTCUSDT"],
                "risk_trigger_symbols": ["BTCUSDT"],
                "market_snapshots": {
                    "BTCUSDT": {
                        "regime": {
                            "trend_alignment": "aligned_up",
                            "volatility_regime": "high",
                        }
                    }
                },
            },
            intent={"decision": "BUY", "orders": [], "confidence": 0.7},
            risk_ok=False,
            reason="Entry blocked",
            receipts=[],
            risk_mode="trigger",
            execution_summary={
                "evidence_quality": {
                    "factor": 0.7,
                    "issues": ["low_signal_diversity"],
                },
                "entry_size_cap": {
                    "base_cap_pct": 30.0,
                    "effective_cap_pct": 21.0,
                },
            },
        )
        self.store.save_position_ledger_entries(
            audit_id,
            [
                {
                    "symbol": "BTCUSDT",
                    "decision": "BUY",
                    "fill_count": 1,
                    "total_amount": 0.1,
                    "gross_realized_pnl": 0.0,
                    "fee_cost": 1.2,
                    "net_realized_pnl": -1.2,
                }
            ],
        )
        self.store.save_journal(
            decision="BUY",
            rationale=["Momentum confirmed on BTC."],
            audit_log_id=audit_id,
            rationale_items=[
                {
                    "summary": "Momentum confirmed",
                    "why_it_matters": "Trend and participation align.",
                    "trade_implication": "Allow controlled long entry.",
                    "failure_mode": "Breakout fails.",
                    "evidence": [
                        {
                            "signal": "volume_ratio",
                            "symbol": "BTCUSDT",
                            "timeframe": "15m",
                            "operator": "gte",
                            "observed_value": 2.4,
                            "threshold": 1.8,
                            "units": "ratio",
                            "source": "market_snapshots.BTCUSDT.timeframes.15m.volume_ratio",
                        }
                    ],
                }
            ],
            confidence=0.7,
            risk_reason="Approved",
            trade_case_ids=["BTCUSDT-case-2"],
            case_statuses={"BTCUSDT": "opened"},
            case_summary="BTCUSDT:opened",
        )

        recent = self.store.get_recent_journals(limit=1)

        self.assertEqual(len(recent), 1)
        self.assertIn("volume_ratio@15m=2.4 gte 1.8", recent[0])
        self.assertIn("ledger_net=-1.2000", recent[0])
        self.assertIn("fees=1.2000", recent[0])
        self.assertIn("eq=0.70", recent[0])
        self.assertIn("cap=30.0->21.0", recent[0])
        self.assertIn("trigger=BTCUSDT", recent[0])
        self.assertIn("risk_trigger=BTCUSDT", recent[0])
        self.assertIn("cases=BTCUSDT:opened", recent[0])

        context = self.store.get_recent_journal_context(limit=1)

        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["journal_id"], 1)
        self.assertEqual(context[0]["trigger_symbols"], ["BTCUSDT"])
        self.assertEqual(context[0]["risk_trigger_symbols"], ["BTCUSDT"])
        self.assertEqual(context[0]["evidence_quality"]["factor"], 0.7)
        self.assertEqual(context[0]["entry_size_cap"]["effective_cap_pct"], 21.0)
        self.assertEqual(context[0]["trade_case_ids"], ["BTCUSDT-case-2"])
        self.assertEqual(context[0]["case_statuses"]["BTCUSDT"], "opened")

    def test_get_audit_log_and_replay_candidates_decode_json_payloads(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000},
            market={"symbol": "BTCUSDT"},
            intent={"decision": "HOLD", "orders": [], "confidence": 0.2},
            risk_ok=True,
            reason="Safe action.",
            receipts=[{"status": "closed"}],
            decision_input_hash="hash-1",
            risk_mode="normal",
            risk_profile={"max_open_positions": 2},
            decision_request={"policy_summary": "x", "account_state": {}, "market_snapshot": {}},
            execution_summary={"receipt_summary": {"filled_orders": 1}},
            market_vectors={"BTCUSDT": [1.0, 0.5, 0.0]},
        )
        self.store.save_execution_fills(
            audit_id,
            [
                {
                    "timestamp": 1111,
                    "symbol": "BTCUSDT",
                    "order_id": "order-1",
                    "client_order_id": "client-1",
                    "trade_id": "trade-1",
                    "side": "buy",
                    "price": 30000,
                    "amount": 0.1,
                    "cost": 3000,
                    "fee_cost": 2.3,
                    "fee_currency": "USDT",
                    "realized_pnl": 1.7,
                }
            ],
        )

        audit = self.store.get_audit_log(audit_id)
        candidates = self.store.get_replay_candidates(limit=5)
        fill_stats = self.store.get_fill_stats()

        self.assertIsNotNone(audit)
        self.assertEqual(audit["intent"]["decision"], "HOLD")
        self.assertEqual(audit["receipts"][0]["status"], "closed")
        self.assertEqual(audit["execution_summary"]["receipt_summary"]["filled_orders"], 1)
        self.assertEqual(audit["fills"][0]["trade_id"], "trade-1")
        self.assertEqual(audit["decision_input_hash"], "hash-1")
        self.assertEqual(audit["risk_mode"], "normal")
        self.assertEqual(audit["risk_profile"]["max_open_positions"], 2)
        self.assertEqual(audit["market_vectors"]["BTCUSDT"], [1.0, 0.5, 0.0])
        self.assertEqual(candidates[0]["id"], audit_id)
        self.assertEqual(candidates[0]["intent"]["decision"], "HOLD")
        self.assertEqual(candidates[0]["market_vectors"]["BTCUSDT"], [1.0, 0.5, 0.0])
        self.assertEqual(fill_stats["fill_count"], 1)
        self.assertEqual(fill_stats["fee_cost"], 2.3)
        self.assertEqual(fill_stats["realized_pnl"], 1.7)

    def test_position_ledger_and_replay_runs_are_queryable(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000},
            market={"symbol": "ETHUSDT"},
            intent={"decision": "EXIT", "orders": [], "confidence": 0.9},
            risk_ok=True,
            reason="Approved",
            receipts=[],
        )
        self.store.save_position_ledger_entries(
            audit_id,
            [
                {
                    "symbol": "ETHUSDT",
                    "decision": "EXIT",
                    "fill_count": 2,
                    "total_amount": 1.5,
                    "gross_realized_pnl": 12.4,
                    "fee_cost": 1.1,
                    "net_realized_pnl": 11.3,
                    "balance_delta": 10.8,
                    "balance_delta_pct": 1.08,
                }
            ],
        )
        self.store.save_replay_run(
            audit_id,
            {
                "comparison": {
                    "original_decision": "EXIT",
                    "replayed_decision": "REDUCE",
                    "decision_changed": True,
                    "order_count_changed": False,
                    "side_changed": True,
                    "size_delta": -0.4,
                    "confidence_delta": -0.2,
                    "rationale_keywords_added": ["volatility"],
                    "rationale_keywords_removed": ["momentum"],
                    "evidence_signals_added": ["ETHUSDT:spread_bps@microstructure"],
                    "evidence_signals_removed": ["ETHUSDT:rsi_14@15m"],
                    "evidence_signal_count_delta": 0,
                }
            },
        )
        self.store.save_order_state_transitions(
            audit_id,
            [
                {
                    "symbol": "ETHUSDT",
                    "client_order_id": "client-77",
                    "exchange_order_id": "order-77",
                    "from_state": "none",
                    "to_state": "submitted",
                    "event_type": "intent_submitted",
                    "payload": {"quantity": 1.5},
                }
            ],
        )
        self.store.save_position_state_transitions(
            audit_id,
            [
                {
                    "symbol": "ETHUSDT",
                    "from_state": "open",
                    "to_state": "closed",
                    "event_type": "intent_exit",
                    "payload": {"before_size": 1.5, "after_size": 0.0},
                }
            ],
        )

        ledger_stats = self.store.get_position_ledger_stats()
        recent_ledger = self.store.get_recent_position_ledger(limit=5)
        recent_replays = self.store.get_recent_replay_runs(limit=5)
        audit = self.store.get_audit_log(audit_id)

        self.assertEqual(ledger_stats["row_count"], 1)
        self.assertEqual(ledger_stats["net_realized_pnl"], 11.3)
        self.assertEqual(recent_ledger[0]["symbol"], "ETHUSDT")
        self.assertEqual(recent_replays[0]["audit_log_id"], audit_id)
        self.assertTrue(recent_replays[0]["decision_changed"])
        self.assertTrue(recent_replays[0]["side_changed"])
        self.assertEqual(recent_replays[0]["rationale_keywords_added"], ["volatility"])
        self.assertEqual(recent_replays[0]["evidence_signals_added"], ["ETHUSDT:spread_bps@microstructure"])
        self.assertEqual(recent_replays[0]["evidence_signals_removed"], ["ETHUSDT:rsi_14@15m"])
        self.assertEqual(audit["replay_runs"][0]["replayed_decision"], "REDUCE")
        self.assertIn("payload", recent_replays[0])
        self.assertEqual(audit["ledger_entries"][0]["symbol"], "ETHUSDT")
        self.assertEqual(audit["order_state_transitions"][0]["to_state"], "submitted")
        self.assertEqual(audit["position_state_transitions"][0]["to_state"], "closed")

    def test_get_long_term_memory_returns_structured_conditions(self):
        self.store.save_long_term_memory_entries(
            [
                {
                    "rule": "Only fade spikes when higher timeframe trend is flat.",
                    "version": 1,
                    "metrics": {
                        "status": "rule",
                        "support": 5,
                        "category": "anti_pattern",
                        "regime": "mean_reversion",
                        "apply_when": {
                            "regime": ["mean_reversion"],
                            "volatility": ["high"],
                            "trend": ["flat"],
                        },
                        "ignore_when": ["Ignore in strong breakout continuation."],
                        "evidence_journal_ids": [1, 2, 3],
                    },
                }
            ]
        )

        memory = self.store.get_long_term_memory()

        self.assertEqual(memory[0]["rule"], "Only fade spikes when higher timeframe trend is flat.")
        self.assertEqual(memory[0]["apply_when"]["trend"], ["flat"])
        self.assertEqual(memory[0]["ignore_when"], ["Ignore in strong breakout continuation."])

    def test_save_long_term_memory_entries_updates_existing_rows(self):
        self.store.save_long_term_memory_entries(
            [
                {
                    "rule": "Keep size small during wide spreads.",
                    "version": 1,
                    "metrics": {
                        "status": "rule",
                        "support": 4,
                        "category": "risk_control",
                        "regime": "general",
                        "confidence_score": 0.41,
                        "validity_score": 0.41,
                        "retired": False,
                    },
                }
            ]
        )
        original = self.store.get_recent_long_term_memory(limit=5)[0]

        self.store.save_long_term_memory_entries(
            [
                {
                    "id": original["id"],
                    "rule": "Keep size smaller during wide spreads.",
                    "version": 2,
                    "metrics": {
                        "status": "rule",
                        "support": 6,
                        "category": "risk_control",
                        "regime": "general",
                        "confidence_score": 0.77,
                        "validity_score": 0.73,
                        "retired": False,
                    },
                }
            ]
        )

        rows = self.store.get_recent_long_term_memory(limit=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], original["id"])
        self.assertEqual(rows[0]["version"], 2)
        self.assertEqual(rows[0]["rule"], "Keep size smaller during wide spreads.")
        self.assertAlmostEqual(rows[0]["confidence_score"], 0.77)

    def test_get_risk_state_marks_drawdown_mode(self):
        self.store.save_audit(
            account={"total_balance": 1000},
            market={"symbol": "BTCUSDT"},
            intent={"decision": "HOLD"},
            risk_ok=True,
            reason="Safe action.",
            receipts=[],
        )

        risk_state = self.store.get_risk_state(
            current_balance=980,
            cooldown_minutes=30,
            drawdown_activate_pct=1.5,
        )

        self.assertTrue(risk_state["drawdown_active"])
        self.assertAlmostEqual(risk_state["daily_pnl_pct"], -2.0)

    def test_symbol_and_daily_reports_aggregate_ledger_rows(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000},
            market={"symbol": "BTCUSDT"},
            intent={"decision": "REDUCE", "orders": [], "confidence": 0.6},
            risk_ok=True,
            reason="Approved",
            receipts=[],
        )
        self.store.save_position_ledger_entries(
            audit_id,
            [
                {
                    "symbol": "BTCUSDT",
                    "decision": "REDUCE",
                    "fill_count": 2,
                    "total_amount": 0.2,
                    "gross_realized_pnl": 15.0,
                    "fee_cost": 1.0,
                    "net_realized_pnl": 14.0,
                    "balance_delta": 13.5,
                    "balance_delta_pct": 1.35,
                },
                {
                    "symbol": "ETHUSDT",
                    "decision": "EXIT",
                    "fill_count": 1,
                    "total_amount": 1.0,
                    "gross_realized_pnl": -4.0,
                    "fee_cost": 0.5,
                    "net_realized_pnl": -4.5,
                    "balance_delta": -4.0,
                    "balance_delta_pct": -0.4,
                },
            ],
        )

        symbol_report = self.store.get_symbol_performance_report(hours=24)
        daily_report = self.store.get_daily_ledger_report(days=7)

        self.assertEqual(len(symbol_report), 2)
        self.assertEqual(symbol_report[0]["symbol"], "BTCUSDT")
        self.assertEqual(symbol_report[0]["net_realized_pnl"], 14.0)
        self.assertEqual(symbol_report[1]["symbol"], "ETHUSDT")
        self.assertEqual(daily_report[0]["row_count"], 2)
        self.assertEqual(daily_report[0]["net_realized_pnl"], 9.5)

    def test_symbol_trigger_state_round_trip(self):
        self.store.save_symbol_trigger_state(
            "BTCUSDT",
            {"fast_price_move_15m_active": True},
        )

        state = self.store.get_symbol_trigger_state("BTCUSDT")
        missing = self.store.get_symbol_trigger_state("ETHUSDT")

        self.assertEqual(state["fast_price_move_15m_active"], True)
        self.assertEqual(missing, {})

    def test_decision_cache_round_trip(self):
        self.store.save_cached_decision(
            prompt_hash="prompt-hash-1",
            model_name="gemini-3.0-pro",
            input_hash="input-hash-1",
            raw_response='{"decision":"HOLD","orders":[],"rationale":["Cached"],"risk_notes":[],"confidence":0.5}',
            intent={
                "decision": "HOLD",
                "orders": [],
                "rationale": ["Cached"],
                "risk_notes": [],
                "confidence": 0.5,
            },
        )

        cached = self.store.get_cached_decision("prompt-hash-1")

        self.assertIsNotNone(cached)
        self.assertEqual(cached["model_name"], "gemini-3.0-pro")
        self.assertEqual(cached["input_hash"], "input-hash-1")
        self.assertEqual(cached["intent"]["decision"], "HOLD")
        self.assertEqual(cached["hit_count"], 1)

    def test_get_recent_rag_candidates_decodes_market_vectors(self):
        self.store.save_audit(
            account={"total_balance": 1000},
            market={"market_snapshots": {"BTCUSDT": {"symbol": "BTCUSDT"}}},
            intent={"decision": "BUY", "rationale": ["Momentum"]},
            risk_ok=True,
            reason="Approved",
            receipts=[],
            market_vectors={"BTCUSDT": [0.1, 0.2, 0.3]},
        )

        rows = self.store.get_recent_rag_candidates(limit=5)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["market_vectors"]["BTCUSDT"], [0.1, 0.2, 0.3])
        self.assertEqual(rows[0]["intent"]["decision"], "BUY")

    def test_symbol_journal_entries_round_trip(self):
        audit_id = self.store.save_audit(
            account={"total_balance": 1000},
            market={"market_snapshots": {"BTCUSDT": {"symbol": "BTCUSDT"}}},
            intent={"decision": "BUY"},
            risk_ok=True,
            reason="Approved with risk advisories: BTCUSDT: wide_spread=12.30bps",
            receipts=[],
            risk_mode="trigger",
        )
        self.store.save_symbol_journal_entries(
            [
                {
                    "audit_log_id": audit_id,
                    "symbol": "BTCUSDT",
                    "decision": "BUY",
                    "risk_reason": "Approved with risk advisories: BTCUSDT: wide_spread=12.30bps",
                    "risk_mode": "trigger",
                    "risk_approved": True,
                    "triggered": True,
                    "risk_triggered": True,
                    "net_realized_pnl": -2.5,
                    "fee_cost": 0.8,
                    "estimated_pnl_pct": -0.25,
                    "trade_case_id": "BTCUSDT-case-3",
                    "case_status": "opened",
                    "case_opened_at": "2026-03-07 12:00:00",
                    "holding_minutes": 5.5,
                    "evidence_signals": ["volume_ratio@15m", "spread_bps@microstructure"],
                    "summary": "Momentum setup with caution.",
                    "payload": {"order_actions": ["BUY:MARKET:0.01"]},
                }
            ]
        )

        rows = self.store.get_recent_symbol_journal(limit=5)
        filtered = self.store.get_recent_symbol_journal(limit=5, symbol="BTCUSDT")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["audit_log_id"], audit_id)
        self.assertEqual(rows[0]["symbol"], "BTCUSDT")
        self.assertEqual(rows[0]["risk_mode"], "trigger")
        self.assertEqual(rows[0]["triggered"], 1)
        self.assertEqual(rows[0]["risk_triggered"], 1)
        self.assertEqual(rows[0]["evidence_signals"], ["volume_ratio@15m", "spread_bps@microstructure"])
        self.assertEqual(rows[0]["payload"]["order_actions"], ["BUY:MARKET:0.01"])
        self.assertEqual(rows[0]["trade_case_id"], "BTCUSDT-case-3")
        self.assertEqual(rows[0]["case_status"], "opened")
        self.assertEqual(rows[0]["case_opened_at"], "2026-03-07 12:00:00")
        self.assertAlmostEqual(rows[0]["holding_minutes"], 5.5)
        self.assertEqual(len(filtered), 1)

        since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
        since_rows = self.store.get_symbol_journal_entries_since(since)
        self.assertEqual(len(since_rows), 1)
        self.assertEqual(since_rows[0]["symbol"], "BTCUSDT")

    def test_active_trade_case_round_trip_and_close(self):
        self.store.upsert_active_trade_case(
            symbol="BTCUSDT",
            trade_case_id="BTCUSDT-case-4",
            opened_at="2026-03-07 13:00:00",
            entry_decision="BUY",
            last_decision="BUY",
            last_audit_log_id=10,
            payload={"opened_via": "intent_buy"},
        )

        active = self.store.get_active_trade_case("BTCUSDT")
        closed = self.store.close_active_trade_case(
            symbol="BTCUSDT",
            closed_at="2026-03-07 13:45:00",
            last_decision="EXIT",
            last_audit_log_id=11,
        )
        missing = self.store.get_active_trade_case("BTCUSDT")

        self.assertEqual(active["trade_case_id"], "BTCUSDT-case-4")
        self.assertEqual(active["entry_decision"], "BUY")
        self.assertEqual(active["payload"]["opened_via"], "intent_buy")
        self.assertEqual(closed["trade_case_id"], "BTCUSDT-case-4")
        self.assertEqual(closed["opened_at"], "2026-03-07 13:00:00")
        self.assertEqual(missing, {})

    def test_trade_case_round_trip_and_query(self):
        self.store.upsert_trade_case(
            {
                "trade_case_id": "BTCUSDT-case-5",
                "symbol": "BTCUSDT",
                "status": "closed",
                "risk_mode": "trigger",
                "opened_at": "2026-03-07 13:00:00",
                "closed_at": "2026-03-07 13:20:00",
                "entry_audit_log_id": 15,
                "exit_audit_log_id": 16,
                "entry_decision": "BUY",
                "exit_decision": "EXIT",
                "entry_side": "LONG",
                "entry_price": 30000.0,
                "exit_price": 30120.0,
                "estimated_pnl_pct": 0.4,
                "realized_pnl": 12.5,
                "fee_cost": 1.1,
                "holding_minutes": 20.0,
                "observed_mfe_pct": 1.3,
                "observed_mae_pct": -0.6,
                "slippage_cost": 0.9,
                "slippage_bps": 3.0,
                "thesis_failure_reason": None,
                "evidence_signals": ["volume_ratio@15m"],
                "trigger_reasons": ["range_break_1h"],
                "risk_trigger_reasons": [],
                "feature_vector": [30000.0, 120.0, 240.0],
                "payload": {"regime_tags": ["trend:aligned_up"]},
            }
        )

        self.store.save_trade_case_snapshots(
            [
                {
                    "trade_case_id": "BTCUSDT-case-5",
                    "timestamp": "2026-03-07 13:05:00",
                    "audit_log_id": 15,
                    "symbol": "BTCUSDT",
                    "current_price": 30050.0,
                    "mark_price": 30052.0,
                    "mid_price": 30051.0,
                    "spread_bps": 1.2,
                    "volume_ratio_15m": 2.0,
                    "unrealized_pnl_pct": 0.8,
                    "payload": {"case_status": "active"},
                }
            ]
        )

        row = self.store.get_trade_case("BTCUSDT-case-5")
        recent = self.store.get_recent_trade_cases(limit=5)
        since = self.store.get_trade_cases_since(datetime(2026, 3, 7, 12, 0, 0))
        snapshots = self.store.get_trade_case_snapshots("BTCUSDT-case-5")
        bounded_snapshots = self.store.get_trade_case_snapshots_between(
            "BTCUSDT-case-5",
            start_timestamp="2026-03-07 13:00:00",
            end_timestamp="2026-03-07 13:10:00",
        )
        vector_candidates = self.store.get_trade_case_vector_candidates(limit=5, symbols=["BTCUSDT"])
        band_candidates = self.store.get_trade_case_vector_band_candidates(
            current_feature_vectors={"BTCUSDT": [30000.0, 120.0, 240.0]},
            limit=5,
        )

        self.assertEqual(row["symbol"], "BTCUSDT")
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["evidence_signals"], ["volume_ratio@15m"])
        self.assertEqual(row["feature_vector"], [30000.0, 120.0, 240.0])
        self.assertEqual(row["payload"]["regime_tags"], ["trend:aligned_up"])
        self.assertEqual(recent[0]["trade_case_id"], "BTCUSDT-case-5")
        self.assertEqual(since[0]["trade_case_id"], "BTCUSDT-case-5")
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(len(bounded_snapshots), 1)
        self.assertEqual(snapshots[0]["payload"]["case_status"], "active")
        self.assertEqual(vector_candidates[0]["trade_case_id"], "BTCUSDT-case-5")
        self.assertEqual(vector_candidates[0]["feature_vector"], [30000.0, 120.0, 240.0])
        self.assertEqual(band_candidates[0]["trade_case_id"], "BTCUSDT-case-5")
        self.assertGreaterEqual(band_candidates[0]["matched_band_count"], 1)

    def test_symbol_journal_indexes_exist(self):
        rows = self.store.conn.execute("PRAGMA index_list('symbol_journal')").fetchall()
        index_names = {row["name"] for row in rows}

        self.assertIn("idx_symbol_journal_symbol_id", index_names)
        self.assertIn("idx_symbol_journal_timestamp_id", index_names)
        self.assertIn("idx_symbol_journal_audit_log_id", index_names)

    def test_runtime_lock_blocks_other_owner_until_release(self):
        acquired_a = self.store.try_acquire_runtime_lock("loop_lock", owner="worker-a", ttl_seconds=60)
        acquired_b_before_release = self.store.try_acquire_runtime_lock("loop_lock", owner="worker-b", ttl_seconds=60)

        self.store.release_runtime_lock("loop_lock", owner="worker-a")
        acquired_b_after_release = self.store.try_acquire_runtime_lock("loop_lock", owner="worker-b", ttl_seconds=60)

        self.assertTrue(acquired_a)
        self.assertFalse(acquired_b_before_release)
        self.assertTrue(acquired_b_after_release)

    def test_entry_mutex_blocks_duplicate_symbol_entry(self):
        acquired_a = self.store.acquire_entry_mutex("BTCUSDT", owner="worker-a", ttl_seconds=60)
        acquired_b_before_release = self.store.acquire_entry_mutex("BTCUSDT", owner="worker-b", ttl_seconds=60)

        self.store.release_entry_mutex("BTCUSDT", owner="worker-a")
        acquired_b_after_release = self.store.acquire_entry_mutex("BTCUSDT", owner="worker-b", ttl_seconds=60)

        self.assertTrue(acquired_a)
        self.assertFalse(acquired_b_before_release)
        self.assertTrue(acquired_b_after_release)

    def test_decision_fingerprint_and_unknown_runtime_state_queries(self):
        self.store.save_decision_fingerprints(
            [
                {
                    "symbol": "BTCUSDT",
                    "action": "BUY",
                    "size_band": "small",
                    "fingerprint": "fp-1",
                    "status": "planned",
                }
            ]
        )
        has_recent = self.store.has_recent_decision_fingerprint(
            symbol="BTCUSDT",
            action="BUY",
            size_band="small",
            within_minutes=10,
        )

        self.store.save_order_state_transitions(
            audit_log_id=1,
            entries=[
                {
                    "symbol": "BTCUSDT",
                    "client_order_id": "cid-1",
                    "exchange_order_id": "oid-1",
                    "from_state": "none",
                    "to_state": "submitted",
                    "event_type": "intent_submitted",
                    "payload": {},
                }
            ],
        )
        self.store.save_order_state_transitions(
            audit_log_id=2,
            entries=[
                {
                    "symbol": "BTCUSDT",
                    "client_order_id": "cid-1",
                    "exchange_order_id": "oid-1",
                    "from_state": "submitted",
                    "to_state": "completed",
                    "event_type": "exchange_update",
                    "payload": {},
                }
            ],
        )
        self.store.save_order_state_transitions(
            audit_log_id=3,
            entries=[
                {
                    "symbol": "BTCUSDT",
                    "client_order_id": "cid-1",
                    "exchange_order_id": "oid-1",
                    "from_state": "completed",
                    "to_state": "submitted",
                    "event_type": "invalid_regression",
                    "payload": {},
                }
            ],
        )

        # Ensure CURRENT_TIMESTAMP has advanced in SQLite before querying lookback windows.
        time.sleep(0.01)
        unknown_count = self.store.count_recent_unknown_orders(minutes=30)
        unknown_rows = self.store.get_unknown_order_runtime_states(limit=10)

        self.assertTrue(has_recent)
        self.assertGreaterEqual(unknown_count, 1)
        self.assertEqual(unknown_rows[0]["state"], "unknown")
        self.assertEqual(unknown_rows[0]["client_order_id"], "cid-1")


if __name__ == "__main__":
    unittest.main()
