from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Dict, Optional

from core.models import AccountState, OrderIntent


class SafetyPolicyEngine:
    DEFAULT_CONFIG = {
        "loss_exit_pct": -0.9,
        "risk_trigger_exit_count": 1,
        "microstructure_exit_spread_percentile": 85.0,
        "microstructure_exit_depth_percentile": 15.0,
        "profit_protect_trigger_pct": 1.0,
        "profit_protect_reduce_fraction": 0.35,
        "stale_order_minutes": 12.0,
        "stale_order_risk_trigger_count": 1,
        "stale_order_reprice_enabled": True,
        "stale_order_reprice_spread_percentile_max": 70.0,
        "stale_order_reprice_depth_percentile_min": 30.0,
        "stale_order_reprice_limit_offset_bps": 3.0,
    }

    def __init__(self, policy: Optional[Dict[str, Any]] = None, state_store=None):
        configured = ((policy or {}).get("safety_policy") or {})
        self.config = dict(self.DEFAULT_CONFIG)
        self.config.update(configured)
        self.state_store = state_store

    def evaluate(
        self,
        account_state: AccountState,
        market_snapshots: Dict[str, Any],
        risk_mode: str,
        loop_mode: str,
        regime_key: Optional[str] = None,
        policy_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        config = dict(self.config)
        if self.state_store is not None and hasattr(self.state_store, "get_policy_registry_entry"):
            registry = {}
            scoped_fetch = getattr(self.state_store, "get_policy_registry_entry_for_regime", None)
            if callable(scoped_fetch):
                registry = scoped_fetch("safety_5m", regime_key=regime_key)
            if not registry:
                registry = self.state_store.get_policy_registry_entry("champion:safety_5m")
            if registry:
                config.update(registry.get("config") or {})
        if policy_override:
            config.update(policy_override)

        positions = [
            position
            for position in (getattr(account_state, "positions", []) or [])
            if abs(float(getattr(position, "size", 0.0) or 0.0)) > 0
        ]
        open_orders = list(getattr(account_state, "open_orders", []) or [])
        if not positions and not open_orders:
            return {
                "escalate_to_ai": False,
                "decision_result": {
                    "decision": "HOLD",
                    "orders": [],
                    "rationale": ["5m safety loop stays flat and leaves new entries to the 1h alpha loop."],
                    "risk_notes": ["safety_policy:flat_hold", f"risk_mode:{risk_mode}"],
                    "confidence": 0.96,
                    "metadata": {
                        "policy_source": "safety_fsm",
                        "loop_mode": loop_mode,
                        "state": "flat",
                    },
                },
            }

        if not positions and open_orders:
            stale_orders = self._select_stale_open_orders(
                open_orders,
                threshold_minutes=float(config["stale_order_minutes"]),
            )
            if stale_orders:
                replacement_order = self._build_replacement_order(
                    stale_orders[0],
                    snapshot=(market_snapshots or {}).get(str(getattr(stale_orders[0], "symbol", "") or "")),
                    config=config,
                ) if len(stale_orders) == 1 else None
                if replacement_order is not None:
                    return {
                        "escalate_to_ai": False,
                        "decision_result": {
                            "decision": "REPLACE_ENTRY",
                            "orders": [self._build_cancel_order(stale_orders[0]), replacement_order],
                            "rationale": [
                                f"5m safety loop reprices a stale working order for {replacement_order.symbol}."
                            ],
                            "risk_notes": ["safety_policy:stale_order_reprice", f"risk_mode:{risk_mode}"],
                            "confidence": 0.88,
                            "exit_reason": "order_reprice",
                            "metadata": {
                                "policy_source": "safety_fsm",
                                "loop_mode": loop_mode,
                                "state": "working_order_reprice",
                            },
                        },
                    }
                return {
                    "escalate_to_ai": False,
                    "decision_result": {
                        "decision": "CANCEL",
                        "orders": [self._build_cancel_order(order) for order in stale_orders],
                        "rationale": [
                            f"5m safety loop cancels {len(stale_orders)} stale working order(s) older than {float(config['stale_order_minutes']):.1f}m."
                        ],
                        "risk_notes": ["safety_policy:stale_working_orders", f"risk_mode:{risk_mode}"],
                        "confidence": 0.91,
                        "exit_reason": "order_cleanup",
                        "metadata": {
                            "policy_source": "safety_fsm",
                            "loop_mode": loop_mode,
                            "state": "working_orders_only",
                        },
                    },
                }

        per_symbol = []
        decisive_actions = []
        for position in positions:
            symbol = str(getattr(position, "symbol", "") or "")
            if not symbol:
                continue
            snapshot = (market_snapshots or {}).get(symbol)
            symbol_orders = [order for order in open_orders if str(getattr(order, "symbol", "") or "") == symbol]
            state = self._evaluate_symbol_state(
                position=position,
                snapshot=snapshot,
                open_orders=symbol_orders,
                config=config,
            )
            per_symbol.append(state)
            if state["action"] in ("REDUCE", "EXIT"):
                decisive_actions.append(state)

        stale_order_symbols = self._build_stale_open_order_notes(open_orders, config)
        if not per_symbol:
            rationale = stale_order_symbols or ["5m safety loop is monitoring working orders only."]
            return {
                "escalate_to_ai": False,
                "decision_result": {
                    "decision": "HOLD",
                    "orders": [],
                    "rationale": rationale,
                    "risk_notes": ["safety_policy:working_orders", f"risk_mode:{risk_mode}"],
                    "confidence": 0.9,
                    "metadata": {
                        "policy_source": "safety_fsm",
                        "loop_mode": loop_mode,
                        "state": "working_orders_only",
                    },
                },
            }

        decisive_actions.sort(key=lambda item: (item["severity"], item["symbol"]), reverse=True)
        if not decisive_actions:
            hold_rationale = [f"{item['symbol']} state={item['state']} reason={item['reason']}" for item in per_symbol[:4]]
            hold_rationale.extend(stale_order_symbols[:2])
            return {
                "escalate_to_ai": False,
                "decision_result": {
                    "decision": "HOLD",
                    "orders": [],
                    "rationale": hold_rationale or ["5m safety policy keeps exposure unchanged."],
                    "risk_notes": [f"safety_policy:{item['state']}" for item in per_symbol[:4]],
                    "confidence": 0.9,
                    "metadata": {
                        "policy_source": "safety_fsm",
                        "per_symbol": per_symbol,
                        "loop_mode": loop_mode,
                    },
                },
            }

        top = decisive_actions[0]
        return {
            "escalate_to_ai": False,
            "decision_result": {
                "decision": top["action"],
                "orders": [top["order"]],
                "rationale": [top["reason"]],
                "risk_notes": [f"safety_policy:{top['state']}", f"risk_mode:{risk_mode}"],
                "confidence": 0.92 if top["action"] == "EXIT" else 0.86,
                "exit_reason": top.get("exit_reason"),
                "metadata": {
                    "policy_source": "safety_fsm",
                    "per_symbol": per_symbol,
                    "loop_mode": loop_mode,
                },
            },
        }

    def _evaluate_symbol_state(self, position, snapshot, open_orders, config: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(getattr(position, "symbol", "") or "")
        pnl_pct = self._safe_float(getattr(position, "unrealized_pnl_pct", None)) or 0.0
        spread_percentile = self._safe_float(getattr(snapshot, "spread_percentile_1h", None))
        depth_percentile = self._safe_float(getattr(snapshot, "depth_percentile_1h", None))
        risk_trigger_reasons = list(getattr(snapshot, "risk_trigger_reasons", []) or []) if snapshot is not None else []
        state = "stable"
        action = "HOLD"
        reason = f"{symbol} remains inside the 5m safety envelope."
        exit_reason = None
        reduce_fraction = float(config["profit_protect_reduce_fraction"])
        severity = 0.1

        microstructure_degraded = (
            spread_percentile is not None
            and spread_percentile >= float(config["microstructure_exit_spread_percentile"])
            and depth_percentile is not None
            and depth_percentile <= float(config["microstructure_exit_depth_percentile"])
        )
        stale_orders = self._count_stale_orders(open_orders, float(config["stale_order_minutes"]))

        if pnl_pct <= float(config["loss_exit_pct"]):
            state = "acute_loss"
            action = "EXIT"
            reason = f"{symbol} breached the 5m safety loss threshold at {pnl_pct:.2f}%."
            exit_reason = "emergency_exit"
            severity = 1.0
        elif len(risk_trigger_reasons) >= int(config["risk_trigger_exit_count"]) and microstructure_degraded:
            state = "liquidity_risk"
            action = "EXIT"
            reason = f"{symbol} has concurrent risk triggers and degraded microstructure."
            exit_reason = "liquidity_exit"
            severity = 0.98
        elif stale_orders >= 1 and len(risk_trigger_reasons) >= int(config["stale_order_risk_trigger_count"]) and microstructure_degraded:
            state = "stale_order_risk"
            action = "EXIT"
            reason = f"{symbol} has stale working orders while trigger quality deteriorates."
            exit_reason = "emergency_exit"
            severity = 0.94
        elif pnl_pct >= float(config["profit_protect_trigger_pct"]) and len(risk_trigger_reasons) >= 1:
            state = "protect_profit"
            action = "REDUCE"
            reason = f"{symbol} is in profit but 5m risk signals increased, so safety policy trims exposure."
            exit_reason = "scale_out"
            severity = 0.68

        order = None
        if action in ("REDUCE", "EXIT"):
            order = self._build_reduce_order(position, action, reduce_fraction)

        return {
            "symbol": symbol,
            "state": state,
            "action": action,
            "reason": reason,
            "exit_reason": exit_reason,
            "severity": severity,
            "unrealized_pnl_pct": pnl_pct,
            "stale_order_count": stale_orders,
            "order": order,
        }

    def _build_stale_open_order_notes(self, open_orders, config: Dict[str, Any]) -> list[str]:
        threshold = float(config["stale_order_minutes"])
        notes = []
        for order in open_orders or []:
            age_minutes = self._calculate_order_age_minutes(getattr(order, "timestamp", None))
            if age_minutes is None or age_minutes < threshold:
                continue
            symbol = str(getattr(order, "symbol", "") or "portfolio")
            notes.append(f"{symbol} has a working order older than {age_minutes:.1f}m; safety loop keeps monitoring.")
        return notes

    def _count_stale_orders(self, open_orders, threshold_minutes: float) -> int:
        count = 0
        for order in open_orders or []:
            age_minutes = self._calculate_order_age_minutes(getattr(order, "timestamp", None))
            if age_minutes is not None and age_minutes >= threshold_minutes:
                count += 1
        return count

    def _calculate_order_age_minutes(self, timestamp_value: Any) -> Optional[float]:
        try:
            if timestamp_value in (None, ""):
                return None
            raw_value = float(timestamp_value)
        except (TypeError, ValueError):
            return None
        if raw_value > 10_000_000_000:
            raw_value /= 1000.0
        now_epoch = datetime.now(UTC).timestamp()
        return round(max(0.0, now_epoch - raw_value) / 60.0, 2)

    def _build_reduce_order(self, position, action: str, reduce_fraction: float):
        size = abs(float(getattr(position, "size", 0.0) or 0.0))
        if action == "EXIT":
            quantity = size
        else:
            quantity = max(0.0, round(size * float(reduce_fraction), 8))
            if quantity <= 0.0:
                quantity = size
        side = str(getattr(position, "side", "") or "").upper()
        order_action = "SELL" if side == "LONG" else "BUY"
        return OrderIntent(
            symbol=str(getattr(position, "symbol", "") or ""),
            action=order_action,
            order_type="MARKET",
            quantity=quantity,
            reduce_only=True,
            execution_profile="market_urgent",
            urgency_score=0.97,
            execution_context={"action_type": "exit" if action == "EXIT" else "reduce", "policy_source": "safety_fsm"},
        )

    def _build_cancel_order(self, open_order):
        replace_group_id = self._build_replace_group_id(open_order)
        return OrderIntent(
            symbol=str(getattr(open_order, "symbol", "") or ""),
            action="CANCEL",
            order_type="MARKET",
            quantity=0.0,
            target_order_id=getattr(open_order, "order_id", None),
            target_client_order_id=getattr(open_order, "client_order_id", None),
            replace_group_id=replace_group_id,
            lineage_role="cancel",
            lineage_parent_order_id=getattr(open_order, "order_id", None),
            lineage_parent_client_order_id=getattr(open_order, "client_order_id", None),
            execution_context={"action_type": "cancel", "policy_source": "safety_fsm", "replace_group_id": replace_group_id},
        )

    def _build_replacement_order(self, open_order, snapshot, config: Dict[str, Any]):
        if not bool(config.get("stale_order_reprice_enabled", True)):
            return None
        if bool(getattr(open_order, "reduce_only", False)):
            return None
        side = str(getattr(open_order, "side", "") or "").upper()
        if side not in ("BUY", "SELL"):
            return None
        quantity = abs(float(getattr(open_order, "quantity", 0.0) or 0.0))
        if quantity <= 0.0 or snapshot is None:
            return None
        risk_trigger_reasons = list(getattr(snapshot, "risk_trigger_reasons", []) or [])
        if risk_trigger_reasons:
            return None
        spread_percentile = self._safe_float(getattr(snapshot, "spread_percentile_1h", None))
        depth_percentile = self._safe_float(getattr(snapshot, "depth_percentile_1h", None))
        if spread_percentile is not None and spread_percentile > float(config["stale_order_reprice_spread_percentile_max"]):
            return None
        if depth_percentile is not None and depth_percentile < float(config["stale_order_reprice_depth_percentile_min"]):
            return None
        price = self._build_replacement_price(
            side=side,
            snapshot=snapshot,
            limit_offset_bps=float(config["stale_order_reprice_limit_offset_bps"]),
        )
        if price is None:
            return None
        replace_group_id = self._build_replace_group_id(open_order)
        return OrderIntent(
            symbol=str(getattr(open_order, "symbol", "") or ""),
            action=side,
            order_type="LIMIT",
            quantity=quantity,
            price=price,
            reduce_only=False,
            take_profit=getattr(open_order, "take_profit", None),
            stop_loss=getattr(open_order, "stop_loss", None),
            replace_group_id=replace_group_id,
            lineage_role="replacement",
            lineage_parent_order_id=getattr(open_order, "order_id", None),
            lineage_parent_client_order_id=getattr(open_order, "client_order_id", None),
            wait_for_cancel_confirmation=True,
            execution_context={
                "action_type": "replace_entry",
                "policy_source": "safety_fsm",
                "requested_reprice": True,
                "replace_group_id": replace_group_id,
                "reprice_origin_order_id": getattr(open_order, "order_id", None),
                "reprice_origin_client_order_id": getattr(open_order, "client_order_id", None),
            },
        )

    def _select_stale_open_orders(self, open_orders, threshold_minutes: float):
        stale_orders = []
        for open_order in open_orders or []:
            age_minutes = self._calculate_order_age_minutes(getattr(open_order, "timestamp", None))
            if age_minutes is None or age_minutes < threshold_minutes:
                continue
            stale_orders.append(open_order)
        return stale_orders

    def _build_replacement_price(self, side: str, snapshot, limit_offset_bps: float) -> Optional[float]:
        reference_price = self._safe_float(getattr(snapshot, "current_price", None))
        order_book = getattr(snapshot, "order_book", None)
        best_bid = self._safe_float(getattr(order_book, "best_bid", None))
        best_ask = self._safe_float(getattr(order_book, "best_ask", None))
        if reference_price is None or reference_price <= 0:
            return None
        offset_ratio = float(limit_offset_bps) / 10000.0
        if side == "BUY":
            candidate = reference_price * (1.0 - offset_ratio)
            if best_bid is not None:
                candidate = min(candidate, best_bid)
        else:
            candidate = reference_price * (1.0 + offset_ratio)
            if best_ask is not None:
                candidate = max(candidate, best_ask)
        return round(candidate, 8)

    def _build_replace_group_id(self, open_order) -> str:
        basis = "|".join(
            [
                str(getattr(open_order, "symbol", "") or ""),
                str(getattr(open_order, "order_id", "") or ""),
                str(getattr(open_order, "client_order_id", "") or ""),
            ]
        )
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]

    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
