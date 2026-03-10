import unittest

from core.alpha_policy import AlphaPolicyEngine
from core.models import MarketSnapshot


class DummyStore:
    def __init__(self, config=None):
        self.config = dict(config or {})

    def get_policy_registry_entry(self, policy_key):
        if policy_key != "champion:alpha_1h" or not self.config:
            return {}
        return {
            "policy_key": policy_key,
            "config": dict(self.config),
        }


class TestAlphaPolicyEngine(unittest.TestCase):
    def test_filter_market_snapshots_blocks_symbols_outside_thresholds(self):
        engine = AlphaPolicyEngine(policy={"alpha_policy": {"min_volume_ratio_15m": 1.3}}, state_store=DummyStore({"min_volume_ratio_15m": 1.3}))
        snapshots = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=30000.0,
                atr_15m=100.0,
                atr_1h=200.0,
                trend_1h="UP",
                recent_volume_surge=False,
                volume_ratio_15m=0.9,
            ),
            "ETHUSDT": MarketSnapshot(
                symbol="ETHUSDT",
                current_price=2000.0,
                atr_15m=20.0,
                atr_1h=40.0,
                trend_1h="UP",
                recent_volume_surge=False,
                volume_ratio_15m=1.8,
            ),
        }

        allowed, blocked = engine.filter_market_snapshots(snapshots)

        self.assertEqual(list(allowed.keys()), ["ETHUSDT"])
        self.assertIn("BTCUSDT", blocked)
        self.assertIn("volume_ratio_15m<1.3", blocked["BTCUSDT"][0])

    def test_would_block_symbol_returns_false_without_match(self):
        engine = AlphaPolicyEngine()
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            current_price=30000.0,
            atr_15m=100.0,
            atr_1h=200.0,
            trend_1h="UP",
            recent_volume_surge=False,
            spread_percentile_1h=40.0,
        )

        blocked, reasons = engine.would_block_symbol(snapshot, {"max_spread_percentile_1h": 70.0})

        self.assertFalse(blocked)
        self.assertEqual(reasons, [])


if __name__ == "__main__":
    unittest.main()
