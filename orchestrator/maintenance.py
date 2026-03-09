
import hashlib
import os
import subprocess
import sys
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
        replay_config = self._build_replay_batch_config()
        if replay_config["enabled"]:
            log.info("Running incremental replay batch before daily memory updater...")
            try:
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
                subprocess.run(replay_args, check=True)
                log.info("Incremental replay batch finished successfully.")
            except Exception as e:
                log.error(f"Incremental replay batch failed: {e}")
        log.info("Running daily memory updater batch...")
        try:
            # Reuse the current interpreter so the batch job sees the same virtualenv.
            subprocess.run([sys.executable, "-m", "batch.memory_updater"], check=True)
            log.info("Daily batch finished successfully.")
        except Exception as e:
            log.error(f"Daily batch failed: {e}")

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
                loop_type = self._loop_label_for_mode(current_mode)
                snapshot_bundle = self.snapshot_service.materialize(now_epoch=now_epoch)
                cycle_result = self.run_cycle(
                    loop_type,
                    snapshot_bundle=snapshot_bundle,
                    loop_mode=current_mode,
                )
                cycle_finished_at = time.time()
                self._refresh_runtime_activity(cycle_result, cycle_finished_at)
                desired_mode = self._determine_desired_loop_mode(cycle_result)
                active_mode = self._apply_adaptive_loop_mode(
                    desired_mode,
                    cycle_finished_at,
                    cycle_result=cycle_result,
                )
                next_cycle_at = self._next_aligned_epoch(
                    self._loop_minutes_for_mode(active_mode),
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
