import tempfile
import unittest
from pathlib import Path

from ai.rag_engine import RagEngine
from db.state_store import StateStore


class TestRagEngine(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "state.db")
        self.store = StateStore(self.db_path)
        self.engine = RagEngine(self.db_path, candidate_cache_ttl_ms=60_000)

    def tearDown(self):
        self.store.close()
        self.tmpdir.cleanup()

    def test_get_similar_cases_batch_returns_symbol_scoped_results(self):
        self.store.save_audit(
            account={"total_balance": 1000},
            market={
                "market_snapshots": {
                    "BTCUSDT": {
                        "symbol": "BTCUSDT",
                        "atr_15m": 100.0,
                        "atr_1h": 200.0,
                        "trend_1h": "UP",
                        "recent_volume_surge": True,
                    },
                    "ETHUSDT": {
                        "symbol": "ETHUSDT",
                        "atr_15m": 10.0,
                        "atr_1h": 20.0,
                        "trend_1h": "DOWN",
                        "recent_volume_surge": False,
                    },
                }
            },
            intent={"decision": "BUY", "rationale": ["Momentum continuation"]},
            risk_ok=True,
            reason="Approved",
            receipts=[],
            market_vectors={
                "BTCUSDT": [100.0, 200.0, 1.0, 1.0, 0.4, 0.8, 1.2, 65.0, 62.0, 0.55, 0.0002, 2.1, 1.0],
                "ETHUSDT": [10.0, 20.0, -1.0, 0.0, -0.3, -0.5, -0.8, 40.0, 42.0, 0.45, -0.0001, -1.2, -1.0],
            },
        )

        results = self.engine.get_similar_cases_batch(
            {
                "BTCUSDT": {
                    "symbol": "BTCUSDT",
                    "atr_15m": 100.0,
                    "atr_1h": 200.0,
                    "trend_1h": "UP",
                    "recent_volume_surge": True,
                    "timeframes": {
                        "15m": {"change_3bar_pct": 0.4, "rsi_14": 65.0},
                        "1h": {"change_3bar_pct": 0.8, "rsi_14": 62.0},
                        "4h": {"change_3bar_pct": 1.2},
                    },
                    "order_book": {"imbalance_top5": 0.55},
                    "derivatives": {"funding_rate": 0.0002},
                    "price_change_24h_pct": 2.1,
                    "regime": {"trend_alignment": "bullish"},
                },
                "ETHUSDT": {
                    "symbol": "ETHUSDT",
                    "atr_15m": 10.0,
                    "atr_1h": 20.0,
                    "trend_1h": "DOWN",
                    "recent_volume_surge": False,
                    "timeframes": {
                        "15m": {"change_3bar_pct": -0.3, "rsi_14": 40.0},
                        "1h": {"change_3bar_pct": -0.5, "rsi_14": 42.0},
                        "4h": {"change_3bar_pct": -0.8},
                    },
                    "order_book": {"imbalance_top5": 0.45},
                    "derivatives": {"funding_rate": -0.0001},
                    "price_change_24h_pct": -1.2,
                    "regime": {"trend_alignment": "bearish"},
                },
            },
            limit_per_symbol=2,
        )

        self.assertEqual(len(results["BTCUSDT"]), 1)
        self.assertIn("BUY", results["BTCUSDT"][0])
        self.assertEqual(len(results["ETHUSDT"]), 1)
        self.assertIn("BUY", results["ETHUSDT"][0])

    def test_get_similar_cases_falls_back_to_legacy_market_state(self):
        self.store.save_audit(
            account={"total_balance": 1000},
            market={
                "symbol": "BTCUSDT",
                "atr_15m": 100.0,
                "atr_1h": 200.0,
                "trend_1h": "UP",
                "recent_volume_surge": True,
                "timeframes": {
                    "15m": {"change_3bar_pct": 0.4, "rsi_14": 65.0},
                    "1h": {"change_3bar_pct": 0.8, "rsi_14": 62.0},
                    "4h": {"change_3bar_pct": 1.2},
                },
                "order_book": {"imbalance_top5": 0.55},
                "derivatives": {"funding_rate": 0.0002},
                "price_change_24h_pct": 2.1,
                "regime": {"trend_alignment": "bullish"},
            },
            intent={"decision": "HOLD", "rationale": ["Legacy"]},
            risk_ok=True,
            reason="Approved",
            receipts=[],
        )

        cases = self.engine.get_similar_cases(
            {
                "symbol": "BTCUSDT",
                "atr_15m": 100.0,
                "atr_1h": 200.0,
                "trend_1h": "UP",
                "recent_volume_surge": True,
                "timeframes": {
                    "15m": {"change_3bar_pct": 0.4, "rsi_14": 65.0},
                    "1h": {"change_3bar_pct": 0.8, "rsi_14": 62.0},
                    "4h": {"change_3bar_pct": 1.2},
                },
                "order_book": {"imbalance_top5": 0.55},
                "derivatives": {"funding_rate": 0.0002},
                "price_change_24h_pct": 2.1,
                "regime": {"trend_alignment": "bullish"},
            },
            limit=2,
        )

        self.assertEqual(len(cases), 1)
        self.assertIn("HOLD", cases[0])
