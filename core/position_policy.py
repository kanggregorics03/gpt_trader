from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Optional

from core.models import AccountState, OrderIntent, TradeIntent


class PositionPolicyEngine:
    DEFAULT_CONFIG = {
        "losing_exit_pct": -1.25,
        "stalled_minutes": 45.0,
        "stalled_max_pnl_pct": 0.2,
        "weakened_minutes": 30.0,
        "risk_trigger_exit_count": 2,
        "microstructure_exit_spread_percentile": 90.0,
        "microstructure_exit_depth_percentile": 12.0,
        "microstructure_exit_confirmations": 2,
        "invalidation_reduce_distance_atr": 0.75,
        "stale_order_minutes": 30.0,
    }

    def __init__(self, policy: Optional[Dict[str, Any]] = None, state_store=None):
        configured = ((policy or {}).get("position_policy") or {})
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
        positions = [
            position
            for position in (getattr(account_state, "positions", []) or [])
            if abs(float(getattr(position, "size", 0.0) or 0.0)) > 0
        ]
        open_orders = list(getattr(account_state, "open_orders", []) or [])

        config = dict(self.config)
        if policy_override:
            config.update(policy_override)

        if not positions:
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
                            f"Cancelling {len(stale_orders)} stale working order(s) older than {float(config['stale_order_minutes']):.1f}m."
                        ],
                        "risk_notes": ["position_policy:stale_working_orders", f"risk_mode:{risk_mode}"],
                        "confidence": 0.86,
                        "exit_reason": "order_cleanup",
                        "metadata": {"policy_source": "position_fsm", "loop_mode": loop_mode, "state": "working_orders_only"},
                    },
                }
            if open_orders:
                return {
                    "escalate_to_ai": False,
                    "decision_result": {
                        "decision": "HOLD",
                        "orders": [],
                        "rationale": ["Working orders remain within freshness limits; keep monitoring in the 15m loop."],
                        "risk_notes": ["position_policy:working_orders", f"risk_mode:{risk_mode}"],
                        "confidence": 0.8,
                        "metadata": {"policy_source": "position_fsm", "loop_mode": loop_mode, "state": "working_orders_only"},
                    },
                }
            return {"escalate_to_ai": True, "reason": "no_live_positions"}

        per_symbol = []
        decisive_actions = []
        for position in positions:
            symbol = str(getattr(position, "symbol", "") or "")
            if not symbol:
                continue
            snapshot = (market_snapshots or {}).get(symbol)
            active_case = self.state_store.get_active_trade_case(symbol) if self.state_store is not None else {}
            current_case = (
                self.state_store.get_trade_case(active_case.get("trade_case_id"))
                if self.state_store is not None and active_case.get("trade_case_id")
                else {}
            )
            state = self._evaluate_symbol_state(
                position=position,
                snapshot=snapshot,
                active_case=active_case,
                current_case=current_case,
                config=config,
            )
            per_symbol.append(state)
            if state["action"] == "EXIT":
                decisive_actions.append(state)

        if not per_symbol:
            return {"escalate_to_ai": True, "reason": "no_symbol_state"}

        decisive_actions.sort(key=lambda item: (item["severity"], item["symbol"]), reverse=True)
        if not decisive_actions:
            hold_rationale = [f"{item['symbol']} state={item['state']} reason={item['reason']}" for item in per_symbol[:4]]
            return {
                "escalate_to_ai": False,
                "decision_result": {
                    "decision": "HOLD",
                    "orders": [],
                    "rationale": hold_rationale or ["Position policy keeps current exposure unchanged."],
                    "risk_notes": [f"position_policy:{item['state']}" for item in per_symbol[:4]],
                    "confidence": 0.82,
                    "metadata": {"policy_source": "position_fsm", "per_symbol": per_symbol, "loop_mode": loop_mode},
                },
            }

        top = decisive_actions[0]
        return {
            "escalate_to_ai": False,
            "decision_result": {
                "decision": top["action"],
                "orders": [top["order"]],
                "rationale": [top["reason"]],
                "risk_notes": [f"position_policy:{top['state']}", f"risk_mode:{risk_mode}"],
                "confidence": 0.88,
                "exit_reason": top.get("exit_reason"),
                "metadata": {"policy_source": "position_fsm", "per_symbol": per_symbol, "loop_mode": loop_mode},
            },
        }

    def _evaluate_symbol_state(self, position, snapshot, active_case, current_case, config: Dict[str, Any]) -> Dict[str, Any]:
        symbol = str(getattr(position, "symbol", "") or "")
        pnl_pct = self._select_loss_metric_pct(position)
        holding_minutes = self._calculate_holding_minutes(
            active_case.get("opened_at") or current_case.get("opened_at")
        )
        distance_to_invalid_level = self._safe_float(getattr(snapshot, "distance_to_invalid_level", None))
        state = "healthy"
        action = "HOLD"
        reason = f"{symbol} remains within its management envelope."
        exit_reason = None
        severity = 0.1
        microstructure_exit_confirmed = self._is_microstructure_exit_confirmed(symbol, config)

        if pnl_pct <= float(config["losing_exit_pct"]):
            state = "exit"
            action = "EXIT"
            reason = f"{symbol} breached the management account-loss threshold at {pnl_pct:.2f}%."
            exit_reason = "thesis_break"
            severity = 1.0
        elif microstructure_exit_confirmed:
            state = "de-risk"
            action = "EXIT"
            reason = f"{symbol} confirmed liquidity deterioration across consecutive snapshots."
            exit_reason = "liquidity_exit"
            severity = 0.95
        elif holding_minutes is not None and holding_minutes >= float(config["stalled_minutes"]) and pnl_pct <= float(
            config["stalled_max_pnl_pct"]
        ):
            state = "stalled"
            action = "EXIT"
            reason = f"{symbol} stalled for {holding_minutes:.1f}m without follow-through."
            exit_reason = "time_stop"
            severity = 0.7
        elif (
            holding_minutes is not None
            and holding_minutes >= float(config["weakened_minutes"])
            and pnl_pct < 0.0
            and self._is_near_invalidation(distance_to_invalid_level, config)
        ):
            state = "weakened"
            action = "EXIT"
            reason = (
                f"{symbol} approached invalidation after {holding_minutes:.1f}m "
                f"with pnl {pnl_pct:.2f}% and distance {distance_to_invalid_level:.2f} ATR."
            )
            exit_reason = "thesis_break"
            severity = 0.65

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
            "holding_minutes": holding_minutes,
            "unrealized_pnl_pct": pnl_pct,
            "order": order,
        }

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
            urgency_score=0.95,
            execution_context={"action_type": "exit" if action == "EXIT" else "reduce", "policy_source": "position_fsm"},
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
            execution_context={"action_type": "cancel", "policy_source": "position_fsm", "replace_group_id": replace_group_id},
        )

    def _build_replace_group_id(self, open_order) -> str:
        basis = "|".join(
            [
                str(getattr(open_order, "symbol", "") or ""),
                str(getattr(open_order, "order_id", "") or ""),
                str(getattr(open_order, "client_order_id", "") or ""),
            ]
        )
        return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]

    def _select_stale_open_orders(self, open_orders, threshold_minutes: float):
        stale_orders = []
        for open_order in open_orders or []:
            age_minutes = self._calculate_order_age_minutes(getattr(open_order, "timestamp", None))
            if age_minutes is None or age_minutes < threshold_minutes:
                continue
            stale_orders.append(open_order)
        return stale_orders

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

    def _calculate_holding_minutes(self, opened_at: Optional[str]) -> Optional[float]:
        if not opened_at:
            return None
        try:
            opened_dt = datetime.strptime(str(opened_at), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None
        now = datetime.now(UTC)
        return round(max(0.0, (now - opened_dt).total_seconds()) / 60.0, 2)

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

    def _is_near_invalidation(self, distance_to_invalid_level: Optional[float], config: Dict[str, Any]) -> bool:
        if distance_to_invalid_level is None:
            return False
        return distance_to_invalid_level <= float(config["invalidation_reduce_distance_atr"])

    def _is_microstructure_exit_confirmed(self, symbol: str, config: Dict[str, Any]) -> bool:
        required = max(1, int(config.get("microstructure_exit_confirmations", 2) or 1))
        rows = self._get_recent_market_history(symbol, lookback_minutes=max(30, required * 10), limit=required)
        if len(rows) < required:
            return False
        return all(
            self._row_meets_microstructure_exit(
                row,
                spread_threshold=float(config["microstructure_exit_spread_percentile"]),
                depth_threshold=float(config["microstructure_exit_depth_percentile"]),
                min_risk_trigger_count=int(config["risk_trigger_exit_count"]),
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
