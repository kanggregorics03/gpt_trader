import unittest

from main import TradingOrchestrator
from core.models import AccountState, MarketSnapshot, PositionState


class DummyRiskEngine:
    policy = {
        "risk_limits": {
            "daily_max_loss_pct": 3.0,
            "max_consecutive_losses": 3,
        }
    }


class TestTradingOrchestrator(unittest.TestCase):
    def test_deterministic_skip_when_no_exposure_and_entries_blocked(self):
        orchestrator = TradingOrchestrator.__new__(TradingOrchestrator)
        orchestrator.risk_engine = DummyRiskEngine()

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

        self.assertEqual(reason, "risk_profile_cooldown_blocks_entries")

    def test_deterministic_skip_does_not_apply_when_position_exists(self):
        orchestrator = TradingOrchestrator.__new__(TradingOrchestrator)
        orchestrator.risk_engine = DummyRiskEngine()

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
