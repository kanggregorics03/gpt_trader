import time
from datetime import datetime

from utils.logger import log


class TradingCadencePolicyMixin:
    def _loop_minutes_for_mode(self, mode: str) -> int:
        mapping = {
            "1h": self.adaptive_loop_config["base_loop_minutes"],
            "15m": self.adaptive_loop_config["position_loop_minutes"],
            "5m": self.adaptive_loop_config["trigger_loop_minutes"],
        }
        return int(mapping.get(mode, self.adaptive_loop_config["base_loop_minutes"]))

    def _loop_label_for_mode(self, mode: str) -> str:
        return self.LOOP_MODE_LABELS.get(mode, f"Adaptive ({mode})")

    def _next_aligned_epoch(self, minutes: int, now_epoch: float | None = None) -> float:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        interval_seconds = max(1, int(minutes)) * 60
        return (int(now_epoch // interval_seconds) + 1) * interval_seconds

    def _has_live_positions(self, account_state) -> bool:
        return any(abs(float(getattr(position, "size", 0.0) or 0.0)) > 0 for position in account_state.positions)

    def _has_live_open_orders(self, account_state) -> bool:
        return bool(account_state.open_orders or getattr(account_state, "open_orders_count", 0))

    def _has_recent_activity(self, timestamp_value, window_minutes: int) -> bool:
        if timestamp_value is None:
            return False
        return (time.time() - float(timestamp_value)) < (max(1, int(window_minutes)) * 60)

    def _position_closed_during_cycle(self, before_state, after_state) -> bool:
        if before_state is None or after_state is None:
            return False
        before_symbols = {
            position.symbol
            for position in before_state.positions
            if abs(float(getattr(position, "size", 0.0) or 0.0)) > 0
        }
        after_symbols = {
            position.symbol
            for position in after_state.positions
            if abs(float(getattr(position, "size", 0.0) or 0.0)) > 0
        }
        return bool(before_symbols - after_symbols)

    def _determine_desired_loop_mode(self, cycle_result: dict) -> str:
        if bool(cycle_result.get("lock_skipped")):
            return self.active_loop_mode
        final_account_state = cycle_result.get("final_account_state")
        has_positions = bool(cycle_result.get("has_positions"))
        has_open_orders = bool(cycle_result.get("has_open_orders"))
        if final_account_state is not None:
            has_positions = self._has_live_positions(final_account_state)
            has_open_orders = self._has_live_open_orders(final_account_state)
        has_unknown_orders = bool(cycle_result.get("has_unknown_orders"))
        recent_execution = self._has_recent_activity(
            self.last_execution_at,
            self.adaptive_loop_config["recent_execution_window_minutes"],
        )
        recent_close = self._has_recent_activity(
            self.last_close_at,
            self.adaptive_loop_config["recent_close_window_minutes"],
        )
        active_management_target = has_positions or has_open_orders or has_unknown_orders
        safety_trigger_active = cycle_result.get("safety_trigger_active")
        if safety_trigger_active is None:
            safety_trigger_active = cycle_result.get("guarded_attention_active")
        if (bool(safety_trigger_active) or has_unknown_orders) and active_management_target:
            return "5m"
        if active_management_target or recent_execution or recent_close:
            return "15m"
        return "1h"

    def _has_active_management_target(self, cycle_result: dict) -> bool:
        final_account_state = cycle_result.get("final_account_state")
        has_positions = bool(cycle_result.get("has_positions"))
        has_open_orders = bool(cycle_result.get("has_open_orders"))
        if final_account_state is not None:
            has_positions = self._has_live_positions(final_account_state)
            has_open_orders = self._has_live_open_orders(final_account_state)
        has_unknown_orders = bool(cycle_result.get("has_unknown_orders"))
        return has_positions or has_open_orders or has_unknown_orders

    def _promote_loop_mode(self, mode: str, now_epoch: float):
        previous = self.active_loop_mode
        self.active_loop_mode = mode
        self.active_loop_mode_since = now_epoch
        self.trigger_clear_streak = 0
        self.flat_clear_streak = 0
        if previous != mode:
            log.info(f"Adaptive cadence promoted: {previous} -> {mode}")

    def _apply_adaptive_loop_mode(self, desired_mode: str, now_epoch: float, cycle_result: dict | None = None) -> str:
        current_mode = self.active_loop_mode
        if self.LOOP_MODE_PRIORITY.get(desired_mode, 0) > self.LOOP_MODE_PRIORITY.get(current_mode, 0):
            self._promote_loop_mode(desired_mode, now_epoch)
            return self.active_loop_mode

        if desired_mode == current_mode:
            if current_mode == "5m":
                self.trigger_clear_streak = 0
            if current_mode == "15m":
                self.flat_clear_streak = 0
            return current_mode

        has_active_management_target = self._has_active_management_target(cycle_result or {})
        if current_mode == "5m":
            if not has_active_management_target:
                previous = self.active_loop_mode
                self.active_loop_mode = desired_mode
                self.active_loop_mode_since = now_epoch
                self.trigger_clear_streak = 0
                self.flat_clear_streak = 0
                log.info(f"Adaptive cadence downgraded immediately: {previous} -> {desired_mode}")
                return self.active_loop_mode
            self.trigger_clear_streak += 1
            if self.trigger_clear_streak < self.adaptive_loop_config["trigger_clear_cycles"]:
                return current_mode
            previous = self.active_loop_mode
            self.active_loop_mode = desired_mode
            self.active_loop_mode_since = now_epoch
            self.trigger_clear_streak = 0
            self.flat_clear_streak = 0
            log.info(f"Adaptive cadence downgraded: {previous} -> {desired_mode}")
            return self.active_loop_mode

        if current_mode == "15m" and desired_mode == "1h":
            if not has_active_management_target:
                previous = self.active_loop_mode
                self.active_loop_mode = "1h"
                self.active_loop_mode_since = now_epoch
                self.flat_clear_streak = 0
                log.info(f"Adaptive cadence downgraded immediately: {previous} -> 1h")
                return self.active_loop_mode
            self.flat_clear_streak += 1
            held_long_enough = (now_epoch - self.active_loop_mode_since) >= (
                self.adaptive_loop_config["position_min_duration_minutes"] * 60
            )
            if (
                not held_long_enough
                or self.flat_clear_streak < self.adaptive_loop_config["flat_clear_cycles"]
            ):
                return current_mode
            previous = self.active_loop_mode
            self.active_loop_mode = "1h"
            self.active_loop_mode_since = now_epoch
            self.flat_clear_streak = 0
            log.info(f"Adaptive cadence downgraded: {previous} -> 1h")
            return self.active_loop_mode

        return current_mode

    def _refresh_runtime_activity(self, cycle_result: dict, now_epoch: float):
        if int(cycle_result.get("executed_order_count", 0) or 0) > 0:
            self.last_execution_at = now_epoch
        if bool(cycle_result.get("position_closed")):
            self.last_close_at = now_epoch

    def _run_due_maintenance_tasks(self, now_epoch: float):
        recovery_interval_seconds = self._build_execution_guards()["recovery_sweep_minutes"] * 60
        if (now_epoch - self.last_recovery_sweep_at) >= recovery_interval_seconds:
            self.run_recovery_sweep()
            self.last_recovery_sweep_at = time.time()

        daily_batch_config = self._build_daily_batch_config()
        now_local = datetime.now().astimezone()
        if (
            daily_batch_config.get("enabled", True)
            and now_local.hour == daily_batch_config["run_hour_local"]
            and now_local.minute == daily_batch_config["run_minute_local"]
            and self.last_daily_batch_date != now_local.date().isoformat()
        ):
            self.run_daily_batch()
            self.last_daily_batch_date = now_local.date().isoformat()
