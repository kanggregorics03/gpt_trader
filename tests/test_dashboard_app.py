import unittest

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
                "last_24h": {"failure_count": 1, "top_issues": [{"issue": "schema", "count": 1}], "top_models": []},
                "last_7d": {"failure_count": 2, "top_models": []},
                "latest_50": {"failure_count": 1, "top_models": []},
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

    def get_recent_trade_cases(self, limit=10):
        return [{"trade_case_id": "case-1", "symbol": "BTCUSDT", "status": "closed", "realized_pnl": 9.5, "holding_minutes": 45.0, "observed_mfe_pct": 2.0, "observed_mae_pct": -0.5, "slippage_bps": 1.2, "thesis_failure_reason": None}]

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
        response = self.client.get("/api/stats")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("trade_case_summary", payload)
        self.assertIn("retrieval_summary", payload)
        self.assertIn("memory_summary", payload)
        self.assertIn("recent_trade_cases", payload)
        self.assertIn("recent_retrieval_evaluations", payload)
        self.assertEqual(payload["trade_case_summary"]["total_count"], 2)
        self.assertEqual(payload["retrieval_summary"]["settled_count"], 1)
        self.assertEqual(payload["memory_summary"]["active_count"], 1)

    def test_audit_detail_endpoint_includes_linked_trade_case_and_retrieval_data(self):
        response = self.client.get("/api/audits/101")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("symbol_journal_entries", payload)
        self.assertIn("trade_cases", payload)
        self.assertIn("retrieval_evaluations", payload)
        self.assertEqual(payload["trade_cases"][0]["trade_case_id"], "case-1")


if __name__ == "__main__":
    unittest.main()
