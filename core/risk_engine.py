import yaml
from pathlib import Path
from typing import Dict, Optional, Tuple

from loguru import logger

from core.models import AccountState, MarketSnapshot, TradeIntent

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

    def get_risk_profiles(self) -> Dict[str, Dict]:
        risk_limits = self.policy.get("risk_limits", {})
        base_max_positions = int(risk_limits.get("max_open_positions", 2) or 2)
        configured_profiles = risk_limits.get("mode_profiles", {}) or {}
        defaults = {
            "normal": {
                "max_open_positions": base_max_positions,
                "max_order_notional_pct_balance": 100.0,
                "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"],
            },
            "trigger": {
                "max_open_positions": max(1, min(base_max_positions, 1)),
                "max_order_notional_pct_balance": 35.0,
                "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"],
            },
            "cooldown": {
                "max_open_positions": base_max_positions,
                "max_order_notional_pct_balance": 0.0,
                "allowed_actions": ["HOLD", "REDUCE", "EXIT"],
            },
            "drawdown": {
                "max_open_positions": max(1, min(base_max_positions, 1)),
                "max_order_notional_pct_balance": 20.0,
                "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT"],
            },
        }
        profiles = {}
        for mode, default_profile in defaults.items():
            profile = dict(default_profile)
            profile.update(configured_profiles.get(mode, {}) or {})
            profile["allowed_actions"] = [str(action).upper() for action in profile.get("allowed_actions", [])]
            profiles[mode] = profile
        return profiles

    def get_active_risk_mode(
        self,
        risk_state: Optional[Dict[str, float]] = None,
        is_trigger_mode: bool = False,
    ) -> str:
        risk_state = risk_state or {}
        if bool(risk_state.get("cooldown_active")):
            return "cooldown"
        if bool(risk_state.get("drawdown_active")):
            return "drawdown"
        if is_trigger_mode:
            return "trigger"
        return "normal"

    def get_active_risk_profile(
        self,
        risk_state: Optional[Dict[str, float]] = None,
        is_trigger_mode: bool = False,
    ) -> Dict:
        profiles = self.get_risk_profiles()
        return profiles.get(
            self.get_active_risk_mode(risk_state=risk_state, is_trigger_mode=is_trigger_mode),
            profiles["normal"],
        )

    def evaluate_intent(
        self,
        intent: TradeIntent,
        account_state: AccountState,
        market_snapshot: Optional[MarketSnapshot] = None,
        market_snapshots: Optional[Dict[str, MarketSnapshot]] = None,
        market_prices: Optional[Dict[str, float]] = None,
        risk_state: Optional[Dict[str, float]] = None,
        is_trigger_mode: bool = False,
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
        market_snapshots = market_snapshots or {}
        risk_state = risk_state or {}
        active_mode = self.get_active_risk_mode(risk_state=risk_state, is_trigger_mode=is_trigger_mode)
        active_profile = self.get_active_risk_profile(risk_state=risk_state, is_trigger_mode=is_trigger_mode)
        is_entry_decision = intent.decision in ("BUY", "SELL")
        is_risk_reducing = intent.decision in ("REDUCE", "EXIT")

        def get_snapshot_for_symbol(symbol: str) -> Optional[MarketSnapshot]:
            if symbol in market_snapshots:
                return market_snapshots[symbol]
            if market_snapshot is not None and market_snapshot.symbol == symbol:
                return market_snapshot
            return market_snapshot

        allowed_actions = set(active_profile.get("allowed_actions", []))
        if allowed_actions and intent.decision not in allowed_actions:
            return False, f"Risk profile {active_mode} blocks action {intent.decision}."

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
        max_positions = min(
            int(self.policy.get('risk_limits', {}).get('max_open_positions', 2) or 2),
            int(active_profile.get("max_open_positions", self.policy.get('risk_limits', {}).get('max_open_positions', 2)) or 0),
        )
        if is_entry_decision:
            new_symbols = {
                order.symbol
                for order in intent.orders
                if not order.reduce_only
            }
            existing_symbols = {
                position.symbol
                for position in account_state.positions
                if abs(float(position.size)) > 0
            }
            projected_symbols = existing_symbols | new_symbols
            if len(projected_symbols) > max_positions:
                return False, f"Risk limit exceeded: Cannot open more than {max_positions} positions."

        # 3. Leverage / Order Type verification
        allowed_symbols = trading_policy.get('symbols', [])
        policy_order_type = str(trading_policy.get("order_type", "market")).upper()
        max_leverage = float(trading_policy.get("max_leverage", 0) or 0)

        for order in intent.orders:
            symbol_market_snapshot = get_snapshot_for_symbol(order.symbol)
            risk_trigger_reasons = set(
                symbol_market_snapshot.risk_trigger_reasons if symbol_market_snapshot else []
            )

            if order.symbol not in allowed_symbols:
                return False, f"Symbol {order.symbol} is not in approved policy list."

            if policy_order_type and order.order_type != policy_order_type:
                return False, (
                    f"Order type {order.order_type} is not allowed. "
                    f"Policy requires {policy_order_type}."
                )

            if is_risk_reducing and not order.reduce_only:
                return False, f"{intent.decision} orders must be marked reduce_only."

            if is_entry_decision and "wide_spread" in risk_trigger_reasons:
                return False, f"Entry blocked: wide spread risk trigger is active for {order.symbol}."

            if is_entry_decision and symbol_market_snapshot is not None:
                funding_rate = symbol_market_snapshot.derivatives.funding_rate
                if "elevated_funding" in risk_trigger_reasons and funding_rate is not None:
                    unfavorable_long = funding_rate > 0 and order.action == "BUY"
                    unfavorable_short = funding_rate < 0 and order.action == "SELL"
                    if unfavorable_long or unfavorable_short:
                        return False, (
                            f"Entry blocked: elevated funding ({funding_rate:.6f}) is unfavorable for "
                            f"{order.action} positions on {order.symbol}."
                        )

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

        max_order_notional_pct_balance = float(
            active_profile.get("max_order_notional_pct_balance", 100.0) or 0.0
        )
        if is_entry_decision and max_order_notional_pct_balance >= 0:
            candidate_notional = 0.0
            for order in intent.orders:
                if order.reduce_only:
                    continue
                reference_price = order.price or market_prices.get(order.symbol)
                if reference_price is None:
                    logger.warning(
                        "Missing market price for {}; using 0 in order-size profile check.",
                        order.symbol,
                    )
                    reference_price = 0.0
                candidate_notional += abs(order.quantity) * float(reference_price)
            max_candidate_notional = account_state.total_balance * (max_order_notional_pct_balance / 100.0)
            if candidate_notional > max_candidate_notional:
                return False, (
                    f"Risk profile {active_mode} limits candidate size to "
                    f"{max_candidate_notional:.2f}, got {candidate_notional:.2f}."
                )

        return True, "Approved"
