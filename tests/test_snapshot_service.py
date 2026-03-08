import unittest

from core.models import AccountState, MarketSnapshot, OrderBookSnapshot, RiskStateSnapshot
from core.snapshot_service import SnapshotService


class DummyStateStore:
    def get_risk_state(self, **_kwargs):
        return {"cooldown_active": True, "cooldown_remaining_minutes": 7}

    def get_symbol_trigger_state(self, _symbol):
        return {}

    def save_symbol_trigger_state(self, _symbol, _state):
        return None


class DummyAccountReader:
    def get_account_state(self, _symbols):
        return AccountState(
            total_balance=1000.0,
            available_balance=950.0,
            positions=[],
            open_orders=[],
            open_orders_count=0,
        )


class DummyRiskEngine:
    policy = {"risk_limits": {}}

    def get_active_risk_mode(self, **_kwargs):
        return "normal"

    def get_active_risk_profile(self, **_kwargs):
        return {"allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"]}

    def get_summary(self):
        return "policy"


class RecordingCollector:
    def __init__(self):
        self.calls = []

    def get_market_snapshot(
        self,
        symbol,
        trigger_state=None,
        timeframes_to_fetch=None,
        existing_snapshot=None,
    ):
        refreshed = dict(timeframes_to_fetch or {})
        self.calls.append(
            {
                "symbol": symbol,
                "timeframes": tuple(sorted(refreshed.keys())),
                "has_existing": existing_snapshot is not None,
            }
        )
        timeframes = dict(existing_snapshot.timeframes) if existing_snapshot else {}
        for timeframe in refreshed:
            timeframes[timeframe] = {
                "timeframe": timeframe,
                "closed_at": len(self.calls),
                "last_close": 100.0,
            }
        return MarketSnapshot(
            symbol=symbol,
            current_price=100.0,
            atr_15m=1.0,
            atr_1h=2.0,
            trend_1h="UP",
            recent_volume_surge=False,
            timeframes=timeframes,
            order_book=OrderBookSnapshot(),
        )

    def build_next_trigger_state(self, _snapshot):
        return {}


class TestSnapshotService(unittest.TestCase):
    def test_materialization_refreshes_only_due_timeframes_after_initial_load(self):
        service = SnapshotService(
            state_store=DummyStateStore(),
            account_reader=DummyAccountReader(),
            market_collector=RecordingCollector(),
            risk_engine=DummyRiskEngine(),
            symbols=["BTCUSDT"],
            materialization_minutes=5,
        )

        first = service.materialize(now_epoch=1, force=True)
        second = service.materialize(now_epoch=(5 * 60) + 1)
        third = service.materialize(now_epoch=(15 * 60) + 1)
        fourth = service.materialize(now_epoch=(60 * 60) + 1)

        self.assertEqual(first.refreshed_timeframes, ("15m", "1h", "4h", "5m"))
        self.assertEqual(second.refreshed_timeframes, ("5m",))
        self.assertEqual(third.refreshed_timeframes, ("15m", "5m"))
        self.assertEqual(fourth.refreshed_timeframes, ("15m", "1h", "5m"))

    def test_materialize_reuses_cached_bundle_within_same_5m_bucket(self):
        service = SnapshotService(
            state_store=DummyStateStore(),
            account_reader=DummyAccountReader(),
            market_collector=RecordingCollector(),
            risk_engine=DummyRiskEngine(),
            symbols=["BTCUSDT"],
            materialization_minutes=5,
        )

        first = service.materialize(now_epoch=1, force=True)
        second = service.materialize(now_epoch=60, force=False)

        self.assertIs(first, second)
        self.assertEqual(len(service.market_collector.calls), 1)

    def test_materialize_coerces_risk_state_and_uses_single_symbol(self):
        service = SnapshotService(
            state_store=DummyStateStore(),
            account_reader=DummyAccountReader(),
            market_collector=RecordingCollector(),
            risk_engine=DummyRiskEngine(),
            symbols=["BTCUSDT", "ETHUSDT"],
            materialization_minutes=5,
        )

        bundle = service.materialize(now_epoch=1, force=True)

        self.assertIsInstance(bundle.risk_state, RiskStateSnapshot)
        self.assertEqual(bundle.risk_state.cooldown_remaining_minutes, 7)
        self.assertEqual(sorted(bundle.market_snapshots.keys()), ["BTCUSDT"])
        self.assertEqual([call["symbol"] for call in service.market_collector.calls], ["BTCUSDT"])

    def test_trigger_mode_requires_strong_reason_and_multiple_reasons_on_same_symbol(self):
        service = SnapshotService(
            state_store=DummyStateStore(),
            account_reader=DummyAccountReader(),
            market_collector=RecordingCollector(),
            risk_engine=DummyRiskEngine(),
            symbols=["BTCUSDT"],
            materialization_minutes=5,
        )

        fast_only = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=100.0,
                atr_15m=1.0,
                atr_1h=2.0,
                trend_1h="UP",
                recent_volume_surge=False,
                is_opportunity_trigger=True,
                opportunity_trigger_reasons=["fast_price_move_15m"],
            )
        }
        mixed_strong = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=100.0,
                atr_15m=1.0,
                atr_1h=2.0,
                trend_1h="UP",
                recent_volume_surge=True,
                is_opportunity_trigger=True,
                opportunity_trigger_reasons=["fast_price_move_15m", "volume_surge_15m"],
            )
        }
        split_across_symbols = {
            "BTCUSDT": MarketSnapshot(
                symbol="BTCUSDT",
                current_price=100.0,
                atr_15m=1.0,
                atr_1h=2.0,
                trend_1h="UP",
                recent_volume_surge=False,
                is_opportunity_trigger=True,
                opportunity_trigger_reasons=["fast_price_move_15m"],
            ),
            "ETHUSDT": MarketSnapshot(
                symbol="ETHUSDT",
                current_price=200.0,
                atr_15m=2.0,
                atr_1h=4.0,
                trend_1h="UP",
                recent_volume_surge=True,
                is_opportunity_trigger=True,
                opportunity_trigger_reasons=["volume_surge_15m"],
            ),
        }

        self.assertFalse(service._should_enable_trigger_mode(fast_only))
        self.assertTrue(service._should_enable_trigger_mode(mixed_strong))
        self.assertFalse(service._should_enable_trigger_mode(split_across_symbols))
