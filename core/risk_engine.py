import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from core.models import (
    AccountState,
    MarketSnapshot,
    RiskStateSnapshot,
    TradeIntent,
    coerce_risk_state_snapshot,
)

class RiskEngine:
    def __init__(self, config_path: str = "policy.yaml"):
        self.config_path = Path(config_path)
        self.policy = self._validate_policy(self._normalize_single_symbol_policy(self._load_policy()))

    def _load_policy(self) -> dict:
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return yaml.safe_load(f)
        except Exception as e:
            logger.error(f"Failed to load policy.yaml: {e}")
            raise

    def _normalize_single_symbol_policy(self, raw_policy: Optional[dict]) -> dict:
        policy = dict(raw_policy or {})
        trading_policy = dict(policy.get("trading") or {})
        configured_symbols = [
            str(symbol).strip().upper()
            for symbol in (trading_policy.get("symbols") or [])
            if str(symbol).strip()
        ]
        if not configured_symbols:
            configured_symbols = ["BTCUSDT"]
        primary_symbol = configured_symbols[0]
        if len(configured_symbols) > 1:
            logger.warning(
                "Single-symbol mode is active. Using {} and ignoring {} additional configured symbol(s).",
                primary_symbol,
                len(configured_symbols) - 1,
            )
        trading_policy["symbols"] = [primary_symbol]
        policy["trading"] = trading_policy

        risk_limits = dict(policy.get("risk_limits") or {})
        risk_limits["max_open_positions"] = 1
        mode_profiles = dict(risk_limits.get("mode_profiles") or {})
        for mode_name, configured_profile in list(mode_profiles.items()):
            profile = dict(configured_profile or {})
            profile["max_open_positions"] = 1
            mode_profiles[mode_name] = profile
        risk_limits["mode_profiles"] = mode_profiles
        policy["risk_limits"] = risk_limits
        return policy

    def _validate_policy(self, policy: dict) -> dict:
        trading_policy = dict(policy.get("trading") or {})
        configured_symbols = [str(symbol).strip().upper() for symbol in (trading_policy.get("symbols") or []) if str(symbol).strip()]
        if len(configured_symbols) != 1:
            raise ValueError("Single-symbol mode requires exactly one trading symbol in policy.yaml.")

        risk_limits = dict(policy.get("risk_limits") or {})
        if int(risk_limits.get("max_open_positions", 1) or 1) != 1:
            raise ValueError("Single-symbol mode requires risk_limits.max_open_positions = 1.")
        for mode_name, profile in ((risk_limits.get("mode_profiles") or {}) or {}).items():
            if int((profile or {}).get("max_open_positions", 1) or 1) != 1:
                raise ValueError(f"Single-symbol mode requires mode_profiles.{mode_name}.max_open_positions = 1.")

        for section_name in ("position_policy", "safety_policy"):
            section = dict(policy.get(section_name) or {})
            if not section:
                continue
            spread_limit = section.get("stale_order_reprice_spread_percentile_max")
            depth_floor = section.get("stale_order_reprice_depth_percentile_min")
            limit_offset = section.get("stale_order_reprice_limit_offset_bps")
            stale_minutes = section.get("stale_order_minutes")
            if spread_limit is not None and not (0 <= float(spread_limit) <= 100):
                raise ValueError(f"{section_name}.stale_order_reprice_spread_percentile_max must be between 0 and 100.")
            if depth_floor is not None and not (0 <= float(depth_floor) <= 100):
                raise ValueError(f"{section_name}.stale_order_reprice_depth_percentile_min must be between 0 and 100.")
            if limit_offset is not None and float(limit_offset) < 0:
                raise ValueError(f"{section_name}.stale_order_reprice_limit_offset_bps must be >= 0.")
            if stale_minutes is not None and float(stale_minutes) <= 0:
                raise ValueError(f"{section_name}.stale_order_minutes must be > 0.")

        execution_guards = dict(policy.get("execution_guards") or {})
        close_minutes = int(execution_guards.get("symbol_reentry_close_cooldown_minutes", 0) or 0)
        loss_minutes = int(execution_guards.get("symbol_reentry_loss_cooldown_minutes", 0) or 0)
        loss_threshold_pct = float(execution_guards.get("symbol_reentry_loss_threshold_pct", 0.0) or 0.0)
        if close_minutes < 0 or loss_minutes < 0:
            raise ValueError("symbol re-entry cooldown minutes must be >= 0.")
        if loss_minutes and close_minutes and loss_minutes < close_minutes:
            raise ValueError("symbol_reentry_loss_cooldown_minutes must be >= symbol_reentry_close_cooldown_minutes.")
        if loss_threshold_pct > 0:
            raise ValueError("symbol_reentry_loss_threshold_pct must be <= 0.0.")
        return policy

    def get_summary(self) -> str:
        """Returns a string representation of the policy for Gemini context"""
        return yaml.dump(self.policy)

    def get_trading_symbol(self) -> str:
        trading_policy = self.policy.get("trading", {}) or {}
        configured = trading_policy.get("symbols") or ["BTCUSDT"]
        return str(configured[0]).strip().upper()

    def get_trading_symbols(self) -> List[str]:
        return [self.get_trading_symbol()]

    def get_allowed_order_types(self) -> List[str]:
        trading_policy = self.policy.get("trading", {}) or {}
        execution_policy = self.policy.get("execution_policy", {}) or {}
        configured = execution_policy.get("allowed_order_types")
        if configured:
            return [str(item).upper() for item in configured if str(item).strip()]
        legacy = str(trading_policy.get("order_type") or "").strip().upper()
        if legacy:
            return [legacy]
        return ["MARKET", "LIMIT"]

    def get_risk_profiles(self) -> Dict[str, Dict]:
        risk_limits = self.policy.get("risk_limits", {})
        base_max_positions = int(risk_limits.get("max_open_positions", 2) or 2)
        configured_profiles = risk_limits.get("mode_profiles", {}) or {}
        defaults = {
            "normal": {
                "max_open_positions": base_max_positions,
                "max_order_notional_pct_balance": 100.0,
                "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"],
            },
            "trigger": {
                "max_open_positions": max(1, min(base_max_positions, 1)),
                "max_order_notional_pct_balance": 35.0,
                "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"],
                "block_additional_entries_when_position_exists": True,
            },
            "cooldown": {
                "max_open_positions": base_max_positions,
                "max_order_notional_pct_balance": 0.0,
                "allowed_actions": ["HOLD", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"],
            },
            "drawdown": {
                "max_open_positions": max(1, min(base_max_positions, 1)),
                "max_order_notional_pct_balance": 20.0,
                "entry_size_multiplier": 0.75,
                "confidence_cap": 0.55,
                "failure_pattern_size_multiplier": 0.75,
                "risk_trigger_size_multiplier": 0.85,
                "allowed_actions": ["HOLD", "BUY", "SELL", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"],
            },
        }
        profiles = {}
        for mode, default_profile in defaults.items():
            profile = dict(default_profile)
            profile.update(configured_profiles.get(mode, {}) or {})
            allowed_actions = [str(action).upper() for action in profile.get("allowed_actions", [])]
            for management_action in ("CANCEL", "REPLACE_ENTRY"):
                if management_action not in allowed_actions:
                    allowed_actions.append(management_action)
            profile["allowed_actions"] = allowed_actions
            profiles[mode] = profile
        return profiles

    def get_active_risk_mode(
        self,
        risk_state: Optional[Dict[str, Any] | RiskStateSnapshot] = None,
        is_trigger_mode: bool = False,
    ) -> str:
        risk_state = coerce_risk_state_snapshot(risk_state)
        if bool(risk_state.cooldown_active):
            return "cooldown"
        if bool(risk_state.drawdown_active):
            return "drawdown"
        if is_trigger_mode:
            return "trigger"
        return "normal"

    def get_active_risk_profile(
        self,
        risk_state: Optional[Dict[str, Any] | RiskStateSnapshot] = None,
        is_trigger_mode: bool = False,
    ) -> Dict:
        profiles = self.get_risk_profiles()
        return profiles.get(
            self.get_active_risk_mode(risk_state=risk_state, is_trigger_mode=is_trigger_mode),
            profiles["normal"],
        )

    def get_evidence_quality_summary(self, intent: TradeIntent) -> Dict[str, object]:
        evidence_items = []
        unique_signals = set()
        covered_symbols = set()
        failure_mode_count = 0
        for item in intent.rationale_items:
            if item.failure_mode:
                failure_mode_count += 1
            for evidence in item.evidence:
                evidence_items.append(evidence)
                unique_signals.add((evidence.symbol or "", evidence.signal, evidence.timeframe))
                if evidence.symbol:
                    covered_symbols.add(evidence.symbol)

        factor = 1.0
        issues = []
        if intent.rationale_items:
            if len(unique_signals) < 2:
                factor = min(factor, 0.7)
                issues.append("low_signal_diversity")
            if len(evidence_items) < max(1, len(intent.orders)):
                factor = min(factor, 0.6)
                issues.append("insufficient_evidence_rows")
            if failure_mode_count < len(intent.rationale_items):
                factor = min(factor, 0.85)
                issues.append("missing_failure_mode")

            entry_symbols = {
                order.symbol
                for order in intent.orders
                if not order.reduce_only
            }
            uncovered_symbols = sorted(symbol for symbol in entry_symbols if symbol not in covered_symbols)
            if uncovered_symbols:
                factor = min(factor, 0.5)
                issues.append(f"missing_symbol_evidence:{','.join(uncovered_symbols)}")
        return {
            "factor": factor,
            "issues": issues,
            "unique_signal_count": len(unique_signals),
            "evidence_count": len(evidence_items),
            "covered_symbols": sorted(covered_symbols),
        }

    def get_effective_max_order_notional_pct_balance(
        self,
        intent: TradeIntent,
        active_profile: Optional[Dict] = None,
        risk_state: Optional[Dict[str, Any] | RiskStateSnapshot] = None,
        market_snapshots: Optional[Dict[str, MarketSnapshot]] = None,
    ) -> Dict[str, object]:
        active_profile = active_profile or {}
        risk_state = coerce_risk_state_snapshot(risk_state)
        market_snapshots = market_snapshots or {}
        base_cap_pct = float(active_profile.get("max_order_notional_pct_balance", 100.0) or 0.0)
        evidence_quality = self.get_evidence_quality_summary(intent)
        evidence_factor = float(evidence_quality.get("factor") or 1.0)
        profile_factor = 1.0
        profile_issues: List[str] = []
        profile_adjustments: Dict[str, float] = {}

        entry_size_multiplier = min(
            1.0,
            max(0.0, float(active_profile.get("entry_size_multiplier", 1.0) or 1.0)),
        )
        if entry_size_multiplier < 1.0:
            profile_factor *= entry_size_multiplier
            profile_adjustments["entry_size_multiplier"] = entry_size_multiplier
            profile_issues.append("mode_entry_size_multiplier")

        confidence_cap = active_profile.get("confidence_cap")
        if confidence_cap is not None:
            confidence_cap = float(confidence_cap or 0.0)
            if confidence_cap > 0 and float(intent.confidence or 0.0) > confidence_cap:
                confidence_factor = max(
                    0.0,
                    min(1.0, confidence_cap / float(intent.confidence or 1.0)),
                )
                profile_factor *= confidence_factor
                profile_adjustments["confidence_factor"] = confidence_factor
                profile_issues.append(f"confidence_cap:{confidence_cap:.2f}")

        failure_pattern_score = min(
            1.0,
            max(0.0, float(risk_state.failure_pattern_score or 0.0)),
        )
        failure_pattern_size_multiplier = min(
            1.0,
            max(0.0, float(active_profile.get("failure_pattern_size_multiplier", 1.0) or 1.0)),
        )
        if failure_pattern_score > 0 and failure_pattern_size_multiplier < 1.0:
            failure_pattern_factor = 1.0 - (
                (1.0 - failure_pattern_size_multiplier) * failure_pattern_score
            )
            profile_factor *= failure_pattern_factor
            profile_adjustments["failure_pattern_factor"] = failure_pattern_factor
            profile_issues.append(f"failure_pattern_score:{failure_pattern_score:.2f}")

        risk_trigger_size_multiplier = min(
            1.0,
            max(0.0, float(active_profile.get("risk_trigger_size_multiplier", 1.0) or 1.0)),
        )
        has_risk_trigger = any(
            bool(getattr(snapshot, "is_risk_trigger", False))
            for snapshot in market_snapshots.values()
        )
        if has_risk_trigger and risk_trigger_size_multiplier < 1.0:
            profile_factor *= risk_trigger_size_multiplier
            profile_adjustments["risk_trigger_factor"] = risk_trigger_size_multiplier
            profile_issues.append("risk_trigger_size_penalty")

        factor = evidence_factor * profile_factor
        effective_cap_pct = base_cap_pct
        if intent.rationale_items:
            effective_cap_pct *= factor
        elif profile_factor != 1.0:
            effective_cap_pct *= profile_factor
        return {
            "base_cap_pct": base_cap_pct,
            "effective_cap_pct": effective_cap_pct,
            "evidence_quality": evidence_quality,
            "profile_adjustments": profile_adjustments,
            "profile_issues": profile_issues,
            "failure_pattern_score": failure_pattern_score,
            "adjusted": effective_cap_pct != base_cap_pct,
        }

    def evaluate_intent(
        self,
        intent: TradeIntent,
        account_state: AccountState,
        market_snapshot: Optional[MarketSnapshot] = None,
        market_snapshots: Optional[Dict[str, MarketSnapshot]] = None,
        market_prices: Optional[Dict[str, float]] = None,
        risk_state: Optional[Dict[str, Any] | RiskStateSnapshot] = None,
        is_trigger_mode: bool = False,
        unresolved_timeout_count: int = 0,
        active_profile_override: Optional[Dict] = None,
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
        risk_state = coerce_risk_state_snapshot(risk_state)
        active_mode = self.get_active_risk_mode(risk_state=risk_state, is_trigger_mode=is_trigger_mode)
        active_profile = active_profile_override or self.get_active_risk_profile(
            risk_state=risk_state,
            is_trigger_mode=is_trigger_mode,
        )
        allowed_symbols = trading_policy.get('symbols', [])
        is_cancel_decision = intent.decision == "CANCEL"
        is_replace_entry_decision = intent.decision == "REPLACE_ENTRY"
        is_entry_decision = intent.decision in ("BUY", "SELL")
        uses_entry_exposure = is_entry_decision or is_replace_entry_decision
        is_risk_reducing = intent.decision in ("REDUCE", "EXIT", "CANCEL")

        def get_snapshot_for_symbol(symbol: str) -> Optional[MarketSnapshot]:
            if symbol in market_snapshots:
                return market_snapshots[symbol]
            if market_snapshot is not None and market_snapshot.symbol == symbol:
                return market_snapshot
            return market_snapshot

        def is_pending_entry_order(order) -> bool:
            status = str(getattr(order, "status", "") or "").lower()
            if status in ("closed", "filled", "canceled", "cancelled", "rejected", "failed"):
                return False
            if bool(getattr(order, "reduce_only", False)):
                return False
            return str(getattr(order, "side", "")).upper() in ("BUY", "SELL")

        def is_cancelled_by_intent(open_order) -> bool:
            symbol = str(getattr(open_order, "symbol", "") or "")
            order_id = getattr(open_order, "order_id", None)
            client_order_id = getattr(open_order, "client_order_id", None)
            for cancel_order in cancel_orders:
                if cancel_order.symbol != symbol:
                    continue
                if order_id and cancel_order.target_order_id and order_id == cancel_order.target_order_id:
                    return True
                if client_order_id and cancel_order.target_client_order_id and client_order_id == cancel_order.target_client_order_id:
                    return True
            return False

        allowed_actions = set(active_profile.get("allowed_actions", []))
        if allowed_actions and intent.decision not in allowed_actions:
            return False, f"Risk profile {active_mode} blocks action {intent.decision}."

        cancel_orders = [order for order in intent.orders if order.action == "CANCEL"]
        non_cancel_orders = [order for order in intent.orders if order.action != "CANCEL"]

        if cancel_orders:
            open_orders_by_symbol = {}
            for open_order in account_state.open_orders:
                open_orders_by_symbol.setdefault(str(getattr(open_order, "symbol", "") or ""), []).append(open_order)
            for order in cancel_orders:
                if order.symbol not in allowed_symbols:
                    return False, f"Symbol {order.symbol} is not in approved policy list."
                candidates = open_orders_by_symbol.get(order.symbol, [])
                if not candidates:
                    return False, f"No live open orders found for {order.symbol}."
                matches_target = any(
                    (
                        getattr(candidate, "order_id", None)
                        and getattr(candidate, "order_id", None) == order.target_order_id
                    )
                    or (
                        getattr(candidate, "client_order_id", None)
                        and getattr(candidate, "client_order_id", None) == order.target_client_order_id
                    )
                    for candidate in candidates
                )
                if not matches_target:
                    return False, (
                        f"Cancel target for {order.symbol} is not present in the live open-order set."
                    )
        if is_cancel_decision:
            if not intent.orders:
                return False, "Cancel action requires at least one target order."
            if non_cancel_orders:
                return False, "Cancel decision may only contain CANCEL orders."
            return True, "Approved"

        if is_replace_entry_decision:
            if not cancel_orders or not non_cancel_orders:
                return False, "REPLACE_ENTRY requires both a cancel leg and a replacement entry leg."
            if len(cancel_orders) != len(non_cancel_orders):
                return False, "REPLACE_ENTRY requires one replacement order per cancel leg."
            open_orders_by_key = {}
            for open_order in account_state.open_orders:
                symbol = str(getattr(open_order, "symbol", "") or "")
                order_id = getattr(open_order, "order_id", None)
                client_order_id = getattr(open_order, "client_order_id", None)
                if order_id:
                    open_orders_by_key[("order_id", symbol, order_id)] = open_order
                if client_order_id:
                    open_orders_by_key[("client_order_id", symbol, client_order_id)] = open_order
            for replacement_order in non_cancel_orders:
                if replacement_order.reduce_only:
                    return False, "REPLACE_ENTRY replacement orders must not be reduce_only."
                if replacement_order.action not in ("BUY", "SELL"):
                    return False, "REPLACE_ENTRY replacement orders must be BUY or SELL."
                if replacement_order.symbol not in allowed_symbols:
                    return False, f"Symbol {replacement_order.symbol} is not in approved policy list."
                if replacement_order.wait_for_cancel_confirmation is not True:
                    return False, "REPLACE_ENTRY replacement orders must wait for cancel confirmation."
                matched_cancel = next(
                    (
                        order for order in cancel_orders
                        if order.replace_group_id
                        and order.replace_group_id == replacement_order.replace_group_id
                    ),
                    None,
                )
                if matched_cancel is None:
                    return False, "REPLACE_ENTRY replacement order is missing its linked cancel leg."
                target_open_order = None
                if matched_cancel.target_order_id:
                    target_open_order = open_orders_by_key.get(("order_id", matched_cancel.symbol, matched_cancel.target_order_id))
                if target_open_order is None and matched_cancel.target_client_order_id:
                    target_open_order = open_orders_by_key.get(("client_order_id", matched_cancel.symbol, matched_cancel.target_client_order_id))
                if target_open_order is None:
                    return False, "REPLACE_ENTRY target order is not present in the live open-order set."
                target_side = str(getattr(target_open_order, "side", "") or "").upper()
                target_quantity = abs(float(getattr(target_open_order, "quantity", 0.0) or 0.0))
                if replacement_order.symbol != matched_cancel.symbol:
                    return False, "REPLACE_ENTRY cancel and replacement legs must target the same symbol."
                if replacement_order.action != target_side:
                    return False, "REPLACE_ENTRY replacement side must match the cancelled entry order side."
                if abs(float(replacement_order.quantity or 0.0)) > target_quantity + 1e-9:
                    return False, "REPLACE_ENTRY replacement size must not exceed the cancelled order quantity."

        if is_entry_decision:
            block_additional_entries = bool(
                active_profile.get("block_additional_entries_when_position_exists", False)
            )
            has_live_positions = any(
                abs(float(getattr(position, "size", 0.0) or 0.0)) > 0
                for position in account_state.positions
            )
            if active_mode == "trigger" and block_additional_entries and has_live_positions:
                return False, (
                    "Trigger mode blocks additional entry while a live position already exists. "
                    "Manage the position with HOLD/REDUCE/EXIT instead."
                )
            if int(unresolved_timeout_count or 0) > 0:
                return False, (
                    f"Entry blocked: {int(unresolved_timeout_count)} unresolved order lifecycle state(s) remain unknown."
                )
            daily_max_loss_pct = float(risk_limits.get("daily_max_loss_pct", 0.0) or 0.0)
            daily_pnl_pct = float(risk_state.daily_pnl_pct or 0.0)
            if daily_max_loss_pct > 0 and daily_pnl_pct <= -daily_max_loss_pct:
                return False, (
                    f"Kill switch active: daily pnl {daily_pnl_pct:.2f}% "
                    f"is below limit {-daily_max_loss_pct:.2f}%."
                )

            max_consecutive_losses = int(risk_limits.get("max_consecutive_losses", 0) or 0)
            consecutive_losses = int(risk_state.consecutive_losses or 0)
            if max_consecutive_losses > 0 and consecutive_losses >= max_consecutive_losses:
                return False, (
                    f"Kill switch active: {consecutive_losses} consecutive losses "
                    f"reached policy limit {max_consecutive_losses}."
                )

            if bool(risk_state.cooldown_active):
                remaining = int(risk_state.cooldown_remaining_minutes or 0)
                effective_window = int(risk_state.cooldown_minutes_effective or 0)
                pattern_reason = str(risk_state.dominant_failure_reason or "").strip()
                suffix = ""
                if effective_window > 0:
                    suffix = f" (window {effective_window} minute(s)"
                    if pattern_reason:
                        suffix += f", pattern={pattern_reason}"
                    suffix += ")"
                return False, f"Cooldown active for another {remaining} minute(s){suffix}."

        # 2. Max Open Positions
        max_positions = min(
            int(self.policy.get('risk_limits', {}).get('max_open_positions', 2) or 2),
            int(active_profile.get("max_open_positions", self.policy.get('risk_limits', {}).get('max_open_positions', 2)) or 0),
        )
        if uses_entry_exposure:
            new_symbols = {
                order.symbol
                for order in non_cancel_orders
                if not order.reduce_only
            }
            pending_symbols = {
                order.symbol
                for order in account_state.open_orders
                if is_pending_entry_order(order) and not is_cancelled_by_intent(order)
            }
            existing_symbols = {
                position.symbol
                for position in account_state.positions
                if abs(float(position.size)) > 0
            }
            projected_symbols = existing_symbols | pending_symbols | new_symbols
            if len(projected_symbols) > max_positions:
                return False, f"Risk limit exceeded: Cannot open more than {max_positions} positions."

        # 3. Leverage / Order Type verification
        allowed_order_types = set(self.get_allowed_order_types())
        max_leverage = float(trading_policy.get("max_leverage", 0) or 0)
        risk_advisories: List[str] = []

        for order in intent.orders:
            if order.action == "CANCEL":
                continue
            symbol_market_snapshot = get_snapshot_for_symbol(order.symbol)
            risk_trigger_reasons = set(
                symbol_market_snapshot.risk_trigger_reasons if symbol_market_snapshot else []
            )

            if order.symbol not in allowed_symbols:
                return False, f"Symbol {order.symbol} is not in approved policy list."

            if allowed_order_types and order.order_type not in allowed_order_types:
                return False, (
                    f"Order type {order.order_type} is not allowed. "
                    f"Policy allows {', '.join(sorted(allowed_order_types))}."
                )

            if is_risk_reducing and not is_entry_decision and not order.reduce_only:
                return False, f"{intent.decision} orders must be marked reduce_only."

            if uses_entry_exposure and "wide_spread" in risk_trigger_reasons:
                spread_bps = (
                    symbol_market_snapshot.order_book.spread_bps
                    if symbol_market_snapshot is not None
                    else None
                )
                if spread_bps is not None:
                    risk_advisories.append(f"{order.symbol}: wide_spread={float(spread_bps):.2f}bps")
                else:
                    risk_advisories.append(f"{order.symbol}: wide_spread")

            if uses_entry_exposure and symbol_market_snapshot is not None:
                funding_rate = symbol_market_snapshot.derivatives.funding_rate
                if "elevated_funding" in risk_trigger_reasons and funding_rate is not None:
                    unfavorable_long = funding_rate > 0 and order.action == "BUY"
                    unfavorable_short = funding_rate < 0 and order.action == "SELL"
                    if unfavorable_long or unfavorable_short:
                        risk_advisories.append(
                            f"{order.symbol}: elevated_funding_unfavorable={float(funding_rate):.6f}"
                        )

        if uses_entry_exposure and max_leverage > 0:
            current_exposure = sum(abs(pos.size) * pos.entry_price for pos in account_state.positions)
            pending_exposure = 0.0
            for pending_order in account_state.open_orders:
                if not is_pending_entry_order(pending_order) or is_cancelled_by_intent(pending_order):
                    continue
                pending_price = pending_order.price or market_prices.get(pending_order.symbol)
                if pending_price is None:
                    pending_price = 0.0
                pending_exposure += abs(float(pending_order.quantity or 0.0)) * float(pending_price)
            incremental_exposure = 0.0
            for order in non_cancel_orders:
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

            projected_exposure = current_exposure + pending_exposure + incremental_exposure
            max_allowed_exposure = account_state.total_balance * max_leverage
            if max_allowed_exposure > 0 and projected_exposure > max_allowed_exposure:
                return False, (
                    f"Projected exposure {projected_exposure:.2f} exceeds "
                    f"max allowed {max_allowed_exposure:.2f}."
                )

        max_order_notional_pct_balance = float(
            active_profile.get("max_order_notional_pct_balance", 100.0) or 0.0
        )
        if uses_entry_exposure and max_order_notional_pct_balance >= 0:
            cap_summary = self.get_effective_max_order_notional_pct_balance(
                intent=intent,
                active_profile=active_profile,
                risk_state=risk_state,
                market_snapshots=market_snapshots,
            )
            evidence_quality = cap_summary["evidence_quality"]
            adjusted_notional_pct_balance = float(cap_summary["effective_cap_pct"])
            candidate_notional = 0.0
            pending_notional = 0.0
            for pending_order in account_state.open_orders:
                if not is_pending_entry_order(pending_order) or is_cancelled_by_intent(pending_order):
                    continue
                pending_price = pending_order.price or market_prices.get(pending_order.symbol)
                if pending_price is None:
                    pending_price = 0.0
                pending_notional += abs(float(pending_order.quantity or 0.0)) * float(pending_price)
            for order in non_cancel_orders:
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
            candidate_notional += pending_notional
            max_candidate_notional = account_state.total_balance * (adjusted_notional_pct_balance / 100.0)
            if candidate_notional > max_candidate_notional:
                issue_suffix = ""
                detail_parts: List[str] = []
                if intent.rationale_items and evidence_quality.get("issues"):
                    detail_parts.append(f"evidence_quality={','.join(evidence_quality['issues'])}")
                if cap_summary.get("profile_issues"):
                    detail_parts.append(f"profile_adjustments={','.join(cap_summary['profile_issues'])}")
                if detail_parts:
                    issue_suffix = " " + " ".join(detail_parts) + "."
                return False, (
                    f"Risk profile {active_mode} limits candidate size to "
                    f"{max_candidate_notional:.2f}, got {candidate_notional:.2f}."
                    f"{issue_suffix}"
                )

        if risk_advisories:
            summarized = "; ".join(dict.fromkeys(risk_advisories))
            return True, f"Approved with risk advisories: {summarized}"

        return True, "Approved"
