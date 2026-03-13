import unittest
from datetime import datetime
from unittest.mock import patch

from dashboard import app as dashboard_app


class FakeStateStore:
    def get_monitoring_snapshot(self, now=None):
        return {
            "total_audits_24h": 8,
            "rejected_24h": 2,
            "loop_errors_24h": 1,
            "timeouts_unresolved_24h": 0,
            "fee_cost_24h": 1.25,
            "ledger_net_pnl_24h": 12.5,
            "recent_logs": [
                {
                    "id": 101,
                    "timestamp": "2026-03-07 10:00:00",
                    "intent": {"decision": "BUY"},
                    "risk_approved": 1,
                    "risk_reason": "",
                }
            ],
            "semantic_validation_summary": {
                "last_24h": {
                    "failure_count": 1,
                    "safe_fallback_count": 1,
                    "explanation_fallback_count": 1,
                    "action_schema_fallback_count": 0,
                    "json_parse_fallback_count": 0,
                    "top_issues": [{"issue": "schema", "count": 1}],
                    "top_models": [],
                    "top_validation_stages": [{"stage": "explanation_semantic", "count": 1}],
                },
                "last_7d": {"failure_count": 2, "safe_fallback_count": 1, "top_models": []},
                "latest_50": {"failure_count": 1, "safe_fallback_count": 1, "top_models": []},
            },
            "hold_quality_summary_7d": {
                "total_hold_count": 5,
                "good_hold_count": 2,
                "bad_hold_count": 1,
                "fallback_hold_count": 1,
                "unlabeled_hold_count": 1,
                "evaluated_hold_count": 4,
            },
            "replay_learning_summary_7d": {
                "evaluation_count": 3,
                "outcome_linked_count": 2,
                "with_learning_better_count": 1,
                "without_learning_better_count": 1,
                "tie_count": 0,
                "decision_changed_count": 2,
                "avg_outcome_utility_delta": 0.15,
            },
            "label_quality_summary_7d": {
                "tracked_total": 4,
                "coverage_rate": 0.8,
                "outcome_linked_rate": 0.6,
                "label_counts": {
                    "edge_failed": 1,
                    "missed_due_to_no_fill": 1,
                    "risk_exit": 1,
                    "stale_entry_cancelled": 1,
                },
            },
            "context_bias_summary": {
                "last_24h": {
                    "journal_selected": {
                        "total": 10,
                        "group_counts": {
                            "good_hold": 2,
                            "missed_opportunity_hold": 1,
                            "action": 6,
                            "fallback": 1,
                        },
                        "group_ratios": {
                            "good_hold": 0.2,
                            "missed_opportunity_hold": 0.1,
                            "action": 0.6,
                            "fallback": 0.1,
                        },
                    }
                }
            },
            "learning_liveness": {
                "stale": False,
                "promotion_blocked": False,
                "replay_candidate_count_24h": 5,
                "replay_run_count_24h": 4,
                "replay_processed_rate_24h": 0.8,
                "stale_reasons": [],
            },
            "recent_memory": [],
            "recent_symbol_journal": [],
            "recent_fills": [],
            "recent_ledger": [],
            "recent_replays": [],
            "recent_order_states": [],
            "recent_position_states": [],
        }

    def get_symbol_performance_report(self, hours=24, now=None):
        return [{"symbol": "BTCUSDT", "net_realized_pnl": 10.0, "gross_realized_pnl": 11.0, "fee_cost": 1.0, "fill_count": 2, "win_rate": 50.0}]

    def get_daily_ledger_report(self, days=7, now=None):
        return [{"day": "2026-03-07", "net_realized_pnl": 12.5, "gross_realized_pnl": 13.0, "fee_cost": 0.5, "row_count": 2}]

    def get_recent_trade_cases(self, limit=10, status=None):
        rows = [
            {"trade_case_id": "case-3", "symbol": "SOLUSDT", "status": "closed", "realized_pnl": -1.2, "holding_minutes": 25.0, "observed_mfe_pct": 0.8, "observed_mae_pct": -1.1, "slippage_bps": 1.7, "thesis_failure_reason": "stopped_out"},
            {"trade_case_id": "case-2", "symbol": "ETHUSDT", "status": "active", "estimated_pnl_pct": 0.4, "holding_minutes": 10.0, "observed_mfe_pct": None, "observed_mae_pct": None, "slippage_bps": None, "thesis_failure_reason": None, "exit_reason": None, "opened_at": "2026-03-07 09:00:00", "closed_at": None},
            {"trade_case_id": "case-1", "symbol": "BTCUSDT", "status": "closed", "realized_pnl": 9.5, "holding_minutes": 45.0, "observed_mfe_pct": 2.0, "observed_mae_pct": -0.5, "slippage_bps": 1.2, "thesis_failure_reason": None, "exit_reason": "risk_exit", "opened_at": "2026-03-06 09:00:00", "closed_at": "2026-03-07 10:00:00"},
        ]
        if status:
            rows = [row for row in rows if row["status"] == status]
        return rows[:limit]

    def get_all_active_trade_cases(self):
        return [{"trade_case_id": "case-live", "symbol": "ETHUSDT"}]

    def get_retrieval_evaluations_since(self, since, context_type=None, limit=None):
        return [
            {
                "id": 1,
                "audit_log_id": 101,
                "context_type": "similar_past_cases",
                "rank_index": 0,
                "case_symbol": "BTCUSDT",
                "matched_symbol": "BTCUSDT",
                "decision_symbol_aligned": 1,
                "similarity": 0.82,
                "final_score": 0.91,
                "retrieval_source": "band_index",
                "matched_band_count": 3,
                "outcome_status": "closed",
                "outcome_realized_pnl": 8.4,
                "outcome_estimated_pnl_pct": 1.5,
            }
        ]

    def get_recent_long_term_memory(self, limit=12):
        return [
            {
                "rule": "Prefer breakout continuation with volume support",
                "support": 4,
                "regime": "bullish",
                "apply_when": {"trend": ["UP"]},
                "ignore_when": ["Wide spread"],
                "confidence_score": 0.78,
                "validity_score": 0.73,
                "retired": False,
                "lifecycle_status": "active",
                "merge_count": 1,
                "revive_count": 0,
                "retirement_reason": None,
            }
        ]

    def get_trade_cases_since(self, since, limit=None):
        return [
            {"trade_case_id": "case-1", "symbol": "BTCUSDT", "status": "closed", "realized_pnl": 9.5, "holding_minutes": 45.0},
            {"trade_case_id": "case-2", "symbol": "ETHUSDT", "status": "active", "estimated_pnl_pct": 0.4, "holding_minutes": 10.0},
        ]

    def get_recent_audits(self, limit=20, since=None):
        return [
            {
                "id": 101,
                "execution_summary": {
                    "execution_policy": {
                        "orders": [
                            {
                                "action_type": "entry",
                                "intent_order_type": "MARKET",
                                "entry_style": "immediate",
                                "order_type": "MARKET",
                                "execution_profile": "market_urgent",
                                "contract_adjustment": "market_entry_kept_immediate",
                            }
                        ]
                    }
                },
            },
            {
                "id": 102,
                "execution_summary": {
                    "execution_policy": {
                        "orders": [
                            {
                                "action_type": "reduce",
                                "intent_order_type": "LIMIT",
                                "entry_style": None,
                                "order_type": "MARKET",
                                "execution_profile": "market_urgent",
                                "contract_adjustment": "reduce_market_only",
                            }
                        ]
                    }
                },
            },
        ]

    def get_execution_policy_evaluations_since(self, since: datetime):
        return [
            {
                "symbol": "BTCUSDT",
                "action_type": "entry",
                "entry_style": "immediate",
                "filled_quantity": 0.01,
                "outcome_trade_case_id": "case-1",
                "outcome_estimated_pnl_pct": None,
                "outcome_realized_pnl": None,
                "outcome_label": None,
                "payload": {},
            },
            {
                "symbol": "ETHUSDT",
                "action_type": "entry",
                "entry_style": "value",
                "filled_quantity": 0.0,
                "outcome_trade_case_id": None,
                "outcome_estimated_pnl_pct": None,
                "outcome_realized_pnl": None,
                "outcome_label": "missed_due_to_no_fill",
                "payload": {},
            },
            {
                "symbol": "BTCUSDT",
                "action_type": "entry",
                "entry_style": "retest",
                "filled_quantity": 0.01,
                "outcome_trade_case_id": "case-2",
                "outcome_estimated_pnl_pct": -0.6,
                "outcome_realized_pnl": None,
                "outcome_label": "edge_failed",
                "payload": {},
            },
            {
                "symbol": "BTCUSDT",
                "action_type": "cancel",
                "entry_style": None,
                "filled_quantity": 0.0,
                "outcome_trade_case_id": None,
                "outcome_estimated_pnl_pct": None,
                "outcome_realized_pnl": None,
                "outcome_label": "stale_entry_cancelled",
                "payload": {},
            },
            {
                "symbol": "BTCUSDT",
                "action_type": "exit",
                "entry_style": None,
                "filled_quantity": 0.1,
                "outcome_trade_case_id": "case-1",
                "outcome_estimated_pnl_pct": None,
                "outcome_realized_pnl": 9.5,
                "outcome_label": "risk_exit",
                "payload": {},
            },
        ]

    def get_trade_case(self, trade_case_id):
        if trade_case_id == "case-1":
            return {"trade_case_id": "case-1", "status": "closed", "estimated_pnl_pct": 1.25, "thesis_failure_reason": None}
        if trade_case_id == "case-2":
            return {"trade_case_id": "case-2", "status": "closed", "estimated_pnl_pct": -0.6, "thesis_failure_reason": "edge_failed"}
        return {}

    def get_audit_log(self, audit_id):
        if audit_id != 101:
            return None
        return {
            "id": audit_id,
            "decision_input_hash": "hash-101",
            "decision_request": {"market_context": {"BTCUSDT": {}}},
            "execution_summary": {"evidence_quality": {}},
            "risk_profile": {},
            "intent": {"decision": "BUY", "rationale_items": []},
            "ai_raw_response": "raw",
            "fills": [],
            "ledger_entries": [],
            "replay_runs": [],
            "order_state_transitions": [],
            "position_state_transitions": [],
            "symbol_journal_entries": [{"symbol": "BTCUSDT", "trade_case_id": "case-1"}],
            "trade_cases": [{"trade_case_id": "case-1", "symbol": "BTCUSDT", "status": "closed"}],
            "retrieval_evaluations": [{"trade_case_id": "case-1", "similarity": 0.82}],
        }


