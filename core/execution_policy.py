from __future__ import annotations

from typing import Any, Dict, Optional

from core.models import AccountState, MarketSnapshot, OrderIntent, TradeIntent


class ExecutionPolicyEngine:
    DEFAULT_CONFIG = {
        "allowed_order_types": ["MARKET", "LIMIT"],
        "default_entry_order_type": "LIMIT",
        "default_reduce_order_type": "MARKET",
        "default_exit_order_type": "MARKET",
        "prefer_market_when_trigger_mode": True,
        "prefer_market_spread_percentile_lte": 35.0,
        "prefer_limit_spread_percentile_gte": 80.0,
        "prefer_limit_depth_percentile_lte": 20.0,
        "aggressive_limit_offset_bps": 3.0,
        "passive_limit_offset_bps": 8.0,
        "market_max_slippage_bps": 18.0,
        "limit_max_slippage_bps": 10.0,
        "profile_min_support": 4,
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
        is_trigger_mode: bool,
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
                        "reduce_only": bool(order.reduce_only),
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
                is_trigger_mode=is_trigger_mode,
            )
            recommendation = self._get_profile_recommendation(order.symbol, context)
            execution_profile = self._choose_execution_profile(order, context, recommendation)
            updated_order = self._apply_execution_profile(order, snapshot, context, execution_profile)
            updated_orders.append(updated_order)
            order_summaries.append(
                {
                    "symbol": order.symbol,
                    "action": order.action,
                    "reduce_only": bool(order.reduce_only),
                    "execution_profile": updated_order.execution_profile,
                    "order_type": updated_order.order_type,
                    "price": updated_order.price,
                    "urgency_bucket": context["urgency_bucket"],
                    "liquidity_bucket": context["liquidity_bucket"],
                    "profile_source": recommendation.get("source") if recommendation else "heuristic",
                }
            )

        return intent.model_copy(update={"orders": updated_orders}), {
            "applied": True,
            "loop_mode": loop_mode,
            "risk_mode": risk_mode,
            "trigger_mode": bool(is_trigger_mode),
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
        is_trigger_mode: bool,
    ) -> Dict[str, Any]:
        spread_percentile = self._safe_float(getattr(snapshot, "spread_percentile_1h", None))
        depth_percentile = self._safe_float(getattr(snapshot, "depth_percentile_1h", None))
        spread_bps = self._safe_float(getattr(getattr(snapshot, "order_book", None), "spread_bps", None))
        opportunity_score = 0.0
        risk_score = 0.0
        if snapshot is not None:
            opportunity_score += min(2.0, float(len(getattr(snapshot, "opportunity_trigger_reasons", []) or [])) * 0.4)
            risk_score += min(2.0, float(len(getattr(snapshot, "risk_trigger_reasons", []) or [])) * 0.5)
            if bool(getattr(snapshot, "is_opportunity_trigger", False)):
                opportunity_score += 0.6
            if bool(getattr(snapshot, "is_risk_trigger", False)):
                risk_score += 0.8
        if bool(order.reduce_only) or intent.decision in ("REDUCE", "EXIT"):
            urgency_score = 0.9
        else:
            urgency_score = 0.45
            if loop_mode == "5m":
                urgency_score += 0.25
            if is_trigger_mode and self.config.get("prefer_market_when_trigger_mode", True):
                urgency_score += 0.15
            urgency_score += min(0.15, opportunity_score * 0.05)
            urgency_score -= min(0.2, risk_score * 0.05)
        if spread_percentile is not None and spread_percentile >= float(self.config["prefer_limit_spread_percentile_gte"]):
            urgency_score -= 0.15
        if depth_percentile is not None and depth_percentile <= float(self.config["prefer_limit_depth_percentile_lte"]):
            urgency_score -= 0.1
        urgency_score = max(0.0, min(1.0, urgency_score))

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

        action_type = "entry"
        if order.reduce_only or intent.decision == "REDUCE":
            action_type = "reduce"
        if intent.decision == "EXIT":
            action_type = "exit"

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
            "is_trigger_mode": bool(is_trigger_mode),
            "action_type": action_type,
            "urgency_score": round(urgency_score, 4),
            "urgency_bucket": urgency_bucket,
            "liquidity_bucket": liquidity_bucket,
            "spread_percentile_1h": spread_percentile,
            "depth_percentile_1h": depth_percentile,
            "spread_bps": spread_bps,
            "unrealized_pnl_pct": unrealized_pnl_pct,
        }

    def _get_profile_recommendation(self, symbol: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if self.state_store is None or not hasattr(self.state_store, "get_execution_policy_profile"):
            return None
        for candidate_symbol, source in ((symbol, "symbol"), ("*", "global")):
            row = self.state_store.get_execution_policy_profile(
                symbol=candidate_symbol,
                loop_mode=context["loop_mode"],
                action_type=context["action_type"],
                urgency_bucket=context["urgency_bucket"],
                liquidity_bucket=context["liquidity_bucket"],
                risk_mode=context["risk_mode"],
            )
            if not row:
                continue
            support = int((row.get("metrics") or {}).get("sample_count") or 0)
            if support < int(self.config["profile_min_support"]):
                continue
            recommended_profile = str(row.get("recommended_execution_profile") or "").strip()
            if not recommended_profile:
                continue
            return {
                **row,
                "recommended_execution_profile": recommended_profile,
                "source": source,
            }
        return None

    def _choose_execution_profile(
        self,
        order: OrderIntent,
        context: Dict[str, Any],
        recommendation: Optional[Dict[str, Any]],
    ) -> str:
        if recommendation is not None:
            return str(recommendation["recommended_execution_profile"])

        action_type = context["action_type"]
        urgency_score = float(context["urgency_score"])
        liquidity_bucket = context["liquidity_bucket"]
        spread_percentile = self._safe_float(context.get("spread_percentile_1h"))

        if action_type == "exit":
            return "market_urgent"
        if action_type == "reduce":
            unrealized_pnl_pct = self._safe_float(context.get("unrealized_pnl_pct"))
            if liquidity_bucket == "poor" and unrealized_pnl_pct is not None and unrealized_pnl_pct > 0:
                return "limit_aggressive"
            return "market_urgent"

        if liquidity_bucket == "poor":
            return "limit_passive"
        if urgency_score >= 0.8:
            return "market_urgent"
        if context["loop_mode"] == "5m":
            return "market_urgent"
        if context["is_trigger_mode"] and spread_percentile is not None and spread_percentile <= 55.0:
            return "limit_aggressive"
        return "limit_passive"

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
            price = self._build_limit_price(order, reference_price, snapshot, limit_offset_bps, post_only)
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
        limit_offset_bps: float,
        post_only: bool,
    ) -> Optional[float]:
        if reference_price is None or reference_price <= 0:
            return order.price
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
