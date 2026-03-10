import hashlib

from core.models import RiskStateSnapshot, coerce_risk_state_snapshot


class TradingRuntimeStateMixin:
    def _build_empty_context_selection_summary(self) -> dict:
        return {
            "recent_journal_context": {
                "available_total": 0,
                "selected_total": 0,
                "available_bucket_counts": {},
                "selected_bucket_counts": {},
                "excluded_bucket_counts": {},
                "selected_journal_ids": [],
            },
            "long_term_memory": {
                "available_total": 0,
                "selected_total": 0,
                "available_bucket_counts": {},
                "selected_bucket_counts": {},
                "excluded_bucket_counts": {},
                "selected_rules": [],
            },
            "similar_past_cases": {
                "trigger_mode": False,
                "available_total": 0,
                "selected_total": 0,
                "selected_sources": {},
            },
        }

    def _get_learning_artifact_payload(self, artifact_key: str) -> dict:
        cache = getattr(self, "_learning_artifact_cache", None)
        if cache is None:
            self._learning_artifact_cache = {}
            cache = self._learning_artifact_cache
        key = str(artifact_key or "").strip()
        if not key:
            return {}
        if key in cache:
            return dict(cache[key])
        if not hasattr(self.state_store, "get_learning_artifact"):
            cache[key] = {}
            return {}
        artifact = self.state_store.get_learning_artifact(key) or {}
        payload = artifact.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        cache[key] = dict(payload)
        return dict(payload)

    def _coerce_risk_state(self, risk_state) -> RiskStateSnapshot:
        return coerce_risk_state_snapshot(risk_state)

    def _serialize_risk_state(self, risk_state) -> dict:
        return self._coerce_risk_state(risk_state).model_dump(mode="json")

    def _serialize_json_payload(self, payload):
        if hasattr(payload, "model_dump"):
            return payload.model_dump(mode="json")
        if isinstance(payload, dict):
            return {
                str(key): self._serialize_json_payload(value)
                for key, value in payload.items()
            }
        if isinstance(payload, (list, tuple)):
            return [self._serialize_json_payload(item) for item in payload]
        return payload

    def _primary_symbol(self) -> str:
        configured = [str(symbol).strip().upper() for symbol in (self.symbols or []) if str(symbol).strip()]
        if configured:
            return configured[0]
        return str(getattr(self, "symbol", "BTCUSDT") or "BTCUSDT").strip().upper()

    def _get_active_trade_case_for_symbol(self, symbol: str) -> dict:
        fetcher = getattr(self.state_store, "get_active_trade_case", None)
        if not callable(fetcher) or not symbol:
            return {}
        item = fetcher(symbol) or {}
        return dict(item) if isinstance(item, dict) else {}

    def _normalize_market_snapshots(self, market_snapshots):
        market_snapshots = dict(market_snapshots or {})
        if not market_snapshots:
            return {}
        primary_symbol = self._primary_symbol()
        if primary_symbol in market_snapshots:
            return {primary_symbol: market_snapshots[primary_symbol]}
        first_symbol = next(iter(market_snapshots.keys()))
        return {str(first_symbol): market_snapshots[first_symbol]}

    def _normalize_market_prices(self, market_prices, market_snapshots):
        market_prices = dict(market_prices or {})
        primary_symbol = self._primary_symbol()
        if primary_symbol in market_prices:
            return {primary_symbol: market_prices[primary_symbol]}
        if primary_symbol in (market_snapshots or {}):
            return {primary_symbol: market_snapshots[primary_symbol].current_price}
        if market_prices:
            first_symbol = next(iter(market_prices.keys()))
            return {str(first_symbol): market_prices[first_symbol]}
        if market_snapshots:
            first_symbol = next(iter(market_snapshots.keys()))
            return {str(first_symbol): market_snapshots[first_symbol].current_price}
        return {}

    def _get_symbol_reentry_guard(self, symbol: str, execution_guards: dict):
        fetcher = getattr(self.state_store, "get_recent_closed_trade_case_for_symbol", None)
        if not callable(fetcher) or not symbol:
            return {}
        close_minutes = int(execution_guards.get("symbol_reentry_close_cooldown_minutes", 0) or 0)
        loss_minutes = int(execution_guards.get("symbol_reentry_loss_cooldown_minutes", 0) or 0)
        loss_threshold_pct = float(execution_guards.get("symbol_reentry_loss_threshold_pct", 0.0) or 0.0)
        if close_minutes <= 0 and loss_minutes <= 0:
            return {}
        return fetcher(
            symbol=symbol,
            close_cooldown_minutes=close_minutes,
            loss_cooldown_minutes=loss_minutes,
            loss_threshold_pct=loss_threshold_pct,
            require_no_active_case=True,
        ) or {}

    def _enrich_risk_state_for_cycle(self, risk_state, account_state, market_snapshots, execution_guards: dict):
        snapshot = self._coerce_risk_state(risk_state)
        symbol = next(iter((market_snapshots or {}).keys()), self._primary_symbol())
        has_live_position = any(
            str(getattr(position, "symbol", "") or "").strip().upper() == symbol
            and abs(float(getattr(position, "size", 0.0) or 0.0)) > 0
            for position in (getattr(account_state, "positions", []) or [])
        )
        has_live_open_order = any(
            str(getattr(order, "symbol", "") or "").strip().upper() == symbol
            for order in (getattr(account_state, "open_orders", []) or [])
        )
        active_case_fetcher = getattr(self.state_store, "get_active_trade_case", None)
        has_active_trade_case = bool(active_case_fetcher(symbol)) if callable(active_case_fetcher) else False
        if has_live_position or has_live_open_order or has_active_trade_case:
            return snapshot.model_copy(
                update={
                    "reentry_guard_active": False,
                    "reentry_guard_symbol": symbol,
                    "reentry_guard_reason": None,
                    "reentry_guard_remaining_minutes": 0,
                    "reentry_guard_last_closed_at": None,
                    "reentry_guard_last_estimated_pnl_pct": None,
                    "reentry_guard_last_exit_reason": None,
                }
            )
        guard = self._get_symbol_reentry_guard(symbol, execution_guards)
        if not guard or not bool(guard.get("active")):
            return snapshot
        return snapshot.model_copy(
            update={
                "reentry_guard_active": True,
                "reentry_guard_symbol": symbol,
                "reentry_guard_reason": str(guard.get("reason") or "").strip() or "recent_symbol_close",
                "reentry_guard_remaining_minutes": int(guard.get("remaining_minutes", 0) or 0),
                "reentry_guard_last_closed_at": guard.get("closed_at"),
                "reentry_guard_last_estimated_pnl_pct": guard.get("estimated_pnl_pct"),
                "reentry_guard_last_exit_reason": guard.get("exit_reason"),
            }
        )

    def _is_daily_kill_switch_active(self, risk_state) -> bool:
        risk_limits = self.risk_engine.policy.get("risk_limits", {})
        daily_max_loss_pct = float(risk_limits.get("daily_max_loss_pct", 0.0) or 0.0)
        if daily_max_loss_pct <= 0:
            return False
        daily_pnl_pct = float(self._coerce_risk_state(risk_state).daily_pnl_pct or 0.0)
        return daily_pnl_pct <= -abs(daily_max_loss_pct)

    def _is_daily_kill_switch_reason(self, reason: str) -> bool:
        return "Kill switch active: daily pnl" in str(reason or "")

    def _build_cooldown_extension_signature(self, risk_state):
        risk_state = self._coerce_risk_state(risk_state)
        if not bool(risk_state.cooldown_active):
            return None
        risk_limits = self.risk_engine.policy.get("risk_limits", {})
        dynamic_config = risk_limits.get("dynamic_cooldown", {}) or {}
        base_minutes = int(dynamic_config.get("base_minutes", risk_limits.get("cooldown_minutes", 0)) or 0)
        effective_minutes = int(risk_state.cooldown_minutes_effective or 0)
        if effective_minutes <= max(0, base_minutes):
            return None
        return (
            base_minutes,
            effective_minutes,
            tuple(str(component) for component in (risk_state.cooldown_components or [])),
            int(risk_state.consecutive_losses or 0),
            str(risk_state.dominant_failure_reason or "").strip(),
            round(float(risk_state.last_loss_estimated_pnl_pct or 0.0), 4),
        )

    def _build_effective_risk_profile(
        self,
        loop_mode: str,
        account_state,
        risk_profile,
        risk_mode: str | None = None,
        regime_key: str = "",
    ):
        effective_profile = dict(risk_profile or {})
        primary_symbol = self._primary_symbol()
        active_trade_case = self._get_active_trade_case_for_symbol(primary_symbol)
        has_live_positions = self._has_live_positions(account_state)
        has_live_open_orders = self._has_live_open_orders(account_state)
        trade_consistency_lock_active = bool(has_live_positions)
        allowed_actions = [
            str(action).upper()
            for action in effective_profile.get("allowed_actions", [])
        ]
        for management_action in ("CANCEL", "REPLACE_ENTRY"):
            if management_action not in allowed_actions:
                allowed_actions.append(management_action)
        if risk_mode == "normal" and regime_key:
            exposure_regime_key = f"{risk_mode}|{regime_key}"
            registry_fetch = getattr(self.state_store, "get_policy_registry_entry_for_regime", None)
            exposure_registry = {}
            if callable(registry_fetch):
                exposure_registry = registry_fetch("exposure_budget", regime_key=exposure_regime_key)
            if exposure_registry:
                config = exposure_registry.get("config") or {}
                for key in ("max_order_notional_pct_balance", "entry_size_multiplier"):
                    if key in config:
                        effective_profile[key] = config[key]
                effective_profile["exposure_policy_key"] = exposure_registry.get("policy_key")
                effective_profile["exposure_regime_key"] = exposure_regime_key
        if loop_mode == "15m":
            effective_profile["cycle_role"] = "position_management"
            effective_profile["cycle_guidance"] = (
                "Prioritize managing live exposure and working orders. Avoid opening new positions in the 15m cycle."
            )
            effective_profile["allowed_actions"] = [
                action for action in allowed_actions
                if action in ("HOLD", "EXIT", "CANCEL", "REPLACE_ENTRY")
            ]
        elif loop_mode == "5m":
            effective_profile["cycle_role"] = "execution_safety"
            effective_profile["cycle_guidance"] = (
                "Run the 5m safety loop for execution quality, trigger deterioration, and emergency de-risking. "
                "Do not open new positions in the 5m cycle."
            )
            effective_profile["allowed_actions"] = [
                action for action in allowed_actions
                if action in ("HOLD", "EXIT", "CANCEL", "REPLACE_ENTRY")
            ]
        elif loop_mode == "1h" and trade_consistency_lock_active:
            effective_profile["cycle_role"] = "trade_consistency_management"
            effective_profile["cycle_guidance"] = (
                "A live position is already open. Preserve trade consistency: manage the existing thesis "
                "or exit conditions, but do not create a new entry thesis in the 1h cycle."
            )
            effective_profile["allowed_actions"] = [
                action for action in allowed_actions
                if action in ("HOLD", "EXIT", "CANCEL", "REPLACE_ENTRY")
            ]
        else:
            effective_profile["cycle_role"] = "strategy"
            effective_profile["cycle_guidance"] = (
                "Set portfolio direction and initiate new exposure only when the higher-timeframe case is justified."
            )
            effective_profile["allowed_actions"] = allowed_actions
        effective_profile["has_live_positions"] = has_live_positions
        effective_profile["has_live_open_orders"] = has_live_open_orders
        effective_profile["trade_consistency_lock_active"] = trade_consistency_lock_active
        effective_profile["active_trade_case_id"] = active_trade_case.get("trade_case_id")
        effective_profile["active_thesis_id"] = active_trade_case.get("thesis_id")
        effective_profile["active_management_template_id"] = active_trade_case.get("management_template_id")
        return effective_profile

    def _extract_entry_orders(self, orders):
        return [
            order
            for order in (orders or [])
            if not getattr(order, "reduce_only", False) and getattr(order, "action", "") in ("BUY", "SELL")
        ]

    def _build_order_size_band(self, order, market_prices):
        reference_price = order.price or market_prices.get(order.symbol) or 0.0
        notional = abs(float(order.quantity or 0.0) * float(reference_price))
        if notional <= 50:
            return "micro"
        if notional <= 250:
            return "tiny"
        if notional <= 1000:
            return "small"
        if notional <= 5000:
            return "medium"
        if notional <= 20000:
            return "large"
        return "xlarge"

    def _build_order_fingerprint(self, order, market_prices):
        size_band = self._build_order_size_band(order, market_prices)
        key = f"{order.symbol}|{order.action}|{size_band}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return {
            "symbol": order.symbol,
            "action": order.action,
            "size_band": size_band,
            "fingerprint": digest,
        }

    def _acquire_entry_mutexes(self, entry_orders, owner: str, ttl_seconds: int):
        acquired = []
        for symbol in sorted({order.symbol for order in entry_orders if order.symbol}):
            if not self.state_store.acquire_entry_mutex(symbol, owner=owner, ttl_seconds=ttl_seconds):
                for locked_symbol in acquired:
                    self.state_store.release_entry_mutex(locked_symbol, owner=owner)
                return False, acquired
            acquired.append(symbol)
        return True, acquired

    def _release_entry_mutexes(self, symbols, owner: str):
        for symbol in symbols or []:
            self.state_store.release_entry_mutex(symbol, owner=owner)

    def _resolve_runtime_state_from_recovered_order(self, recovered_order: dict) -> str:
        status = str(
            (recovered_order or {}).get("status")
            or ((recovered_order or {}).get("info") or {}).get("orderStatus")
            or "unknown"
        ).lower()
        if status in ("closed", "filled"):
            return "completed"
        if status in ("canceled", "cancelled"):
            return "cancelled"
        if status in ("failed", "rejected"):
            return "failed"
        if status in ("open", "partially_filled", "partiallyfilled"):
            return "partial_filled"
        return "unknown"
