class TradingRuntimeDiagnosticsMixin:
    def _maybe_alert_risk_state_transitions(self, risk_state):
        risk_state = self._coerce_risk_state(risk_state)
        daily_kill_switch_active = self._is_daily_kill_switch_active(risk_state)
        if daily_kill_switch_active and self._daily_kill_switch_active is not True:
            daily_max_loss_pct = abs(
                float(self.risk_engine.policy.get("risk_limits", {}).get("daily_max_loss_pct", 0.0) or 0.0)
            )
            daily_pnl_pct = float(risk_state.daily_pnl_pct or 0.0)
            self.alert_manager.send(
                "Daily Kill Switch Activated",
                (
                    f"Daily pnl {daily_pnl_pct:.2f}% breached limit {-daily_max_loss_pct:.2f}%. "
                    "New entries are blocked until the daily reset."
                ),
                level="error",
            )
        self._daily_kill_switch_active = daily_kill_switch_active

        drawdown_active = bool(risk_state.drawdown_active)
        drawdown_activate_pct = risk_state.drawdown_activate_pct
        daily_pnl_pct = float(risk_state.daily_pnl_pct or 0.0)
        if drawdown_active and self._drawdown_mode_active is not True:
            threshold_text = ""
            if drawdown_activate_pct is not None:
                threshold_text = f" (threshold {-abs(float(drawdown_activate_pct)):.2f}%)"
            self.alert_manager.send(
                "Drawdown Mode Entered",
                f"Daily pnl {daily_pnl_pct:.2f}% entered drawdown mode{threshold_text}.",
                level="warning",
            )
        elif not drawdown_active and self._drawdown_mode_active is True:
            self.alert_manager.send(
                "Drawdown Mode Cleared",
                f"Daily pnl recovered to {daily_pnl_pct:.2f}%. Drawdown controls are cleared.",
                level="info",
            )
        self._drawdown_mode_active = drawdown_active

        cooldown_signature = self._build_cooldown_extension_signature(risk_state)
        if cooldown_signature is not None and self._cooldown_extension_signature != cooldown_signature:
            base_minutes, effective_minutes, components, losses, pattern_reason, last_loss_pct = cooldown_signature
            detail_parts = [f"{base_minutes}m -> {effective_minutes}m"]
            remaining_minutes = int(risk_state.cooldown_remaining_minutes or 0)
            if remaining_minutes > 0:
                detail_parts.append(f"{remaining_minutes}m remaining")
            if losses > 0:
                detail_parts.append(f"{losses} consecutive loss(es)")
            if components:
                detail_parts.append(f"reasons={', '.join(components)}")
            if pattern_reason:
                detail_parts.append(f"pattern={pattern_reason}")
            if last_loss_pct != 0.0:
                detail_parts.append(f"last_loss={last_loss_pct:.2f}%")
            self.alert_manager.send(
                "Cooldown Extended",
                "Dynamic cooldown extended: " + "; ".join(detail_parts) + ".",
                level="warning",
            )
        self._cooldown_extension_signature = cooldown_signature

    def _get_public_ws_state(self):
        feed = getattr(self, "public_feed", None)
        if feed is None or not hasattr(feed, "get_diagnostics"):
            return None
        diagnostics = feed.get_diagnostics()
        if not bool(diagnostics.get("started")) or not bool(diagnostics.get("available", True)):
            return None
        stale_after_ms = int(diagnostics.get("ws_stale_after_ms", 0) or 0)
        last_message_age_ms = diagnostics.get("last_message_age_ms")
        stale_symbols = [str(symbol) for symbol in (diagnostics.get("stale_symbols") or []) if symbol]
        is_stale = bool(stale_symbols)
        if not is_stale and last_message_age_ms is not None and stale_after_ms > 0:
            is_stale = int(last_message_age_ms) > stale_after_ms
        return {
            "is_stale": is_stale,
            "last_message_age_ms": last_message_age_ms,
            "stale_after_ms": stale_after_ms,
            "stale_symbols": stale_symbols,
        }

    def _get_private_ws_state(self):
        feed = getattr(self, "private_account_feed", None)
        if feed is None or not hasattr(feed, "get_diagnostics"):
            return None
        diagnostics = feed.get_diagnostics()
        if not bool(diagnostics.get("started")) or not bool(diagnostics.get("available", True)):
            return None
        return {
            "health_status": str(diagnostics.get("health_status") or "").lower(),
            "last_ws_message_age_ms": diagnostics.get("last_ws_message_age_ms"),
            "last_private_event_age_ms": diagnostics.get("last_private_event_age_ms"),
            "stale_after_ms": int(diagnostics.get("ws_stale_after_ms", 0) or 0),
        }

    def _maybe_alert_websocket_transitions(self):
        public_state = self._get_public_ws_state()
        if public_state is not None:
            if public_state["is_stale"] and self._public_ws_stale is not True:
                age_seconds = round(float(public_state.get("last_message_age_ms") or 0.0) / 1000.0, 1)
                threshold_seconds = round(float(public_state.get("stale_after_ms") or 0.0) / 1000.0, 1)
                stale_symbols = public_state.get("stale_symbols") or []
                symbol_suffix = (
                    f" Stale symbols: {', '.join(stale_symbols)}."
                    if stale_symbols
                    else ""
                )
                self.alert_manager.send(
                    "WebSocket Stale",
                    (
                        f"Public market WebSocket exceeded freshness threshold "
                        f"({age_seconds}s > {threshold_seconds}s).{symbol_suffix}"
                    ),
                    level="warning",
                )
            elif not public_state["is_stale"] and self._public_ws_stale is True:
                age_seconds = round(float(public_state.get("last_message_age_ms") or 0.0) / 1000.0, 1)
                self.alert_manager.send(
                    "WebSocket Recovered",
                    f"Public market WebSocket recovered. Latest message age is {age_seconds}s.",
                    level="info",
                )
            self._public_ws_stale = bool(public_state["is_stale"])

        private_state = self._get_private_ws_state()
        if private_state is not None:
            is_stale = private_state["health_status"] == "stale"
            if is_stale and self._private_ws_stale is not True:
                ws_age_seconds = round(float(private_state.get("last_ws_message_age_ms") or 0.0) / 1000.0, 1)
                event_age_seconds = round(float(private_state.get("last_private_event_age_ms") or 0.0) / 1000.0, 1)
                threshold_seconds = round(float(private_state.get("stale_after_ms") or 0.0) / 1000.0, 1)
                self.alert_manager.send(
                    "WebSocket Stale",
                    (
                        "Private account WebSocket entered stale state "
                        f"(ws_age={ws_age_seconds}s, event_age={event_age_seconds}s, threshold={threshold_seconds}s)."
                    ),
                    level="warning",
                )
            elif (
                not is_stale
                and self._private_ws_stale is True
                and private_state["health_status"] == "healthy"
            ):
                ws_age_seconds = round(float(private_state.get("last_ws_message_age_ms") or 0.0) / 1000.0, 1)
                self.alert_manager.send(
                    "WebSocket Recovered",
                    f"Private account WebSocket recovered to healthy state. Latest message age is {ws_age_seconds}s.",
                    level="info",
                )
            self._private_ws_stale = is_stale
