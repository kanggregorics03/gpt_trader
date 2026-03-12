
import hashlib
import os
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from ai.decision_engine import DecisionEngine, DecisionResult
from ai.llm_provider import normalize_provider_name, resolve_api_key, resolve_decision_models
from ai.rag_engine import RagEngine
from core.alpha_policy import AlphaPolicyEngine
from core.execution_policy import ExecutionPolicyEngine
from core.models import (
    DecisionRequest,
    EvidenceItem,
    RationaleItem,
    RiskStateSnapshot,
    TradeIntent,
    coerce_risk_state_snapshot,
)
from core.position_policy import PositionPolicyEngine
from core.risk_engine import RiskEngine
from core.safety_policy import SafetyPolicyEngine
from core.snapshot_service import SnapshotService
from db.state_store import StateStore
from exchange.account_reader import AccountReader
from exchange.bybit_factory import (
    create_private_account_feed,
    create_private_exchange,
    create_public_exchange,
    create_public_market_feed,
    normalize_trading_mode,
)
from exchange.collector import MarketCollector
from exchange.execution_engine import ExecutionEngine
from utils.logger import log

class TradingMaintenanceMixin:
    def _resolve_memory_batch_timeout_seconds(self, configured_timeout_seconds: int) -> int:
        timeout_seconds = max(60, int(configured_timeout_seconds or 0))
        memory_provider = normalize_provider_name(os.getenv("MEMORY_PROVIDER"))
        if memory_provider != "deepseek":
            return timeout_seconds
        deepseek_floor = max(
            60,
            int(
                (self.risk_engine.policy.get("daily_batch", {}) or {}).get(
                    "deepseek_memory_timeout_seconds",
                    self.DEFAULT_DAILY_BATCH.get("deepseek_memory_timeout_seconds", timeout_seconds),
                )
                or timeout_seconds
            ),
        )
        return max(timeout_seconds, deepseek_floor)

    def _build_daily_batch_config(self):
        configured = self.risk_engine.policy.get("daily_batch", {})
        config = dict(self.DEFAULT_DAILY_BATCH)
        config.update(configured or {})
        config["enabled"] = bool(config.get("enabled", self.DEFAULT_DAILY_BATCH["enabled"]))
        config["run_hour_local"] = min(
            23,
            max(0, int(config.get("run_hour_local", self.DEFAULT_DAILY_BATCH["run_hour_local"]) or 0)),
        )
        config["run_minute_local"] = min(
            59,
            max(0, int(config.get("run_minute_local", self.DEFAULT_DAILY_BATCH["run_minute_local"]) or 0)),
        )
        config["replay_timeout_seconds"] = max(
            60,
            int(
                config.get(
                    "replay_timeout_seconds",
                    self.DEFAULT_DAILY_BATCH["replay_timeout_seconds"],
                )
                or 0
            ),
        )
        config["memory_timeout_seconds"] = max(
            60,
            int(
                config.get(
                    "memory_timeout_seconds",
                    self.DEFAULT_DAILY_BATCH["memory_timeout_seconds"],
                )
                or 0
            ),
        )
        config["deepseek_memory_timeout_seconds"] = max(
            60,
            int(
                config.get(
                    "deepseek_memory_timeout_seconds",
                    self.DEFAULT_DAILY_BATCH["deepseek_memory_timeout_seconds"],
                )
                or 0
            ),
        )
        config["memory_timeout_seconds"] = self._resolve_memory_batch_timeout_seconds(
            config["memory_timeout_seconds"]
        )
        return config

    def _run_batch_subprocess(self, args: list[str], *, timeout_seconds: int, label: str) -> bool:
        try:
            subprocess.run(args, check=True, timeout=timeout_seconds)
            log.info("{} finished successfully.", label)
            return True
        except subprocess.TimeoutExpired:
            log.error("{} timed out after {} seconds.", label, timeout_seconds)
            return False
        except Exception as e:
            log.error(f"{label} failed: {e}")
            return False

    def _run_daily_batch_worker(self, replay_config: dict, daily_batch_config: dict):
        final_status = "success"
        try:
            if replay_config["enabled"]:
                log.info("Running incremental replay batch before daily memory updater...")
                replay_args = [
                    sys.executable,
                    "-m",
                    "tools.replay_audit",
                    "--auto",
                    "--limit",
                    str(replay_config["limit"]),
                    "--max-limit",
                    str(replay_config["max_limit"]),
                    "--coverage-target",
                    str(replay_config["coverage_target"]),
                    "--hold-quota",
                    str(replay_config["hold_quota"]),
                    "--candidate-pool-limit",
                    str(replay_config["candidate_pool_limit"]),
                    "--window-hours",
                    str(replay_config["window_hours"]),
                ]
                if replay_config["save"]:
                    replay_args.append("--save")
                replay_ok = self._run_batch_subprocess(
                    replay_args,
                    timeout_seconds=daily_batch_config["replay_timeout_seconds"],
                    label="Incremental replay batch",
                )
                if not replay_ok:
                    final_status = "replay_failed"
            log.info("Running daily memory updater batch...")
            memory_ok = self._run_batch_subprocess(
                [sys.executable, "-m", "batch.memory_updater"],
                timeout_seconds=daily_batch_config["memory_timeout_seconds"],
                label="Daily memory updater batch",
            )
            if not memory_ok:
                final_status = "memory_failed" if final_status == "success" else f"{final_status}+memory_failed"
        except Exception as e:
            final_status = "unexpected_error"
            log.error(f"Daily batch worker failed unexpectedly: {e}")
        finally:
            finished_at = time.time()
            with self._daily_batch_lock:
                self.daily_batch_running = False
                self.daily_batch_finished_at = finished_at
                self.daily_batch_last_status = final_status
                self._daily_batch_thread = None
            log.info("Daily batch worker finished with status={}", final_status)

    def run_recovery_sweep(self):
        log.info("Running unresolved order recovery sweep...")
        try:
            unknown_rows = self.state_store.get_unknown_order_runtime_states(limit=100)
            if not unknown_rows:
                log.info("Recovery sweep: no unknown runtime orders.")
                return
            recovered_count = 0
            for row in unknown_rows:
                symbol = row.get("symbol")
                client_order_id = row.get("client_order_id")
                if not symbol or not client_order_id:
                    continue
                recovered_order = self.execution_engine._recover_timed_out_order(symbol, client_order_id)
                if not recovered_order:
                    continue
                to_state = self._resolve_runtime_state_from_recovered_order(recovered_order)
                self.state_store.save_order_state_transitions(
                    audit_log_id=int(row.get("last_audit_log_id") or 0),
                    entries=[
                        {
                            "symbol": symbol,
                            "client_order_id": client_order_id,
                            "exchange_order_id": recovered_order.get("id") or (recovered_order.get("info") or {}).get("orderId"),
                            "from_state": "unknown",
                            "to_state": to_state,
                            "event_type": "recovery_sweep",
                            "payload": {"recovered_order": recovered_order},
                        }
                    ],
                )
                recovered_count += 1
            if recovered_count > 0:
                self.alert_manager.send(
                    "Recovery Sweep",
                    f"Recovered {recovered_count} unknown order lifecycle state(s).",
                    level="info",
                )
            log.info(
                "Recovery sweep finished: unknown_rows={}, recovered={}",
                len(unknown_rows),
                recovered_count,
            )
        except Exception as e:
            log.error(f"Recovery sweep failed: {e}")
            self.alert_manager.send(
                "Recovery Sweep Error",
                f"Recovery sweep failed: {e}",
                level="warning",
            )

    def run_daily_batch(self):
        daily_batch_config = self._build_daily_batch_config()
        if not daily_batch_config["enabled"]:
            log.info("Daily batch disabled; skipping launch.")
            with self._daily_batch_lock:
                self.daily_batch_last_status = "disabled"
            return False

        replay_config = self._build_replay_batch_config()
        with self._daily_batch_lock:
            thread = self._daily_batch_thread
            if self.daily_batch_running or (thread is not None and thread.is_alive()):
                log.warning("Daily batch already running; skipping new launch.")
                self.daily_batch_last_status = "already_running"
                return False
            self.daily_batch_running = True
            self.daily_batch_started_at = time.time()
            self.daily_batch_finished_at = None
            self.daily_batch_last_status = "running"
            worker = threading.Thread(
                target=self._run_daily_batch_worker,
                args=(replay_config, daily_batch_config),
                name="daily-batch-worker",
                daemon=True,
            )
            self._daily_batch_thread = worker
            try:
                worker.start()
            except Exception:
                self.daily_batch_running = False
                self.daily_batch_last_status = "launch_failed"
                self._daily_batch_thread = None
                raise
        log.info(
            "Daily batch launched asynchronously (replay_timeout={}s, memory_timeout={}s).",
            daily_batch_config["replay_timeout_seconds"],
            daily_batch_config["memory_timeout_seconds"],
        )
        return True

    def start_scheduler(self):
        execution_guards = self._build_execution_guards()
        log.info(
            "Adaptive scheduler started. Cadence minutes: 1h={} 15m={} 5m={} snapshot={}m recovery={}m case_path={}s",
            self.adaptive_loop_config["base_loop_minutes"],
            self.adaptive_loop_config["position_loop_minutes"],
            self.adaptive_loop_config["trigger_loop_minutes"],
            self.snapshot_service.materialization_minutes,
            execution_guards.get("recovery_sweep_minutes", 5),
            self.adaptive_loop_config["trade_case_snapshot_poll_seconds"],
        )
        next_snapshot_at = time.time()
        next_cycle_at = time.time()
        while True:
            now_epoch = time.time()
            self._run_due_maintenance_tasks(now_epoch)
            self._sample_trade_case_paths_if_due(now_epoch)
            self._maybe_alert_websocket_transitions()
            if now_epoch >= next_snapshot_at:
                snapshot_bundle = self.snapshot_service.materialize(now_epoch=now_epoch)
                self._maybe_alert_risk_state_transitions(snapshot_bundle.risk_state)
                next_snapshot_at = self.snapshot_service.next_materialization_epoch(now_epoch)
            if now_epoch >= next_cycle_at:
                current_mode = self.active_loop_mode
                was_ai_fallback_retry_cycle = bool(
                    current_mode == "1h" and getattr(self, "ai_fallback_retry_due", False)
                )
                loop_type = self._loop_label_for_mode(current_mode)
                snapshot_bundle = self.snapshot_service.materialize(now_epoch=now_epoch)
                cycle_result = self.run_cycle(
                    loop_type,
                    snapshot_bundle=snapshot_bundle,
                    loop_mode=current_mode,
                )
                cycle_finished_at = time.time()
                self._refresh_runtime_activity(cycle_result, cycle_finished_at)
                self._refresh_ai_fallback_retry_state(
                    cycle_result,
                    current_mode,
                    was_retry_cycle=was_ai_fallback_retry_cycle,
                )
                desired_mode = self._determine_desired_loop_mode(cycle_result)
                active_mode = self._apply_adaptive_loop_mode(
                    desired_mode,
                    cycle_finished_at,
                    cycle_result=cycle_result,
                )
                next_cycle_at = self._next_aligned_epoch(
                    self._scheduled_minutes_for_active_mode(active_mode),
                    cycle_finished_at,
                )
                log.info(
                    "Next cycle scheduled for epoch {} with active cadence {}",
                    int(next_cycle_at),
                    active_mode,
                )
                continue
            sleep_seconds = min(
                self.adaptive_loop_config["poll_seconds"],
                max(0.1, min(next_cycle_at, next_snapshot_at) - now_epoch),
            )
            time.sleep(sleep_seconds)