class TestDashboardApp(unittest.TestCase):
    def setUp(self):
        self.original_state_store = dashboard_app.state_store
        dashboard_app.state_store = FakeStateStore()
        self.client = dashboard_app.app.test_client()

    def tearDown(self):
        dashboard_app.state_store = self.original_state_store

    def test_stats_endpoint_includes_trade_case_retrieval_and_memory_summaries(self):
        with patch.dict(
            "os.environ",
            {
                "AI_PROVIDER": "openai",
                "AI_DEFAULT_MODEL": "gpt-4.1-mini",
                "AI_GUARDED_ATTENTION_MODEL": "gpt-4.1",
                "MEMORY_PROVIDER": "anthropic",
                "MEMORY_MODEL": "claude-3.7-sonnet",
                "REPLAY_PROVIDER": "deepseek",
                "REPLAY_MODEL": "deepseek-chat",
            },
            clear=False,
        ):
            response = self.client.get("/api/stats")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("trade_case_summary", payload)
        self.assertIn("retrieval_summary", payload)
        self.assertIn("memory_summary", payload)
        self.assertIn("execution_contract_summary", payload)
        self.assertIn("entry_style_summary", payload)
        self.assertIn("post_trade_outcome_summary", payload)
        self.assertIn("context_bias_summary", payload)
        self.assertIn("learning_liveness", payload)
        self.assertIn("recent_trade_cases", payload)
        self.assertIn("recent_retrieval_evaluations", payload)
        self.assertEqual(payload["models"]["provider"], "openai")
        self.assertEqual(payload["models"]["default_model"], "gpt-4.1-mini")
        self.assertEqual(payload["models"]["guarded_attention_model"], "gpt-4.1")
        self.assertEqual(payload["models"]["memory_provider"], "anthropic")
        self.assertEqual(payload["models"]["memory_model"], "claude-3.7-sonnet")
        self.assertEqual(payload["models"]["replay_provider"], "deepseek")
        self.assertEqual(payload["models"]["replay_model"], "deepseek-chat")
        self.assertEqual(payload["trade_case_summary"]["total_count"], 2)
        self.assertEqual(payload["retrieval_summary"]["settled_count"], 1)
        self.assertEqual(payload["memory_summary"]["active_count"], 1)
        self.assertEqual(payload["execution_contract_summary"]["total_adjustments"], 2)
        self.assertEqual(payload["execution_contract_summary"]["governed_orders"], 2)
        self.assertEqual(payload["execution_contract_summary"]["immediate_entry_orders"], 1)
        self.assertEqual(payload["execution_contract_summary"]["reduce_exit_orders"], 1)
        self.assertEqual(payload["execution_contract_summary"]["top_adjustment"], "market_entry_kept_immediate")
        self.assertEqual(payload["entry_style_summary"]["total_entries"], 3)
        self.assertEqual(payload["entry_style_summary"]["styles"][0]["style"], "immediate")
        self.assertEqual(payload["entry_style_summary"]["styles"][0]["win_count"], 1)
        self.assertEqual(payload["entry_style_summary"]["styles"][1]["entry_count"], 1)
        self.assertEqual(payload["entry_style_summary"]["styles"][2]["missed_move_count"], 1)
        self.assertEqual(payload["post_trade_outcome_summary"]["label_counts"]["edge_failed"], 1)
        self.assertEqual(payload["post_trade_outcome_summary"]["label_counts"]["missed_due_to_no_fill"], 1)
        self.assertEqual(payload["context_bias_summary"]["last_24h"]["journal_selected"]["group_counts"]["action"], 6)
        self.assertEqual(payload["semantic_validation_summary"]["last_24h"]["safe_fallback_count"], 1)
        self.assertEqual(payload["semantic_validation_summary"]["last_24h"]["explanation_fallback_count"], 1)
        self.assertEqual(payload["hold_quality_summary_7d"]["good_hold_count"], 2)
        self.assertEqual(payload["replay_learning_summary_7d"]["evaluation_count"], 3)
        self.assertEqual(payload["label_quality_summary_7d"]["tracked_total"], 4)
        self.assertEqual(payload["label_quality_summary_7d"]["label_counts"]["risk_exit"], 1)

    def test_audit_detail_endpoint_includes_linked_trade_case_and_retrieval_data(self):
        response = self.client.get("/api/audits/101")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("symbol_journal_entries", payload)
        self.assertIn("trade_cases", payload)
        self.assertIn("retrieval_evaluations", payload)
        self.assertEqual(payload["trade_cases"][0]["trade_case_id"], "case-1")

    def test_trade_cases_endpoint_can_return_full_history(self):
        response = self.client.get("/api/trade-cases?all=1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["full_history"])
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["trade_cases"][0]["trade_case_id"], "case-3")

    def test_trade_cases_endpoint_supports_filters(self):
        response = self.client.get("/api/trade-cases?all=1&status=closed&symbol=BTCUSDT&exit_reason=risk")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["trade_cases"][0]["trade_case_id"], "case-1")

    def test_audit_replay_endpoint_uses_replay_engine_and_payload_options(self):
        fake_engine = object()
        with (
            patch.object(dashboard_app, "build_replay_engine", return_value=fake_engine) as mock_build_engine,
            patch.object(
                dashboard_app,
                "replay_and_optionally_save",
                return_value={"status": "ok"},
            ) as mock_replay,
        ):
            response = self.client.post(
                "/api/audits/101/replay",
                json={"save": False, "include_counterfactual": False},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})
        mock_build_engine.assert_called_once_with()
        mock_replay.assert_called_once_with(
            dashboard_app.state_store,
            audit_id=101,
            engine=fake_engine,
            save=False,
            include_counterfactual=False,
        )

    def test_runtime_freshness_summary_handles_timezone_aware_timestamps(self):
        now = datetime(2026, 3, 13, 12, 0, 0)
        latest_audit = {
            "timestamp": "2026-03-13T11:45:00+00:00",
            "account_state": {"snapshot_timestamp": 1773402300},
        }
        snapshot = {
            "recent_order_states": [{"created_at": "2026-03-13T11:40:00Z"}],
            "recent_position_states": [{"created_at": "2026-03-13 11:35:00"}],
        }

        result = dashboard_app._build_runtime_freshness_summary(now, latest_audit, snapshot)

        self.assertEqual(result["freshness_state"], "fresh")
        self.assertEqual(result["snapshot_age_minutes"], 15.0)
        self.assertEqual(result["latest_audit_age_minutes"], 15.0)
        self.assertEqual(result["latest_order_state_age_minutes"], 20.0)
        self.assertEqual(result["latest_position_state_age_minutes"], 25.0)


if __name__ == "__main__":
    unittest.main()
