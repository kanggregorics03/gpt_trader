import unittest
import yaml
from pathlib import Path
from core.risk_engine import RiskEngine
from core.models import TradeIntent, AccountState, PositionState, OrderIntent

class TestRiskEngine(unittest.TestCase):
    def setUp(self):
        # Create a dummy policy.yaml for testing
        self.dummy_policy_path = Path("test_policy.yaml")
        policy_data = {
            "trading": {
                "symbols": ["BTCUSDT"],
                "max_leverage": 3,
                "order_type": "market"
            },
            "risk_limits": {
                "daily_max_loss_pct": 3.0,
                "max_consecutive_losses": 3,
                "cooldown_minutes": 30,
                "max_open_positions": 2
            }
        }
        with open(self.dummy_policy_path, "w") as f:
            yaml.dump(policy_data, f)
            
        self.engine = RiskEngine(config_path="test_policy.yaml")

    def tearDown(self):
        if self.dummy_policy_path.exists():
            self.dummy_policy_path.unlink()

    def test_hold_always_approved(self):
        intent = TradeIntent(
            decision="HOLD",
            orders=[],
            rationale=["Waiting"],
            risk_notes=[],
            confidence=0.5
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, _ = self.engine.evaluate_intent(intent, acc_state)
        self.assertTrue(approved)

    def test_reject_unapproved_symbol(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="DOGEUSDT", action="BUY", order_type="MARKET", quantity=100)],
            rationale=["Moon"],
            risk_notes=[],
            confidence=0.9
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(intent, acc_state)
        self.assertFalse(approved)
        self.assertIn("not in approved policy list", reason)

    def test_reject_max_positions_exceeded(self):
        # Simulate having 2 open positions already
        pos1 = PositionState(symbol="ETHUSDT", side="LONG", size=1.0, entry_price=2000, unrealized_pnl_pct=0)
        pos2 = PositionState(symbol="SOLUSDT", side="SHORT", size=10.0, entry_price=100, unrealized_pnl_pct=0)
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[pos1, pos2], open_orders_count=0)
        
        # Try to open a new one
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.1)],
            rationale=["Setup"],
            risk_notes=[],
            confidence=0.8
        )
        approved, reason = self.engine.evaluate_intent(intent, acc_state)
        self.assertFalse(approved)
        self.assertIn("cannot open more than", reason.lower() if reason else "")

    def test_reject_entry_during_cooldown(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.1)],
            rationale=["Setup"],
            risk_notes=[],
            confidence=0.8
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            risk_state={"cooldown_active": True, "cooldown_remaining_minutes": 12},
        )
        self.assertFalse(approved)
        self.assertIn("cooldown", reason.lower())

    def test_reject_entry_on_daily_loss_kill_switch(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.1)],
            rationale=["Setup"],
            risk_notes=[],
            confidence=0.8
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 30000},
            risk_state={"daily_pnl_pct": -4.1},
        )
        self.assertFalse(approved)
        self.assertIn("kill switch", reason.lower())

    def test_reject_limit_order_when_policy_requires_market(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="LIMIT", quantity=0.1, price=29000)],
            rationale=["Setup"],
            risk_notes=[],
            confidence=0.8
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(intent, acc_state)
        self.assertFalse(approved)
        self.assertIn("order type", reason.lower())

    def test_reject_when_projected_exposure_exceeds_max_leverage(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=0.2)],
            rationale=["Setup"],
            risk_notes=[],
            confidence=0.8
        )
        acc_state = AccountState(total_balance=1000, available_balance=1000, positions=[], open_orders_count=0)
        approved, reason = self.engine.evaluate_intent(
            intent,
            acc_state,
            market_prices={"BTCUSDT": 20000},
        )
        self.assertFalse(approved)
        self.assertIn("projected exposure", reason.lower())

    def test_exit_is_still_safe_during_kill_switch(self):
        intent = TradeIntent(
            decision="EXIT",
            orders=[],
            rationale=["Emergency exit"],
            risk_notes=[],
            confidence=0.9
        )
        pos = PositionState(symbol="BTCUSDT", side="LONG", size=0.1, entry_price=30000, unrealized_pnl_pct=-2.0)
        acc_state = AccountState(total_balance=950, available_balance=900, positions=[pos], open_orders_count=0)
        approved, _ = self.engine.evaluate_intent(
            intent,
            acc_state,
            risk_state={"daily_pnl_pct": -5.0, "cooldown_active": True, "consecutive_losses": 4},
        )
        self.assertTrue(approved)

if __name__ == '__main__':
    unittest.main()
