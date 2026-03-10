from __future__ import annotations

from typing import Any, Dict, Optional

from core.models import AccountState, MarketSnapshot, OrderIntent, TradeIntent


class ExecutionPolicyEngine:
    DEFAULT_CONFIG = {
        "allowed_order_types": ["MARKET", "LIMIT"],
        "default_entry_order_type": "LIMIT",
        "default_reduce_order_type": "MARKET",
        "default_exit_order_type": "MARKET",
        "preserve_market_entry_semantics": True,
        "aggressive_limit_offset_bps": 3.0,
        "passive_limit_offset_bps": 8.0,
        "value_limit_min_offset_bps": 8.0,
        "value_limit_max_offset_bps": 24.0,
        "value_limit_spread_multiplier": 1.5,
        "value_limit_atr_fraction": 0.12,
        "market_max_slippage_bps": 18.0,
        "limit_max_slippage_bps": 10.0,
    }

    def __init__(self, policy: Optional[Dict[str, Any]] = None, state_store=None):
        trading_policy = ((policy or {}).get("trading") or {})
        execution_policy = ((policy or {}).get("execution_policy") or {})
        config = dict(self.DEFAULT_CONFIG)
        config.update(execution_policy)
        legacy_order_type = str(trading_policy.get("order_type") or "").strip().upper()
        if legacy_order_type and "default_entry_order_type" not in execution_policy:
            config["default_entry_order_type"] = legacy_order_type
        self.config = config
        self.state_store = state_store

    def apply_to_intent(
        self,
        intent: TradeIntent,
        market_snapshots: Dict[str, MarketSnapshot],
        account_state: Optional[AccountState],
        loop_mode: str,
        risk_mode: str,
        is_guarded_attention: bool,
    ) -> tuple[TradeIntent, Dict[str, Any]]:
        if intent.decision == "HOLD" or not intent.orders:
            return intent, {"applied": False, "reason": "no_orders"}
        if all(str(getattr(order, "action", "")).upper() == "CANCEL" for order in (intent.orders or [])):
            return intent, {"applied": False, "reason": "order_management"}

        updated_orders = []
        order_summaries = []
        for order in intent.orders:
            if str(getattr(order, "action", "")).upper() == "CANCEL":
                updated_orders.append(order)
                order_summaries.append(
                    {
                        "symbol": order.symbol,
                        "action": order.action,
                        "action_type": "cancel",
                        "reduce_only": bool(order.reduce_only),
                        "entry_style": getattr(order, "entry_style", None),
                        "execution_profile": getattr(order, "execution_profile", None),
                        "order_type": getattr(order, "order_type", None),
                        "price": getattr(order, "price", None),
                        "urgency_bucket": "order_management",
                        "liquidity_bucket": "order_management",
                        "profile_source": "order_management",
                    }
                )
                continue
            snapshot = market_snapshots.get(order.symbol)
            context = self._build_order_context(
                order=order,
                intent=intent,
                snapshot=snapshot,
                account_state=account_state,
                loop_mode=loop_mode,
                risk_mode=risk_mode,
                is_guarded_attention=is_guarded_attention,
            )
            execution_profile = self._choose_execution_profile(context)
            execution_profile, contract_adjustment = self._enforce_execution_contract(
                order,
                context,
                execution_profile,
            )
            updated_order = self._apply_execution_profile(order, snapshot, context, execution_profile)
            updated_orders.append(updated_order)
            order_summaries.append(
                {
                    "symbol": order.symbol,
                    "action": order.action,
                    "action_type": context["action_type"],
                    "intent_order_type": getattr(order, "order_type", None),
                    "entry_style": context.get("entry_style"),
                    "reduce_only": bool(order.reduce_only),
                    "execution_profile": updated_order.execution_profile,
                    "order_type": updated_order.order_type,
                    "price": updated_order.price,
                    "urgency_bucket": context["urgency_bucket"],
                    "liquidity_bucket": context["liquidity_bucket"],
                    "profile_source": "deterministic_contract",
                    "contract_adjustment": contract_adjustment,
                }
            )

        return intent.model_copy(update={"orders": updated_orders}), {
            "applied": True,
            "loop_mode": loop_mode,
            "risk_mode": risk_mode,
            "guarded_attention": bool(is_guarded_attention),
            "orders": order_summaries,
        }

    def _build_order_context(
        self,
        order: OrderIntent,
        intent: TradeIntent,
        snapshot: Optional[MarketSnapshot],
        account_state: Optional[AccountState],
        loop_mode: str,
        risk_mode: str,
        is_guarded_attention: bool,
    ) -> Dict[str, Any]:
        spread_percentile = self._safe_float(getattr(snapshot, "spread_percentile_1h", None))
        depth_percentile = self._safe_float(getattr(snapshot, "depth_percentile_1h", None))
        spread_bps = self._safe_float(getattr(getattr(snapshot, "order_book", None), "spread_bps", None))
        action_type = "entry"
        if order.reduce_only or intent.decision == "REDUCE":
            action_type = "reduce"
        if intent.decision == "EXIT":
            action_type = "exit"
        explicit_entry_style = str(getattr(order, "entry_style", "") or "").strip().lower()
        if explicit_entry_style == "pullback":
            explicit_entry_style = "value"
        if explicit_entry_style not in {"immediate", "retest", "value"}:
            explicit_entry_style = "immediate" if str(getattr(order, "order_type", "") or "").upper() == "MARKET" else "value"
        intent_order_type = str(getattr(order, "order_type", "") or "").upper()

        if action_type in {"reduce", "exit"}:
            urgency_score = 0.95
        elif intent_order_type == "MARKET":
            urgency_score = 0.85
        elif explicit_entry_style == "immediate":
            urgency_score = 0.7
        else:
            urgency_score = 0.35

        if urgency_score >= 0.8:
            urgency_bucket = "high"
        elif urgency_score >= 0.55:
            urgency_bucket = "medium"
        else:
            urgency_bucket = "low"

        if spread_percentile is None and depth_percentile is None:
            liquidity_bucket = "unknown"
        elif (spread_percentile is not None and spread_percentile >= 80.0) or (
            depth_percentile is not None and depth_percentile <= 20.0
        ):
            liquidity_bucket = "poor"
        elif (spread_percentile is not None and spread_percentile <= 35.0) and (
            depth_percentile is None or depth_percentile >= 45.0
        ):
            liquidity_bucket = "good"
        else:
            liquidity_bucket = "mixed"

        current_position = None
        for position in getattr(account_state, "positions", []) or []:
            if getattr(position, "symbol", None) == order.symbol:
                current_position = position
                break
        unrealized_pnl_pct = self._safe_float(
            getattr(current_position, "unrealized_pnl_pct", None) if current_position is not None else None
        )

        return {
            "loop_mode": loop_mode,
            "risk_mode": risk_mode,
            "is_guarded_attention": bool(is_guarded_attention),
            "intent_order_type": intent_order_type,
            "entry_style": explicit_entry_style if action_type == "entry" else None,
            "action_type": action_type,
            "urgency_score": round(urgency_score, 4),
            "urgency_bucket": urgency_bucket,
            "liquidity_bucket": liquidity_bucket,
            "spread_percentile_1h": spread_percentile,
            "depth_percentile_1h": depth_percentile,
            "spread_bps": spread_bps,
            "unrealized_pnl_pct": unrealized_pnl_pct,
        }

    def _choose_execution_profile(
        self,
        context: Dict[str, Any],
    ) -> str:
        action_type = context["action_type"]
        entry_style = str(context.get("entry_style") or "").strip().lower()
        intent_order_type = str(context.get("intent_order_type") or "").upper()

        if action_type == "exit":
            return "market_urgent"
        if action_type == "reduce":
            return "market_urgent"
        if intent_order_type == "MARKET":
            return "market_urgent"
        if intent_order_type == "LIMIT" and entry_style == "immediate":
            return "limit_aggressive"
        return "limit_passive"

    def _enforce_execution_contract(
        self,
        order: OrderIntent,
        context: Dict[str, Any],
        execution_profile: str,
    ) -> tuple[str, str | None]:
        if not bool(self.config.get("preserve_market_entry_semantics", True)):
            return execution_profile, None
        action_type = str(context.get("action_type") or "").strip().lower()
        intent_order_type = str(context.get("intent_order_type") or getattr(order, "order_type", "") or "").upper()
        entry_style = str(context.get("entry_style") or "").strip().lower()
        if action_type == "exit" and execution_profile != "market_urgent":
            return "market_urgent", "exit_market_only"
        if action_type == "reduce" and execution_profile != "market_urgent":
            return "market_urgent", "reduce_market_only"
        if action_type != "entry":
            return execution_profile, None
        if entry_style == "immediate":
            if intent_order_type == "MARKET" and execution_profile != "market_urgent":
                return "market_urgent", "market_entry_kept_immediate"
            if intent_order_type == "LIMIT" and execution_profile != "limit_aggressive":
                return "limit_aggressive", "limit_entry_kept_immediate"
        if entry_style in {"retest", "value"} and execution_profile != "limit_passive":
            return "limit_passive", f"{entry_style}_entry_kept_passive"
        return execution_profile, None

    def _apply_execution_profile(
        self,
        order: OrderIntent,
        snapshot: Optional[MarketSnapshot],
        context: Dict[str, Any],
        execution_profile: str,
    ) -> OrderIntent:
        order_type = "MARKET" if execution_profile == "market_urgent" else "LIMIT"
        allowed_order_types = {
            str(item).upper()
            for item in (self.config.get("allowed_order_types") or [])
            if str(item).strip()
        }
        if allowed_order_types:
            if order_type not in allowed_order_types:
                if "LIMIT" in allowed_order_types:
                    execution_profile = "limit_aggressive"
                    order_type = "LIMIT"
                else:
                    execution_profile = "market_urgent"
                    order_type = "MARKET"
        reference_price = self._get_reference_price(snapshot)
        if order_type == "MARKET":
            price = None
            limit_offset_bps = None
            post_only = False
            max_slippage_bps = float(self.config["market_max_slippage_bps"])
            time_in_force = None
        else:
            post_only = execution_profile == "limit_passive"
            limit_offset_bps = (
                float(self.config["passive_limit_offset_bps"])
                if execution_profile == "limit_passive"
                else float(self.config["aggressive_limit_offset_bps"])
            )
            if post_only and str(context.get("entry_style") or "").strip().lower() == "value":
                limit_offset_bps = self._resolve_value_limit_offset_bps(
                    reference_price=reference_price,
                    snapshot=snapshot,
                    base_offset_bps=limit_offset_bps,
                )
            price = self._build_limit_price(order, reference_price, snapshot, context, limit_offset_bps, post_only)
            max_slippage_bps = float(self.config["limit_max_slippage_bps"])
            time_in_force = "GTC"

        return order.model_copy(
            update={
                "order_type": order_type,
                "price": price,
                "execution_profile": execution_profile,
                "urgency_score": float(context["urgency_score"]),
                "max_slippage_bps": max_slippage_bps,
                "time_in_force": time_in_force,
                "post_only": post_only,
                "limit_offset_bps": limit_offset_bps,
                "execution_context": dict(context),
            }
        )

    def _build_limit_price(
        self,
        order: OrderIntent,
        reference_price: Optional[float],
        snapshot: Optional[MarketSnapshot],
        context: Dict[str, Any],
        limit_offset_bps: float,
        post_only: bool,
    ) -> Optional[float]:
        if reference_price is None or reference_price <= 0:
            return order.price
        entry_style = str(context.get("entry_style") or "").strip().lower()
        if post_only and entry_style == "retest":
            anchored = self._build_retest_limit_price(order, reference_price, snapshot)
            if anchored is not None:
                return anchored
        order_book = getattr(snapshot, "order_book", None)
        best_bid = self._safe_float(getattr(order_book, "best_bid", None))
        best_ask = self._safe_float(getattr(order_book, "best_ask", None))
        offset_ratio = limit_offset_bps / 10000.0
        if order.action == "BUY":
            candidate = reference_price * (1.0 - offset_ratio)
            if not post_only and best_ask is not None:
                candidate = min(best_ask, reference_price * (1.0 + offset_ratio))
            elif post_only and best_bid is not None:
                candidate = min(candidate, best_bid)
        else:
            candidate = reference_price * (1.0 + offset_ratio)
            if not post_only and best_bid is not None:
                candidate = max(best_bid, reference_price * (1.0 - offset_ratio))
            elif post_only and best_ask is not None:
                candidate = max(candidate, best_ask)
        return round(candidate, 8)

    def _resolve_value_limit_offset_bps(
        self,
        *,
        reference_price: Optional[float],
        snapshot: Optional[MarketSnapshot],
        base_offset_bps: float,
    ) -> float:
        min_offset = max(0.0, float(self.config.get("value_limit_min_offset_bps", base_offset_bps) or base_offset_bps))
        max_offset = max(min_offset, float(self.config.get("value_limit_max_offset_bps", min_offset) or min_offset))
        spread_multiplier = max(0.0, float(self.config.get("value_limit_spread_multiplier", 1.5) or 1.5))
        atr_fraction = max(0.0, float(self.config.get("value_limit_atr_fraction", 0.12) or 0.12))
        spread_bps = self._safe_float(getattr(getattr(snapshot, "order_book", None), "spread_bps", None)) or 0.0
        atr_value = self._safe_float(getattr(snapshot, "atr_15m", None))
        if atr_value is None or atr_value <= 0:
            atr_value = self._safe_float(getattr(snapshot, "atr_1h", None))
        atr_component_bps = 0.0
        if reference_price is not None and reference_price > 0 and atr_value is not None and atr_value > 0:
            atr_component_bps = (atr_value / reference_price) * 10000.0 * atr_fraction
        adaptive_offset = max(base_offset_bps, min_offset, spread_bps * spread_multiplier, atr_component_bps)
        return round(min(max_offset, adaptive_offset), 4)

    def _build_retest_limit_price(
        self,
        order: OrderIntent,
        reference_price: float,
        snapshot: Optional[MarketSnapshot],
    ) -> Optional[float]:
        if snapshot is None:
            return None
        order_book = getattr(snapshot, "order_book", None)
        best_bid = self._safe_float(getattr(order_book, "best_bid", None))
        best_ask = self._safe_float(getattr(order_book, "best_ask", None))
        breakout_offsets = []
        distance_15m = self._safe_float(getattr(snapshot, "distance_to_15m_breakout_level", None))
        atr_15m = self._safe_float(getattr(snapshot, "atr_15m", None))
        if distance_15m is not None and atr_15m is not None and distance_15m >= 0 and atr_15m > 0:
            breakout_offsets.append(distance_15m * atr_15m)
        distance_1h = self._safe_float(getattr(snapshot, "distance_to_1h_breakout_level", None))
        atr_1h = self._safe_float(getattr(snapshot, "atr_1h", None))
        if distance_1h is not None and atr_1h is not None and distance_1h >= 0 and atr_1h > 0:
            breakout_offsets.append(distance_1h * atr_1h)
        if not breakout_offsets:
            return None
        offset = min(breakout_offsets)
        if order.action == "BUY":
            candidate = max(0.0, reference_price - offset)
            if best_bid is not None:
                candidate = min(candidate, best_bid)
        else:
            candidate = reference_price + offset
            if best_ask is not None:
                candidate = max(candidate, best_ask)
        return round(candidate, 8)

    def _get_reference_price(self, snapshot: Optional[MarketSnapshot]) -> Optional[float]:
        if snapshot is None:
            return None
        order_book = getattr(snapshot, "order_book", None)
        for candidate in (
            getattr(order_book, "mid_price", None),
            getattr(order_book, "microprice", None),
            getattr(snapshot, "mark_price", None),
            getattr(snapshot, "current_price", None),
        ):
            value = self._safe_float(candidate)
            if value is not None and value > 0:
                return value
        return None

    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
