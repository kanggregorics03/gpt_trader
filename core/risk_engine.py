import yaml
from pathlib import Path
from loguru import logger
from core.models import TradeIntent, AccountState
from typing import Dict, Optional, Tuple

class RiskEngine:
    def __init__(self, config_path: str = "policy.yaml"):
        self.config_path = Path(config_path)
        self.policy = self._load_policy()

    def _load_policy(self) -> dict:
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Failed to load policy.yaml: {e}")
            raise

    def get_summary(self) -> str:
        """Returns a string representation of the policy for Gemini context"""
        return yaml.dump(self.policy)

    def evaluate_intent(
        self,
        intent: TradeIntent,
        account_state: AccountState,
        market_prices: Optional[Dict[str, float]] = None,
        risk_state: Optional[Dict[str, float]] = None,
    ) -> Tuple[bool, str]:
        """
        Validates the proposed TradeIntent against policy.yaml and account_state.
        Returns (is_approved, reject_reason_string)
        """
        if intent.decision == "HOLD":
            # Exiting or doing nothing is generally always safe
            return True, "Safe action."

        risk_limits = self.policy.get("risk_limits", {})
        trading_policy = self.policy.get("trading", {})
        market_prices = market_prices or {}
        risk_state = risk_state or {}
        is_entry_decision = intent.decision in ("BUY", "SELL")
        is_risk_reducing = intent.decision in ("REDUCE", "EXIT")

        if is_entry_decision:
            daily_max_loss_pct = float(risk_limits.get("daily_max_loss_pct", 0.0) or 0.0)
            daily_pnl_pct = float(risk_state.get("daily_pnl_pct", 0.0) or 0.0)
            if daily_max_loss_pct > 0 and daily_pnl_pct <= -daily_max_loss_pct:
                return False, (
                    f"Kill switch active: daily pnl {daily_pnl_pct:.2f}% "
                    f"is below limit {-daily_max_loss_pct:.2f}%."
                )

            max_consecutive_losses = int(risk_limits.get("max_consecutive_losses", 0) or 0)
            consecutive_losses = int(risk_state.get("consecutive_losses", 0) or 0)
            if max_consecutive_losses > 0 and consecutive_losses >= max_consecutive_losses:
                return False, (
                    f"Kill switch active: {consecutive_losses} consecutive losses "
                    f"reached policy limit {max_consecutive_losses}."
                )

            if bool(risk_state.get("cooldown_active")):
                remaining = int(risk_state.get("cooldown_remaining_minutes", 0) or 0)
                return False, f"Cooldown active for another {remaining} minute(s)."

        # 2. Max Open Positions
        max_positions = self.policy.get('risk_limits', {}).get('max_open_positions', 2)
        if len(account_state.positions) >= max_positions and is_entry_decision:
            # Check if this order opens a *new* position or adds to an existing
            # Strict mode: just reject if we are at max.
            # (A robust check would iterate over intent.orders to see if they match existing symbols)
            new_symbols = {o.symbol for o in intent.orders}
            existing_symbols = {p.symbol for p in account_state.positions}
            if not new_symbols.issubset(existing_symbols):
                return False, f"Risk limit exceeded: Cannot open more than {max_positions} positions."

        # 3. Leverage / Order Type verification
        allowed_symbols = trading_policy.get('symbols', [])
        policy_order_type = str(trading_policy.get("order_type", "market")).upper()
        max_leverage = float(trading_policy.get("max_leverage", 0) or 0)

        for order in intent.orders:
            if order.symbol not in allowed_symbols:
                return False, f"Symbol {order.symbol} is not in approved policy list."

            if policy_order_type and order.order_type != policy_order_type:
                return False, (
                    f"Order type {order.order_type} is not allowed. "
                    f"Policy requires {policy_order_type}."
                )

            if is_risk_reducing and not order.reduce_only:
                return False, f"{intent.decision} orders must be marked reduce_only."

        if is_entry_decision and max_leverage > 0:
            current_exposure = sum(abs(pos.size) * pos.entry_price for pos in account_state.positions)
            incremental_exposure = 0.0
            for order in intent.orders:
                if order.reduce_only:
                    continue
                reference_price = order.price or market_prices.get(order.symbol)
                if reference_price is None:
                    logger.warning(
                        "Missing market price for {}; using 0 in exposure calculation.",
                        order.symbol,
                    )
                    reference_price = 0.0
                incremental_exposure += abs(order.quantity) * float(reference_price)

            projected_exposure = current_exposure + incremental_exposure
            max_allowed_exposure = account_state.total_balance * max_leverage
            if max_allowed_exposure > 0 and projected_exposure > max_allowed_exposure:
                return False, (
                    f"Projected exposure {projected_exposure:.2f} exceeds "
                    f"max allowed {max_allowed_exposure:.2f}."
                )

        return True, "Approved"
