from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

from core.models import AccountState, OrderIntent


class SafetyPolicyEngine:
    DEFAULT_CONFIG = {
        "loss_exit_pct": -0.9,
        "risk_trigger_exit_count": 1,
        "microstructure_exit_spread_percentile": 85.0,
        "microstructure_exit_depth_percentile": 15.0,
        "microstructure_exit_confirmations": 2,
        "stale_order_minutes": 12.0,
        "stale_order_risk_trigger_count": 1,
    }

    def __init__(self, policy: Optional[Dict[str, Any]] = None, state_store=None):
        configured = ((policy or {}).get("safety_policy") or {})
        self.config = dict(self.DEFAULT_CONFIG)
        self.config.update(configured)
        self.state_store = state_store

    def _resolve_config(
        self,
        *,
        regime_key: Optional[str] = None,
        policy_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        config = dict(self.config)
        if policy_override:
            config.update(policy_override)
        return config

    def get_emergency_state(
        self,
        *,
        account_state: AccountState,
        market_snapshots: Dict[str, Any],
        regime_key: Optional[str] = None,
        policy_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        config = self._resolve_config(regime_key=regime_key, policy_override=policy_override)
        positions = [
            position
            for position in (getattr(account_state, "positions", []) or [])
            if abs(float(getattr(position, "size", 0.0) or 0.0)) > 0
        ]
        open_orders = list(getattr(account_state, "open_orders", []) or [])
        reasons: list[str] = []

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
            if state["action"] == "EXIT" and self._is_critical_emergency_state(state["state"]):
                reasons.append(f"{state['state']}:{symbol}")

        deduped_reasons = []
        seen = set()
        for reason in reasons:
            if reason in seen:
                continue
            seen.add(reason)
            deduped_reasons.append(reason)
        return {"active": bool(deduped_reasons), "reasons": deduped_reasons}

    def _is_critical_emergency_state(self, state: str) -> bool:
        return str(state or "").strip().lower() in {
            "liquidity_risk",
            "stale_order_risk",
        }

    def evaluate(
        self,
        account_state: AccountState,
        market_snapshots: Dict[str, Any],
        risk_mode: str,
        loop_mode: str,
        regime_key: Optional[str] = None,
        policy_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        config = self._resolve_config(regime_key=regime_key, policy_override=policy_override)

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
        pnl_pct = self._select_loss_metric_pct(position)
        state = "stable"
        action = "HOLD"
        reason = f"{symbol} remains inside the 5m safety envelope."
        exit_reason = None
        severity = 0.1
        stale_orders = self._count_stale_orders(open_orders, float(config["stale_order_minutes"]))
        liquidity_exit_confirmed = self._is_microstructure_exit_confirmed(
            symbol,
            config,
            min_risk_trigger_count=int(config["risk_trigger_exit_count"]),
        )
        stale_order_liquidity_risk = self._is_microstructure_exit_confirmed(
            symbol,
            config,
            min_risk_trigger_count=int(config["stale_order_risk_trigger_count"]),
        )

        if pnl_pct <= float(config["loss_exit_pct"]):
            state = "acute_loss"
            action = "EXIT"
            reason = f"{symbol} breached the 5m safety account-loss threshold at {pnl_pct:.2f}%."
            exit_reason = "emergency_exit"
            severity = 1.0
        elif liquidity_exit_confirmed:
            state = "liquidity_risk"
            action = "EXIT"
            reason = f"{symbol} confirmed liquidity deterioration across consecutive safety snapshots."
            exit_reason = "liquidity_exit"
            severity = 0.98
        elif stale_orders >= 1 and stale_order_liquidity_risk:
            state = "stale_order_risk"
            action = "EXIT"
            reason = f"{symbol} has stale working orders while trigger quality deteriorates."
            exit_reason = "emergency_exit"
            severity = 0.94

        order = None
        if action == "EXIT":
            order = self._build_reduce_order(position, action, 1.0)

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

    def _select_stale_open_orders(self, open_orders, threshold_minutes: float):
        stale_orders = []
        for open_order in open_orders or []:
            age_minutes = self._calculate_order_age_minutes(getattr(open_order, "timestamp", None))
            if age_minutes is None or age_minutes < threshold_minutes:
                continue
            stale_orders.append(open_order)
        return stale_orders

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

    def _select_loss_metric_pct(self, position) -> float:
        account_pnl_pct = self._safe_float(getattr(position, "account_unrealized_pnl_pct", None))
        if account_pnl_pct is not None:
            return account_pnl_pct
        return self._safe_float(getattr(position, "unrealized_pnl_pct", None)) or 0.0

    def _is_microstructure_exit_confirmed(
        self,
        symbol: str,
        config: Dict[str, Any],
        *,
        min_risk_trigger_count: int,
    ) -> bool:
        required = max(1, int(config.get("microstructure_exit_confirmations", 2) or 1))
        rows = self._get_recent_market_history(symbol, lookback_minutes=max(15, required * 10), limit=required)
        if len(rows) < required:
            return False
        return all(
            self._row_meets_microstructure_exit(
                row,
                spread_threshold=float(config["microstructure_exit_spread_percentile"]),
                depth_threshold=float(config["microstructure_exit_depth_percentile"]),
                min_risk_trigger_count=min_risk_trigger_count,
            )
            for row in rows[-required:]
        )

    def _get_recent_market_history(self, symbol: str, lookback_minutes: int, limit: int) -> list[dict]:
        fetcher = getattr(self.state_store, "get_market_snapshot_history_between", None)
        if not callable(fetcher) or not symbol:
            return []
        end_dt = datetime.now(UTC).replace(tzinfo=None)
        start_dt = end_dt - timedelta(minutes=max(1, int(lookback_minutes or 1)))
        rows = fetcher(symbol, start_timestamp=start_dt, end_timestamp=end_dt, limit=max(limit * 3, limit)) or []
        return [dict(row) for row in rows][-limit:]

    def _row_meets_microstructure_exit(
        self,
        row: dict,
        *,
        spread_threshold: float,
        depth_threshold: float,
        min_risk_trigger_count: int,
    ) -> bool:
        spread_percentile = self._safe_float(row.get("spread_percentile_1h"))
        depth_percentile = self._safe_float(row.get("depth_percentile_1h"))
        payload = dict(row.get("payload") or {})
        risk_trigger_reasons = list(payload.get("risk_trigger_reasons") or [])
        return (
            spread_percentile is not None
            and spread_percentile >= spread_threshold
            and depth_percentile is not None
            and depth_percentile <= depth_threshold
            and len(risk_trigger_reasons) >= int(min_risk_trigger_count)
        )
