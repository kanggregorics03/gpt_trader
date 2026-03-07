import yaml
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
                "block_additional_entries_when_position_exists": True,
            },
            "cooldown": {
                "max_open_positions": base_max_positions,
                "max_order_notional_pct_balance": 0.0,
                "allowed_actions": ["HOLD", "REDUCE", "EXIT"],
            },
            "drawdown": {
                "max_open_positions": max(1, min(base_max_positions, 1)),
                "max_order_notional_pct_balance": 20.0,
                "entry_size_multiplier": 0.75,
                "confidence_cap": 0.55,
                "failure_pattern_size_multiplier": 0.75,
                "risk_trigger_size_multiplier": 0.85,
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
        risk_state: Optional[Dict] = None,
        market_snapshots: Optional[Dict[str, MarketSnapshot]] = None,
    ) -> Dict[str, object]:
        active_profile = active_profile or {}
        risk_state = risk_state or {}
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
            max(0.0, float(risk_state.get("failure_pattern_score", 0.0) or 0.0)),
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
        risk_state: Optional[Dict[str, float]] = None,
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
        risk_state = risk_state or {}
        active_mode = self.get_active_risk_mode(risk_state=risk_state, is_trigger_mode=is_trigger_mode)
        active_profile = active_profile_override or self.get_active_risk_profile(
            risk_state=risk_state,
            is_trigger_mode=is_trigger_mode,
        )
        is_entry_decision = intent.decision in ("BUY", "SELL")
        is_risk_reducing = intent.decision in ("REDUCE", "EXIT")

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

        allowed_actions = set(active_profile.get("allowed_actions", []))
        if allowed_actions and intent.decision not in allowed_actions:
            return False, f"Risk profile {active_mode} blocks action {intent.decision}."

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
                effective_window = int(risk_state.get("cooldown_minutes_effective", 0) or 0)
                pattern_reason = str(risk_state.get("dominant_failure_reason") or "").strip()
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
        if is_entry_decision:
            new_symbols = {
                order.symbol
                for order in intent.orders
                if not order.reduce_only
            }
            pending_symbols = {
                order.symbol
                for order in account_state.open_orders
                if is_pending_entry_order(order)
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
        allowed_symbols = trading_policy.get('symbols', [])
        policy_order_type = str(trading_policy.get("order_type", "market")).upper()
        max_leverage = float(trading_policy.get("max_leverage", 0) or 0)
        risk_advisories: List[str] = []

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
                spread_bps = (
                    symbol_market_snapshot.order_book.spread_bps
                    if symbol_market_snapshot is not None
                    else None
                )
                if spread_bps is not None:
                    risk_advisories.append(f"{order.symbol}: wide_spread={float(spread_bps):.2f}bps")
                else:
                    risk_advisories.append(f"{order.symbol}: wide_spread")

            if is_entry_decision and symbol_market_snapshot is not None:
                funding_rate = symbol_market_snapshot.derivatives.funding_rate
                if "elevated_funding" in risk_trigger_reasons and funding_rate is not None:
                    unfavorable_long = funding_rate > 0 and order.action == "BUY"
                    unfavorable_short = funding_rate < 0 and order.action == "SELL"
                    if unfavorable_long or unfavorable_short:
                        risk_advisories.append(
                            f"{order.symbol}: elevated_funding_unfavorable={float(funding_rate):.6f}"
                        )

        if is_entry_decision and max_leverage > 0:
            current_exposure = sum(abs(pos.size) * pos.entry_price for pos in account_state.positions)
            pending_exposure = 0.0
            for pending_order in account_state.open_orders:
                if not is_pending_entry_order(pending_order):
                    continue
                pending_price = pending_order.price or market_prices.get(pending_order.symbol)
                if pending_price is None:
                    pending_price = 0.0
                pending_exposure += abs(float(pending_order.quantity or 0.0)) * float(pending_price)
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
        if is_entry_decision and max_order_notional_pct_balance >= 0:
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
                if not is_pending_entry_order(pending_order):
                    continue
                pending_price = pending_order.price or market_prices.get(pending_order.symbol)
                if pending_price is None:
                    pending_price = 0.0
                pending_notional += abs(float(pending_order.quantity or 0.0)) * float(pending_price)
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
