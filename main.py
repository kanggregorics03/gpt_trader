import os
import hashlib
import subprocess
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

from ai.decision_engine import DecisionEngine, DecisionResult
from ai.rag_engine import RagEngine
from core.models import DecisionRequest, EvidenceItem, RationaleItem, TradeIntent
from core.risk_engine import RiskEngine
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
from utils.alerter import AlertManager
from utils.logger import log

load_dotenv()

class TradingOrchestrator:
    DEFAULT_EXECUTION_GUARDS = {
        "loop_lock_ttl_seconds": 240,
        "decision_cooldown_minutes": 10,
        "entry_mutex_ttl_seconds": 180,
        "unknown_order_block_minutes": 30,
        "recovery_sweep_minutes": 5,
    }
    DEFAULT_CONTEXT_BUDGET = {
        "journal_fetch_limit": 24,
        "journal_keep_limit": 6,
        "memory_keep_limit": 8,
        "similar_case_keep_limit": 5,
        "max_rationale_items_per_journal": 2,
        "max_evidence_per_rationale_item": 2,
        "max_ignore_when_per_memory": 2,
    }
    DEFAULT_ADAPTIVE_LOOP = {
        "base_loop_minutes": 60,
        "position_loop_minutes": 15,
        "trigger_loop_minutes": 5,
        "trigger_min_duration_minutes": 15,
        "trigger_clear_cycles": 3,
        "position_min_duration_minutes": 30,
        "flat_clear_cycles": 4,
        "recent_execution_window_minutes": 20,
        "recent_close_window_minutes": 20,
        "poll_seconds": 1,
    }
    DEFAULT_PRIVATE_ACCOUNT_WS = {
        "reconcile_ttl_ms": 600000,
        "ws_stale_after_seconds": 35,
        "ws_recover_stability_seconds": 12,
        "forced_reconcile_on_recover": True,
        "execution_cache_size": 200,
    }
    DEFAULT_PUBLIC_MARKET_WS = {
        "orderbook_depth": 10,
        "stream_ticker": True,
        "stream_orderbook": True,
        "stream_timeframes": [],
        "max_kline_rows": 120,
    }
    DEFAULT_MARKET_DATA_CACHE = {
        "ticker_ttl_ms": 5000,
        "order_book_ttl_ms": 5000,
        "funding_ttl_ms": 300000,
        "open_interest_ttl_ms": 30000,
    }
    LOOP_MODE_LABELS = {
        "1h": "Adaptive (1h)",
        "15m": "Adaptive (15m)",
        "5m": "Adaptive (5m)",
    }
    LOOP_MODE_PRIORITY = {"1h": 1, "15m": 2, "5m": 3}

    def __init__(self):
        log.info("Initializing Trading Orchestrator...")
        self.api_key = os.getenv("BYBIT_API_KEY")
        self.api_secret = os.getenv("BYBIT_API_SECRET")
        self.trading_mode = normalize_trading_mode(
            os.getenv("BYBIT_TRADING_MODE"),
            os.getenv("BYBIT_TESTNET"),
        )
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        self.gemini_default_model = os.getenv("GEMINI_DEFAULT_MODEL") or os.getenv("GEMINI_MODEL")
        self.gemini_trigger_model = os.getenv("GEMINI_TRIGGER_MODEL")
        self.db_path = os.getenv("DB_PATH", "data/gpt_trader.db")

        if not all([self.api_key, self.api_secret, self.gemini_key]):
            log.error("Missing required environment variables. Please check .env")
            exit(1)

        self.state_store = StateStore(self.db_path)
        self.risk_engine = RiskEngine()
        self.rag_engine = RagEngine(self.db_path)
        self.symbols = self.risk_engine.policy.get('trading', {}).get('symbols', ['BTCUSDT'])

        self.private_exchange = create_private_exchange(
            self.api_key,
            self.api_secret,
            trading_mode=self.trading_mode,
        )
        self.public_exchange = create_public_exchange()
        log.info(
            f"Bybit exchange routing: market_data=mainnet, private_trading={self.trading_mode}"
        )
        public_market_ws_config = self._build_public_market_ws_config()
        self.public_feed = create_public_market_feed(
            self.symbols,
            **public_market_ws_config,
        )
        private_account_ws_config = self._build_private_account_ws_config()
        self.private_account_feed = create_private_account_feed(
            self.api_key,
            self.api_secret,
            trading_mode=self.trading_mode,
            **private_account_ws_config,
        )

        self.account_reader = AccountReader(
            self.private_exchange,
            trading_mode=self.trading_mode,
            private_feed=self.private_account_feed,
        )
        self.market_collector = MarketCollector(
            self.public_exchange,
            public_feed=self.public_feed,
            response_cache_ttls_ms=self._build_market_data_cache_config(),
        )
        self.execution_engine = ExecutionEngine(
            self.private_exchange,
            private_feed=self.private_account_feed,
        )
        
        if self.gemini_default_model or self.gemini_trigger_model:
            self.ai_engine = DecisionEngine(
                self.gemini_key,
                cache_store=self.state_store,
                default_model=self.gemini_default_model,
                trigger_model=self.gemini_trigger_model,
            )
        else:
            self.ai_engine = DecisionEngine(self.gemini_key, cache_store=self.state_store)
        log.info(
            f"Gemini models: default={self.ai_engine.flash_model}, trigger={self.ai_engine.pro_model}"
        )
        self.alert_manager = AlertManager()
        materialization_minutes = int(
            self.risk_engine.policy.get("adaptive_loop", {}).get(
                "materialization_minutes",
                self.risk_engine.policy.get("intervals", {}).get("trigger_loop_minutes", 5),
            ) or 5
        )
        self.snapshot_service = SnapshotService(
            state_store=self.state_store,
            account_reader=self.account_reader,
            market_collector=self.market_collector,
            risk_engine=self.risk_engine,
            symbols=self.symbols,
            materialization_minutes=materialization_minutes,
        )
        self._initialize_runtime_state()

    def _build_private_account_ws_config(self):
        configured = self.risk_engine.policy.get("private_account_ws", {})
        config = dict(self.DEFAULT_PRIVATE_ACCOUNT_WS)
        config.update(configured or {})
        config["reconcile_ttl_ms"] = max(
            5000,
            int(config.get("reconcile_ttl_ms", self.DEFAULT_PRIVATE_ACCOUNT_WS["reconcile_ttl_ms"]) or 0),
        )
        config["ws_stale_after_seconds"] = max(
            5,
            int(
                config.get(
                    "ws_stale_after_seconds",
                    self.DEFAULT_PRIVATE_ACCOUNT_WS["ws_stale_after_seconds"],
                )
                or 0
            ),
        )
        config["ws_recover_stability_seconds"] = max(
            1,
            int(
                config.get(
                    "ws_recover_stability_seconds",
                    self.DEFAULT_PRIVATE_ACCOUNT_WS["ws_recover_stability_seconds"],
                )
                or 0
            ),
        )
        config["execution_cache_size"] = max(
            20,
            int(
                config.get(
                    "execution_cache_size",
                    self.DEFAULT_PRIVATE_ACCOUNT_WS["execution_cache_size"],
                )
                or 0
            ),
        )
        config["forced_reconcile_on_recover"] = bool(config.get("forced_reconcile_on_recover", True))
        return config

    def _build_public_market_ws_config(self):
        configured = self.risk_engine.policy.get("public_market_ws", {})
        config = dict(self.DEFAULT_PUBLIC_MARKET_WS)
        config.update(configured or {})
        config["orderbook_depth"] = max(
            1,
            int(config.get("orderbook_depth", self.DEFAULT_PUBLIC_MARKET_WS["orderbook_depth"]) or 0),
        )
        config["stream_ticker"] = bool(config.get("stream_ticker", True))
        config["stream_orderbook"] = bool(config.get("stream_orderbook", True))
        raw_timeframes = config.get("stream_timeframes", self.DEFAULT_PUBLIC_MARKET_WS["stream_timeframes"])
        config["timeframes"] = list(raw_timeframes or [])
        config.pop("stream_timeframes", None)
        config["max_kline_rows"] = max(
            20,
            int(config.get("max_kline_rows", self.DEFAULT_PUBLIC_MARKET_WS["max_kline_rows"]) or 0),
        )
        return config

    def _build_market_data_cache_config(self):
        configured = self.risk_engine.policy.get("market_data_cache", {})
        config = dict(self.DEFAULT_MARKET_DATA_CACHE)
        config.update(configured or {})
        key_mapping = {
            "ticker_ttl_ms": "ticker",
            "order_book_ttl_ms": "order_book",
            "funding_ttl_ms": "funding_rate",
            "open_interest_ttl_ms": "open_interest",
        }
        return {
            collector_key: max(250, int(config.get(policy_key, default_value) or default_value))
            for policy_key, default_value in self.DEFAULT_MARKET_DATA_CACHE.items()
            for collector_key in [key_mapping[policy_key]]
        }

    def _estimate_realized_pnl_pct(self, intent, account_state):
        if intent.decision not in ("REDUCE", "EXIT"):
            return None

        target_symbols = {order.symbol for order in intent.orders}
        if not target_symbols:
            target_symbols = {position.symbol for position in account_state.positions}

        matched_positions = [
            position.unrealized_pnl_pct
            for position in account_state.positions
            if position.symbol in target_symbols
        ]
        if not matched_positions:
            return None
        return sum(matched_positions) / len(matched_positions)

    def _calculate_balance_delta_pct(self, before_balance: float, after_balance: float):
        if before_balance <= 0:
            return None
        return ((after_balance - before_balance) / before_balance) * 100.0

    def _build_context_budget(self):
        configured = self.risk_engine.policy.get("context_budget", {})
        budget = dict(self.DEFAULT_CONTEXT_BUDGET)
        budget.update(configured or {})
        budget["journal_fetch_limit"] = max(6, int(budget.get("journal_fetch_limit", 24) or 24))
        budget["journal_keep_limit"] = max(3, int(budget.get("journal_keep_limit", 6) or 6))
        budget["memory_keep_limit"] = max(3, int(budget.get("memory_keep_limit", 8) or 8))
        budget["similar_case_keep_limit"] = max(1, int(budget.get("similar_case_keep_limit", 5) or 5))
        budget["max_rationale_items_per_journal"] = max(
            1,
            int(budget.get("max_rationale_items_per_journal", 2) or 2),
        )
        budget["max_evidence_per_rationale_item"] = max(
            1,
            int(budget.get("max_evidence_per_rationale_item", 2) or 2),
        )
        budget["max_ignore_when_per_memory"] = max(1, int(budget.get("max_ignore_when_per_memory", 2) or 2))
        return budget

    def _build_execution_guards(self):
        configured = self.risk_engine.policy.get("execution_guards", {})
        guards = dict(self.DEFAULT_EXECUTION_GUARDS)
        guards.update(configured or {})
        guards["loop_lock_ttl_seconds"] = max(30, int(guards.get("loop_lock_ttl_seconds", 240) or 240))
        guards["decision_cooldown_minutes"] = max(1, int(guards.get("decision_cooldown_minutes", 10) or 10))
        guards["entry_mutex_ttl_seconds"] = max(30, int(guards.get("entry_mutex_ttl_seconds", 180) or 180))
        guards["unknown_order_block_minutes"] = max(
            1,
            int(guards.get("unknown_order_block_minutes", 30) or 30),
        )
        guards["recovery_sweep_minutes"] = max(1, int(guards.get("recovery_sweep_minutes", 5) or 5))
        return guards

    def _build_adaptive_loop_config(self):
        intervals = self.risk_engine.policy.get("intervals", {})
        configured = self.risk_engine.policy.get("adaptive_loop", {})
        config = dict(self.DEFAULT_ADAPTIVE_LOOP)
        config["base_loop_minutes"] = int(intervals.get("strategy_loop_minutes", config["base_loop_minutes"]) or 60)
        config["position_loop_minutes"] = int(intervals.get("position_loop_minutes", config["position_loop_minutes"]) or 15)
        config["trigger_loop_minutes"] = int(intervals.get("trigger_loop_minutes", config["trigger_loop_minutes"]) or 5)
        config.update(configured or {})
        config["base_loop_minutes"] = max(5, int(config.get("base_loop_minutes", 60) or 60))
        config["position_loop_minutes"] = max(1, int(config.get("position_loop_minutes", 15) or 15))
        config["trigger_loop_minutes"] = max(1, int(config.get("trigger_loop_minutes", 5) or 5))
        config["trigger_min_duration_minutes"] = max(
            config["trigger_loop_minutes"],
            int(config.get("trigger_min_duration_minutes", 15) or 15),
        )
        config["trigger_clear_cycles"] = max(1, int(config.get("trigger_clear_cycles", 3) or 3))
        config["position_min_duration_minutes"] = max(
            config["position_loop_minutes"],
            int(config.get("position_min_duration_minutes", 30) or 30),
        )
        config["flat_clear_cycles"] = max(1, int(config.get("flat_clear_cycles", 4) or 4))
        config["recent_execution_window_minutes"] = max(
            1,
            int(config.get("recent_execution_window_minutes", 20) or 20),
        )
        config["recent_close_window_minutes"] = max(
            1,
            int(config.get("recent_close_window_minutes", 20) or 20),
        )
        config["poll_seconds"] = max(1, int(config.get("poll_seconds", 1) or 1))
        return config

    def _initialize_runtime_state(self):
        self.adaptive_loop_config = self._build_adaptive_loop_config()
        self.active_loop_mode = "1h"
        self.active_loop_mode_since = time.time()
        self.trigger_clear_streak = 0
        self.flat_clear_streak = 0
        self.last_execution_at = None
        self.last_close_at = None
        self.last_recovery_sweep_at = 0.0
        self.last_daily_batch_date = None

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
        if bool(cycle_result.get("trigger_active")):
            return "5m"
        if has_positions or has_open_orders or has_unknown_orders or recent_execution or recent_close:
            return "15m"
        return "1h"

    def _promote_loop_mode(self, mode: str, now_epoch: float):
        previous = self.active_loop_mode
        self.active_loop_mode = mode
        self.active_loop_mode_since = now_epoch
        self.trigger_clear_streak = 0
        self.flat_clear_streak = 0
        if previous != mode:
            log.info(f"Adaptive cadence promoted: {previous} -> {mode}")

    def _apply_adaptive_loop_mode(self, desired_mode: str, now_epoch: float) -> str:
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

        if current_mode == "5m":
            self.trigger_clear_streak += 1
            held_long_enough = (now_epoch - self.active_loop_mode_since) >= (
                self.adaptive_loop_config["trigger_min_duration_minutes"] * 60
            )
            if (
                not held_long_enough
                or self.trigger_clear_streak < self.adaptive_loop_config["trigger_clear_cycles"]
            ):
                return current_mode
            previous = self.active_loop_mode
            self.active_loop_mode = desired_mode
            self.active_loop_mode_since = now_epoch
            self.trigger_clear_streak = 0
            self.flat_clear_streak = 0
            log.info(f"Adaptive cadence downgraded: {previous} -> {desired_mode}")
            return self.active_loop_mode

        if current_mode == "15m" and desired_mode == "1h":
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

    def _build_effective_risk_profile(self, loop_mode: str, account_state, risk_profile):
        effective_profile = dict(risk_profile or {})
        allowed_actions = [
            str(action).upper()
            for action in effective_profile.get("allowed_actions", [])
        ]
        if loop_mode == "15m":
            effective_profile["cycle_role"] = "position_management"
            effective_profile["cycle_guidance"] = (
                "Prioritize managing live exposure and working orders. Avoid opening new positions in the 15m cycle."
            )
            effective_profile["allowed_actions"] = [
                action for action in allowed_actions
                if action in ("HOLD", "REDUCE", "EXIT")
            ]
        elif loop_mode == "5m":
            effective_profile["cycle_role"] = "trigger_response"
            effective_profile["cycle_guidance"] = (
                "React to active opportunity triggers quickly while respecting trigger-mode risk limits."
            )
            effective_profile["allowed_actions"] = allowed_actions
        else:
            effective_profile["cycle_role"] = "strategy"
            effective_profile["cycle_guidance"] = (
                "Set portfolio direction and initiate new exposure only when the higher-timeframe case is justified."
            )
            effective_profile["allowed_actions"] = allowed_actions
        effective_profile["has_live_positions"] = self._has_live_positions(account_state)
        effective_profile["has_live_open_orders"] = self._has_live_open_orders(account_state)
        return effective_profile

    def _run_due_maintenance_tasks(self, now_epoch: float):
        recovery_interval_seconds = self._build_execution_guards()["recovery_sweep_minutes"] * 60
        if (now_epoch - self.last_recovery_sweep_at) >= recovery_interval_seconds:
            self.run_recovery_sweep()
            self.last_recovery_sweep_at = time.time()

        now_local = datetime.now().astimezone()
        if (
            now_local.hour == 4
            and now_local.minute == 0
            and self.last_daily_batch_date != now_local.date().isoformat()
        ):
            self.run_daily_batch()
            self.last_daily_batch_date = now_local.date().isoformat()

    def _extract_entry_orders(self, orders):
        return [order for order in (orders or []) if not getattr(order, "reduce_only", False)]

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

    def _extract_symbols_from_rationale_items(self, rationale_items):
        symbols = set()
        for item in rationale_items or []:
            evidence_rows = (item.evidence or []) if hasattr(item, "evidence") else (item.get("evidence") or [])
            for evidence in evidence_rows:
                symbol = evidence.symbol if hasattr(evidence, "symbol") else evidence.get("symbol")
                if symbol:
                    symbols.add(str(symbol))
        return symbols

    def _compact_recent_journal_row(self, row, max_rationale_items: int, max_evidence_per_item: int):
        rationale = row.get("rationale") or []
        compact_rationale = rationale[:2] if isinstance(rationale, list) else [str(rationale)]
        compact_items = []
        for item in (row.get("rationale_items") or [])[:max_rationale_items]:
            evidence_rows = (item.evidence or []) if hasattr(item, "evidence") else (item.get("evidence") or [])
            compact_evidence = []
            for evidence in evidence_rows[:max_evidence_per_item]:
                compact_evidence.append(
                    {
                        "signal": evidence.signal if hasattr(evidence, "signal") else evidence.get("signal"),
                        "symbol": evidence.symbol if hasattr(evidence, "symbol") else evidence.get("symbol"),
                        "timeframe": evidence.timeframe if hasattr(evidence, "timeframe") else evidence.get("timeframe"),
                        "operator": evidence.operator if hasattr(evidence, "operator") else evidence.get("operator"),
                        "observed_value": (
                            evidence.observed_value if hasattr(evidence, "observed_value") else evidence.get("observed_value")
                        ),
                        "threshold": evidence.threshold if hasattr(evidence, "threshold") else evidence.get("threshold"),
                        "units": evidence.units if hasattr(evidence, "units") else evidence.get("units"),
                        "source": evidence.source if hasattr(evidence, "source") else evidence.get("source"),
                    }
                )
            compact_items.append(
                {
                    "summary": item.summary if hasattr(item, "summary") else item.get("summary"),
                    "why_it_matters": item.why_it_matters if hasattr(item, "why_it_matters") else item.get("why_it_matters"),
                    "trade_implication": item.trade_implication if hasattr(item, "trade_implication") else item.get("trade_implication"),
                    "failure_mode": item.failure_mode if hasattr(item, "failure_mode") else item.get("failure_mode"),
                    "evidence": compact_evidence,
                }
            )
        return {
            "journal_id": row.get("journal_id") or row.get("id"),
            "audit_log_id": row.get("audit_log_id"),
            "timestamp": row.get("timestamp"),
            "decision": row.get("decision") or "HOLD",
            "rationale": compact_rationale,
            "rationale_items": compact_items,
            "confidence": row.get("confidence") or 0.0,
            "risk_reason": row.get("risk_reason") or "",
            "estimated_pnl_pct": row.get("estimated_pnl_pct"),
            "loss_signal": bool(row.get("loss_signal")),
            "ledger_net_realized_pnl": row.get("ledger_net_realized_pnl") or 0.0,
            "ledger_fee_cost": row.get("ledger_fee_cost") or 0.0,
            "risk_approved": row.get("risk_approved"),
            "risk_mode": row.get("risk_mode"),
            "trigger_symbols": row.get("trigger_symbols") or [],
            "risk_trigger_symbols": row.get("risk_trigger_symbols") or [],
            "evidence_quality": row.get("evidence_quality") or {},
            "entry_size_cap": row.get("entry_size_cap") or {},
            "summary": row.get("summary"),
        }

    def _select_recent_journal_context(
        self,
        rows,
        market_snapshots,
        risk_mode: str,
        limit: int,
        max_rationale_items: int,
        max_evidence_per_item: int,
    ):
        if not rows:
            return []
        current_symbols = set(market_snapshots.keys())
        scored = []
        for index, row in enumerate(rows):
            score = 0.0
            decision = str(row.get("decision") or "HOLD").upper()
            if decision in ("BUY", "SELL", "REDUCE", "EXIT"):
                score += 12.0
            if bool(row.get("loss_signal")):
                score += 10.0
            if str(row.get("risk_mode") or "") == str(risk_mode or ""):
                score += 8.0
            if row.get("estimated_pnl_pct") is not None:
                score += 4.0
            trigger_symbols = set(row.get("trigger_symbols") or [])
            risk_trigger_symbols = set(row.get("risk_trigger_symbols") or [])
            evidence_symbols = self._extract_symbols_from_rationale_items(row.get("rationale_items") or [])
            if current_symbols and current_symbols.intersection(trigger_symbols):
                score += 10.0
            if current_symbols and current_symbols.intersection(risk_trigger_symbols):
                score += 8.0
            if current_symbols and current_symbols.intersection(evidence_symbols):
                score += 6.0
            if str(row.get("risk_reason") or "") not in ("", "Approved", "Safe action."):
                score += 2.0
            score += max(0.0, 6.0 - (index * 0.25))
            scored.append((score, index, row))

        selected = sorted(scored, key=lambda item: (-item[0], item[1]))[: max(1, limit)]
        selected_indices = {index for _, index, _ in selected}
        ordered_rows = [rows[index] for index in range(len(rows)) if index in selected_indices]
        return [
            self._compact_recent_journal_row(
                row,
                max_rationale_items=max_rationale_items,
                max_evidence_per_item=max_evidence_per_item,
            )
            for row in ordered_rows
        ]

    def _compact_memory_record(self, row, max_ignore_when: int):
        metrics = row.get("metrics") or {}
        apply_when = row.get("apply_when") or metrics.get("apply_when") or {
            "regime": [row.get("regime")] if row.get("regime") else [],
            "volatility": [],
            "trend": [],
        }
        ignore_when = row.get("ignore_when") or metrics.get("ignore_when") or []
        evidence_ids = row.get("evidence_journal_ids") or metrics.get("evidence_journal_ids") or []
        compact_metrics = {
            "support": int(row.get("support") or metrics.get("support") or 0),
            "status": row.get("status") or metrics.get("status") or "rule",
            "category": row.get("category") or metrics.get("category") or "lesson",
            "regime": row.get("regime") or metrics.get("regime") or "general",
            "apply_when": apply_when,
            "ignore_when": ignore_when[:max_ignore_when],
            "evidence_journal_ids": evidence_ids[:10],
        }
        return {
            "rule": row.get("rule") or "",
            "category": compact_metrics["category"],
            "status": compact_metrics["status"],
            "support": compact_metrics["support"],
            "regime": compact_metrics["regime"],
            "apply_when": compact_metrics["apply_when"],
            "ignore_when": compact_metrics["ignore_when"],
            "evidence_journal_ids": compact_metrics["evidence_journal_ids"],
            "metrics": compact_metrics,
        }

    def _select_long_term_memory_context(self, rows, market_snapshots, risk_mode: str, limit: int, max_ignore_when: int):
        if not rows:
            return []
        current_symbols = set(market_snapshots.keys())
        active_trends = set()
        active_volatility = set()
        for snapshot in market_snapshots.values():
            active_trends.add(str(getattr(snapshot.regime, "trend_alignment", "") or ""))
            active_volatility.add(str(getattr(snapshot.regime, "volatility_regime", "") or ""))

        scored = []
        for index, row in enumerate(rows):
            score = 0.0
            status = str(row.get("status") or "rule")
            support = int(row.get("support") or 0)
            category = str(row.get("category") or "lesson")
            rule_text = str(row.get("rule") or "")
            apply_when = row.get("apply_when") or {}
            apply_regimes = set(apply_when.get("regime") or [])
            apply_trends = set(apply_when.get("trend") or [])
            apply_volatility = set(apply_when.get("volatility") or [])

            score += 8.0 if status == "rule" else 3.0
            score += min(10.0, float(support))
            if category == "risk_control":
                score += 4.0
            if str(risk_mode or "") in apply_regimes:
                score += 8.0
            if active_trends and active_trends.intersection(apply_trends):
                score += 6.0
            if active_volatility and active_volatility.intersection(apply_volatility):
                score += 5.0
            if current_symbols and any(symbol in rule_text for symbol in current_symbols):
                score += 4.0
            score += max(0.0, 4.0 - (index * 0.15))
            scored.append((score, index, row))

        selected = sorted(scored, key=lambda item: (-item[0], item[1]))[: max(1, limit)]
        selected_indices = {index for _, index, _ in selected}
        ordered_rows = [rows[index] for index in range(len(rows)) if index in selected_indices]
        return [
            self._compact_memory_record(
                row,
                max_ignore_when=max_ignore_when,
            )
            for row in ordered_rows
        ]

    def _build_similar_past_cases(
        self,
        market_snapshots,
        account_state,
        risk_state,
        is_trigger_mode: bool,
        limit: int = 5,
    ):
        if not is_trigger_mode:
            return []

        account_payload = (
            account_state.model_dump(mode="json")
            if hasattr(account_state, "model_dump")
            else dict(account_state or {})
        )
        risk_payload = (
            risk_state.model_dump(mode="json")
            if hasattr(risk_state, "model_dump")
            else dict(risk_state or {})
        )
        current_markets = {
            symbol: snapshot.model_dump(mode="json")
            for symbol, snapshot in market_snapshots.items()
        }
        batch_cases = self.rag_engine.get_similar_cases_batch(
            current_markets,
            limit_per_symbol=3,
            account_state=account_payload,
            risk_state=risk_payload,
        )

        cases = []
        seen = set()
        for symbol, raw_cases in batch_cases.items():
            if not market_snapshots[symbol].is_opportunity_trigger and any(
                snapshot.is_opportunity_trigger for snapshot in market_snapshots.values()
            ):
                continue
            for case in raw_cases:
                tagged_case = f"{symbol}: {case}"
                if tagged_case in seen:
                    continue
                seen.add(tagged_case)
                cases.append(tagged_case)
                if len(cases) >= max(1, int(limit or 1)):
                    return cases[: max(1, int(limit or 1))]
        return cases[: max(1, int(limit or 1))]

    def _build_position_ledger_entries(self, intent, fills, execution_summary):
        if not fills:
            return []

        grouped = {}
        for fill in fills:
            symbol = fill.get("symbol") or "UNKNOWN"
            entry = grouped.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "decision": intent.decision,
                    "fill_count": 0,
                    "total_amount": 0.0,
                    "gross_realized_pnl": 0.0,
                    "fee_cost": 0.0,
                    "net_realized_pnl": 0.0,
                    "balance_delta": 0.0,
                    "balance_delta_pct": 0.0,
                },
            )
            entry["fill_count"] += 1
            entry["total_amount"] += float(fill.get("amount") or 0.0)
            entry["gross_realized_pnl"] += float(fill.get("realized_pnl") or 0.0)
            entry["fee_cost"] += float(fill.get("fee_cost") or 0.0)

        weights = {}
        total_weight = 0.0
        for symbol, entry in grouped.items():
            weight = abs(entry["gross_realized_pnl"]) + abs(entry["fee_cost"]) + abs(entry["total_amount"])
            if weight == 0:
                weight = 1.0
            weights[symbol] = weight
            total_weight += weight

        balance_delta = execution_summary.get("balance_delta")
        balance_delta_pct = execution_summary.get("balance_delta_pct")
        for symbol, entry in grouped.items():
            entry["net_realized_pnl"] = round(entry["gross_realized_pnl"] - entry["fee_cost"], 8)
            entry["total_amount"] = round(entry["total_amount"], 8)
            entry["gross_realized_pnl"] = round(entry["gross_realized_pnl"], 8)
            entry["fee_cost"] = round(entry["fee_cost"], 8)
            if balance_delta is not None and total_weight > 0:
                entry["balance_delta"] = round(float(balance_delta) * (weights[symbol] / total_weight), 8)
            else:
                entry["balance_delta"] = None
            if balance_delta_pct is not None and total_weight > 0:
                entry["balance_delta_pct"] = round(float(balance_delta_pct) * (weights[symbol] / total_weight), 8)
            else:
                entry["balance_delta_pct"] = None

        return list(grouped.values())

    def _build_symbol_journal_entries(
        self,
        audit_log_id: int,
        intent,
        market_snapshots,
        trigger_symbols,
        risk_trigger_symbols,
        risk_reason: str,
        risk_mode: str,
        risk_approved: bool,
        estimated_pnl_pct,
        ledger_entries,
    ):
        ledger_by_symbol = {
            str((entry or {}).get("symbol")): (entry or {})
            for entry in (ledger_entries or [])
            if (entry or {}).get("symbol")
        }
        symbol_order_actions = {}
        for order in (intent.orders or []):
            symbol_order_actions.setdefault(order.symbol, []).append(
                f"{order.action}:{order.order_type}:{order.quantity}"
            )

        symbol_evidence = {}
        for item in (intent.rationale_items or []):
            evidence_rows = (item.evidence or []) if hasattr(item, "evidence") else (item.get("evidence") or [])
            for evidence in evidence_rows:
                signal = evidence.signal if hasattr(evidence, "signal") else evidence.get("signal")
                timeframe = evidence.timeframe if hasattr(evidence, "timeframe") else evidence.get("timeframe")
                symbol = evidence.symbol if hasattr(evidence, "symbol") else evidence.get("symbol")
                if not signal:
                    continue
                tag = f"{signal}@{timeframe or 'context'}"
                key = symbol or "__GLOBAL__"
                symbol_evidence.setdefault(key, [])
                if tag not in symbol_evidence[key]:
                    symbol_evidence[key].append(tag)

        symbols = sorted(
            set(market_snapshots.keys())
            | set(ledger_by_symbol.keys())
            | set(symbol_order_actions.keys())
        )
        summary_seed = intent.rationale[0] if intent.rationale else ""
        entries = []
        for symbol in symbols:
            ledger_entry = ledger_by_symbol.get(symbol, {})
            local_signals = list(symbol_evidence.get(symbol, []))
            local_signals.extend(
                signal for signal in symbol_evidence.get("__GLOBAL__", [])
                if signal not in local_signals
            )
            entries.append(
                {
                    "audit_log_id": audit_log_id,
                    "symbol": symbol,
                    "decision": intent.decision,
                    "risk_reason": risk_reason,
                    "risk_mode": risk_mode,
                    "risk_approved": risk_approved,
                    "triggered": symbol in trigger_symbols,
                    "risk_triggered": symbol in risk_trigger_symbols,
                    "net_realized_pnl": ledger_entry.get("net_realized_pnl"),
                    "fee_cost": ledger_entry.get("fee_cost"),
                    "estimated_pnl_pct": estimated_pnl_pct,
                    "evidence_signals": local_signals,
                    "summary": summary_seed,
                    "payload": {
                        "order_actions": symbol_order_actions.get(symbol, []),
                        "trigger_reasons": (
                            market_snapshots[symbol].trigger_reasons
                            if symbol in market_snapshots
                            else []
                        ),
                        "opportunity_trigger_reasons": (
                            market_snapshots[symbol].opportunity_trigger_reasons
                            if symbol in market_snapshots
                            else []
                        ),
                        "risk_trigger_reasons": (
                            market_snapshots[symbol].risk_trigger_reasons
                            if symbol in market_snapshots
                            else []
                        ),
                    },
                }
            )
        return entries

    def _get_deterministic_skip_reason(
        self,
        account_state,
        market_snapshots,
        risk_state,
        risk_mode: str,
        risk_profile,
    ):
        if account_state.positions or account_state.open_orders:
            return None

        risk_limits = self.risk_engine.policy.get("risk_limits", {})
        daily_max_loss_pct = float(risk_limits.get("daily_max_loss_pct", 0.0) or 0.0)
        daily_pnl_pct = float(risk_state.get("daily_pnl_pct", 0.0) or 0.0)
        if daily_max_loss_pct > 0 and daily_pnl_pct <= -daily_max_loss_pct:
            return "daily_loss_kill_switch"

        max_consecutive_losses = int(risk_limits.get("max_consecutive_losses", 0) or 0)
        consecutive_losses = int(risk_state.get("consecutive_losses", 0) or 0)
        if max_consecutive_losses > 0 and consecutive_losses >= max_consecutive_losses:
            return "consecutive_loss_kill_switch"

        if bool(risk_state.get("cooldown_active")):
            return "cooldown_active"

        allowed_actions = {str(action).upper() for action in risk_profile.get("allowed_actions", [])}
        if "BUY" not in allowed_actions and "SELL" not in allowed_actions:
            return f"risk_profile_{risk_mode}_blocks_entries"

        return None

    def _build_skip_rationale_items(self, skip_reason: str, risk_state, market_snapshots, risk_profile):
        if skip_reason == "cooldown_active":
            remaining = float(risk_state.get("cooldown_remaining_minutes", 0) or 0.0)
            return [
                RationaleItem(
                    summary="Cooldown blocks new entries.",
                    why_it_matters="The strategy is in mandatory cooldown after recent losses.",
                    trade_implication="Keep the portfolio flat until cooldown expires.",
                    failure_mode="Cooldown expires and no other blocking risk state remains.",
                    evidence=[
                        EvidenceItem(
                            signal="cooldown_remaining_minutes",
                            timeframe="risk",
                            operator="gt",
                            observed_value=remaining,
                            threshold=0.0,
                            units="minutes",
                            source="risk_state.cooldown_remaining_minutes",
                        )
                    ],
                )
            ]
        if skip_reason.startswith("risk_profile_"):
            max_positions = float(risk_profile.get("max_open_positions", 0.0) or 0.0)
            return [
                RationaleItem(
                    summary="Active risk profile disallows new entries.",
                    why_it_matters="Current policy mode only permits defensive actions.",
                    trade_implication="Return HOLD until the risk profile re-enables BUY or SELL.",
                    failure_mode="Risk mode changes and entry actions are allowed again.",
                    evidence=[
                        EvidenceItem(
                            signal="max_open_positions",
                            timeframe="risk",
                            operator="note",
                            observed_value=max_positions,
                            threshold=None,
                            units="count",
                            source="risk_profile.max_open_positions",
                        )
                    ],
                )
            ]
        if skip_reason == "daily_loss_kill_switch":
            daily_pnl_pct = float(risk_state.get("daily_pnl_pct", 0.0) or 0.0)
            limit = float(self.risk_engine.policy.get("risk_limits", {}).get("daily_max_loss_pct", 0.0) or 0.0)
            return [
                RationaleItem(
                    summary="Daily loss kill switch is active.",
                    why_it_matters="Further entries would violate the daily drawdown control.",
                    trade_implication="Stop initiating new risk for the rest of the daily window.",
                    failure_mode="A new trading day resets the daily loss state.",
                    evidence=[
                        EvidenceItem(
                            signal="daily_pnl_pct",
                            timeframe="risk",
                            operator="lte",
                            observed_value=daily_pnl_pct,
                            threshold=-limit,
                            units="pct",
                            source="risk_state.daily_pnl_pct",
                        )
                    ],
                )
            ]
        if skip_reason == "consecutive_loss_kill_switch":
            losses = float(risk_state.get("consecutive_losses", 0) or 0.0)
            limit = float(self.risk_engine.policy.get("risk_limits", {}).get("max_consecutive_losses", 0) or 0.0)
            return [
                RationaleItem(
                    summary="Consecutive-loss kill switch is active.",
                    why_it_matters="The recent loss streak signals degraded strategy conditions.",
                    trade_implication="Pause new entries until the loss streak resets.",
                    failure_mode="The loss streak resets under the configured policy rules.",
                    evidence=[
                        EvidenceItem(
                            signal="consecutive_losses",
                            timeframe="risk",
                            operator="gte",
                            observed_value=losses,
                            threshold=limit,
                            units="count",
                            source="risk_state.consecutive_losses",
                        )
                    ],
                )
            ]
        return []

    def run_cycle(self, loop_type: str, snapshot_bundle=None, loop_mode: str | None = None):
        log.info(f"--- Starting {loop_type} Cycle ---")
        execution_guards = self._build_execution_guards()
        loop_mode = loop_mode or self.active_loop_mode
        loop_lock_name = "trading_orchestrator_loop"
        loop_lock_owner = f"{loop_type}:{os.getpid()}:{int(time.time() * 1000)}"
        if not self.state_store.try_acquire_runtime_lock(
            loop_lock_name,
            owner=loop_lock_owner,
            ttl_seconds=execution_guards["loop_lock_ttl_seconds"],
        ):
            log.warning(f"Skipping {loop_type}: loop lock is already held by another worker.")
            return {
                "loop_type": loop_type,
                "trigger_active": self.active_loop_mode == "5m",
                "has_positions": True,
                "has_open_orders": True,
                "has_unknown_orders": True,
                "executed_order_count": 0,
                "position_closed": False,
                "final_account_state": None,
                "locked": True,
            }
        try:
            if snapshot_bundle is None:
                snapshot_bundle = self.snapshot_service.build_cycle_snapshot()

            account_state = snapshot_bundle.account_state
            risk_state = snapshot_bundle.risk_state
            policy_summary = snapshot_bundle.policy_summary
            market_snapshots = snapshot_bundle.market_snapshots
            market_prices = snapshot_bundle.market_prices
            is_trigger_mode = snapshot_bundle.is_trigger_mode
            risk_mode = snapshot_bundle.risk_mode
            risk_profile = snapshot_bundle.risk_profile
            effective_risk_profile = self._build_effective_risk_profile(
                loop_mode,
                account_state=account_state,
                risk_profile=risk_profile,
            )

            # 3. Decision
            skip_reason = self._get_deterministic_skip_reason(
                account_state=account_state,
                market_snapshots=market_snapshots,
                risk_state=risk_state,
                risk_mode=risk_mode,
                risk_profile=effective_risk_profile,
            )
            request = None
            if skip_reason:
                request = DecisionRequest(
                    policy_summary=policy_summary,
                    account_state=account_state,
                    market_snapshot=next(iter(market_snapshots.values())) if len(market_snapshots) == 1 else None,
                    market_snapshots=market_snapshots,
                    risk_state=risk_state,
                    recent_journal_context=[],
                    long_term_memory=[],
                    similar_past_cases=[],
                    is_trigger_mode=is_trigger_mode,
                    risk_mode=risk_mode,
                    risk_profile=effective_risk_profile,
                )
                input_hash = self.ai_engine._build_candidate_input_hash(request)
                log.info(f"Skipping AI decision call: {skip_reason}")
                rationale_items = self._build_skip_rationale_items(
                    skip_reason=skip_reason,
                    risk_state=risk_state,
                    market_snapshots=market_snapshots,
                    risk_profile=risk_profile,
                )
                decision_result = DecisionResult(
                    intent=TradeIntent(
                        decision="HOLD",
                        orders=[],
                        rationale=[f"Deterministic HOLD: {skip_reason}."],
                        rationale_items=rationale_items,
                        risk_notes=[skip_reason],
                        confidence=1.0,
                    ),
                    prompt="",
                    raw_response="",
                    model_name="deterministic_skip",
                    input_hash=input_hash,
                    cache_hit=False,
                    usage_metadata={"skipped": True, "reason": skip_reason},
                )
            else:
                context_budget = self._build_context_budget()
                raw_recent_journal_context = self.state_store.get_recent_journal_context(
                    limit=context_budget["journal_fetch_limit"]
                )
                raw_long_term_memory = self.state_store.get_long_term_memory()
                recent_journal_context = self._select_recent_journal_context(
                    raw_recent_journal_context,
                    market_snapshots=market_snapshots,
                    risk_mode=risk_mode,
                    limit=context_budget["journal_keep_limit"],
                    max_rationale_items=context_budget["max_rationale_items_per_journal"],
                    max_evidence_per_item=context_budget["max_evidence_per_rationale_item"],
                )
                long_term_memory = self._select_long_term_memory_context(
                    raw_long_term_memory,
                    market_snapshots=market_snapshots,
                    risk_mode=risk_mode,
                    limit=context_budget["memory_keep_limit"],
                    max_ignore_when=context_budget["max_ignore_when_per_memory"],
                )
                similar_past_cases = self._build_similar_past_cases(
                    market_snapshots,
                    account_state=account_state,
                    risk_state=risk_state,
                    is_trigger_mode=is_trigger_mode,
                    limit=context_budget["similar_case_keep_limit"],
                )
                log.info(
                    "Context budget applied: journals {}->{}, memory {}->{}, similar_cases={}",
                    len(raw_recent_journal_context),
                    len(recent_journal_context),
                    len(raw_long_term_memory),
                    len(long_term_memory),
                    len(similar_past_cases),
                )
                request = DecisionRequest(
                    policy_summary=policy_summary,
                    account_state=account_state,
                    market_snapshot=next(iter(market_snapshots.values())) if len(market_snapshots) == 1 else None,
                    market_snapshots=market_snapshots,
                    risk_state=risk_state,
                    recent_journal_context=recent_journal_context,
                    long_term_memory=long_term_memory,
                    similar_past_cases=similar_past_cases,
                    is_trigger_mode=is_trigger_mode,
                    risk_mode=risk_mode,
                    risk_profile=effective_risk_profile,
                )
                decision_result = self.ai_engine.generate_decision(request)
            intent = decision_result.intent

            # 4. Risk Evaluation
            unresolved_timeout_count = self.state_store.count_recent_unknown_orders(
                minutes=execution_guards["unknown_order_block_minutes"]
            )
            is_approved, reason = self.risk_engine.evaluate_intent(
                intent,
                account_state,
                market_snapshots=market_snapshots,
                market_prices=market_prices,
                risk_state=risk_state,
                is_trigger_mode=is_trigger_mode,
                unresolved_timeout_count=unresolved_timeout_count,
                active_profile_override=effective_risk_profile,
            )
            evidence_quality_summary = self.risk_engine.get_evidence_quality_summary(intent)
            entry_size_cap_summary = self.risk_engine.get_effective_max_order_notional_pct_balance(
                intent=intent,
                active_profile=effective_risk_profile,
            )

            # 5. Execution
            planned_orders = []
            receipts = []
            fills = []
            effective_pre_execution_state = account_state
            trigger_symbols = [
                symbol
                for symbol, snapshot in market_snapshots.items()
                if snapshot.is_opportunity_trigger
            ]
            risk_trigger_symbols = [
                symbol
                for symbol, snapshot in market_snapshots.items()
                if snapshot.is_risk_trigger
            ]
            execution_summary = {
                "loop_type": loop_type,
                "symbols": list(market_snapshots.keys()),
                "trigger_mode": is_trigger_mode,
                "trigger_symbols": trigger_symbols,
                "risk_trigger_symbols": risk_trigger_symbols,
                "risk_mode": risk_mode,
                "risk_profile": risk_profile,
                "effective_risk_profile": effective_risk_profile,
                "cycle_role": effective_risk_profile.get("cycle_role"),
                "risk_state": risk_state,
                "decision_input_hash": decision_result.input_hash,
                "ai_usage": decision_result.usage_metadata or {},
                "evidence_quality": evidence_quality_summary,
                "entry_size_cap": entry_size_cap_summary,
                "executed_order_count": 0,
                "unknown_order_states_before_execution": unresolved_timeout_count,
            }
            post_account_state = None
            if is_approved and intent.decision in ("BUY", "SELL", "REDUCE", "EXIT"):
                locked_entry_symbols = []
                try:
                    self.account_reader.invalidate_cache()
                    effective_pre_execution_state = self.account_reader.get_account_state(
                        self.symbols,
                        force_refresh=True,
                    )
                    unresolved_after_reconcile = self.state_store.count_recent_unknown_orders(
                        minutes=execution_guards["unknown_order_block_minutes"]
                    )
                    reapproved, rereason = self.risk_engine.evaluate_intent(
                        intent,
                        effective_pre_execution_state,
                        market_snapshots=market_snapshots,
                        market_prices=market_prices,
                        risk_state=risk_state,
                        is_trigger_mode=is_trigger_mode,
                        unresolved_timeout_count=unresolved_after_reconcile,
                        active_profile_override=effective_risk_profile,
                    )
                    execution_summary["pre_trade_reconciliation"] = {
                        "reapproved": bool(reapproved),
                        "reason": rereason,
                        "unknown_order_states": unresolved_after_reconcile,
                    }
                    if not reapproved:
                        is_approved = False
                        reason = f"Pre-trade reconciliation rejected: {rereason}"
                    else:
                        planned_orders = self.execution_engine.resolve_orders(
                            intent,
                            account_state=effective_pre_execution_state,
                        )
                        entry_orders = self._extract_entry_orders(planned_orders)
                        fingerprint_entries = [
                            self._build_order_fingerprint(order, market_prices)
                            for order in entry_orders
                        ]
                        cooldown_minutes = execution_guards["decision_cooldown_minutes"]
                        repeated = []
                        for fp in fingerprint_entries:
                            if self.state_store.has_recent_decision_fingerprint(
                                symbol=fp["symbol"],
                                action=fp["action"],
                                size_band=fp["size_band"],
                                within_minutes=cooldown_minutes,
                            ):
                                repeated.append(f"{fp['symbol']}:{fp['action']}:{fp['size_band']}")
                        if repeated:
                            is_approved = False
                            reason = (
                                f"Decision cooldown active ({cooldown_minutes}m): repeated order fingerprint "
                                f"detected for {', '.join(sorted(repeated))}."
                            )
                            execution_summary["decision_cooldown_blocked"] = sorted(repeated)
                        else:
                            lock_ok, locked_entry_symbols = self._acquire_entry_mutexes(
                                entry_orders,
                                owner=loop_lock_owner,
                                ttl_seconds=execution_guards["entry_mutex_ttl_seconds"],
                            )
                            if not lock_ok:
                                is_approved = False
                                reason = "Entry mutex active for at least one symbol. Skipping duplicate entry execution."
                            else:
                                if fingerprint_entries:
                                    self.state_store.save_decision_fingerprints(
                                        [
                                            {
                                                **fp,
                                                "audit_log_id": None,
                                                "status": "planned",
                                            }
                                            for fp in fingerprint_entries
                                        ]
                                    )
                                log.info(f"Risk Approved. Executing {len(planned_orders)} orders.")
                                receipts = self.execution_engine.execute_intent(
                                    intent,
                                    account_state=effective_pre_execution_state,
                                )
                                execution_summary["executed_order_count"] = len(receipts)
                                execution_summary["receipt_summary"] = self.execution_engine.summarize_receipts(receipts)
                                fills = self.execution_engine.fetch_fills_for_receipts(receipts)
                                execution_summary["fill_summary"] = {
                                    "fill_count": len(fills),
                                    "fee_cost": round(sum(float(fill.get("fee_cost") or 0.0) for fill in fills), 8),
                                    "realized_pnl": round(sum(float(fill.get("realized_pnl") or 0.0) for fill in fills), 8),
                                }
                finally:
                    self._release_entry_mutexes(locked_entry_symbols, owner=loop_lock_owner)
                if is_approved and receipts:
                    try:
                        self.account_reader.invalidate_cache()
                        post_account_state = self.account_reader.get_account_state(
                            self.symbols,
                            force_refresh=True,
                        )
                        execution_summary["post_account_state"] = post_account_state.model_dump()
                        execution_summary["balance_delta"] = round(
                            post_account_state.total_balance - effective_pre_execution_state.total_balance,
                            8,
                        )
                        execution_summary["balance_delta_pct"] = self._calculate_balance_delta_pct(
                            effective_pre_execution_state.total_balance,
                            post_account_state.total_balance,
                        )
                    except Exception as execution_state_error:
                        execution_summary["post_account_state_error"] = str(execution_state_error)
                        log.warning(f"Failed to fetch post-execution account state: {execution_state_error}")
                unresolved = [
                    receipt for receipt in receipts
                    if receipt.get("status") == "timeout_unresolved"
                ]
                if unresolved:
                    unresolved_symbols = sorted(
                        {
                            receipt.get("symbol")
                            for receipt in unresolved
                            if receipt.get("symbol")
                        }
                    )
                    self.alert_manager.send(
                        "Execution Timeout",
                        (
                            f"{len(unresolved)} unresolved timed out order(s)"
                            f" for {', '.join(unresolved_symbols) if unresolved_symbols else 'portfolio'}."
                        ),
                        level="error",
                    )
            if not is_approved:
                log.warning(f"Risk Rejected: {reason}")
                if (
                    "Kill switch" in reason
                    or "Cooldown" in reason
                    or "unresolved order lifecycle" in reason.lower()
                    or "decision cooldown active" in reason.lower()
                ):
                    self.alert_manager.send(
                        "Risk Engine Blocked Trade",
                        f"Portfolio {intent.decision} rejected: {reason}",
                        level="warning",
                    )

            # 6. Logging
            estimated_pnl_pct = execution_summary.get("balance_delta_pct")
            if estimated_pnl_pct is None:
                estimated_pnl_pct = self._estimate_realized_pnl_pct(intent, effective_pre_execution_state)
            loss_signal = estimated_pnl_pct is not None and estimated_pnl_pct < 0
            account_payload = effective_pre_execution_state.model_dump(mode="json")
            risk_payload = (
                risk_state.model_dump(mode="json")
                if hasattr(risk_state, "model_dump")
                else dict(risk_state or {})
            )
            audit_market = {
                "symbols": list(market_snapshots.keys()),
                "market_snapshots": {
                    symbol: snapshot.model_dump(mode="json")
                    for symbol, snapshot in market_snapshots.items()
                },
                "trigger_symbols": trigger_symbols,
                "risk_trigger_symbols": risk_trigger_symbols,
                "rag_context": {
                    "risk_state": risk_payload,
                    "account_state": account_payload,
                },
            }
            audit_id = self.state_store.save_audit(
                account=account_payload,
                market=audit_market,
                intent=intent.model_dump(),
                risk_ok=is_approved,
                reason=reason,
                receipts=receipts,
                policy_snapshot=policy_summary,
                decision_input_hash=decision_result.input_hash,
                risk_mode=risk_mode,
                risk_profile=risk_profile,
                decision_request=request.model_dump(),
                ai_raw_response=decision_result.raw_response,
                execution_summary=execution_summary,
                market_vectors=self.rag_engine.build_market_vectors(
                    audit_market["market_snapshots"],
                    account_state=account_payload,
                    risk_state=risk_payload,
                ),
            )
            if audit_id is not None:
                ledger_entries = []
                if fills:
                    self.state_store.save_execution_fills(audit_id, fills)
                    ledger_entries = self._build_position_ledger_entries(intent, fills, execution_summary)
                    self.state_store.save_position_ledger_entries(audit_id, ledger_entries)
                if planned_orders:
                    order_state_transitions = self.execution_engine.build_order_state_transitions(
                        planned_orders,
                        receipts,
                        fills,
                    )
                    position_state_transitions = self.execution_engine.build_position_state_transitions(
                        intent,
                        effective_pre_execution_state,
                        post_account_state,
                        planned_orders,
                        receipts,
                    )
                    self.state_store.save_order_state_transitions(audit_id, order_state_transitions)
                    self.state_store.save_position_state_transitions(audit_id, position_state_transitions)
                symbol_journal_entries = self._build_symbol_journal_entries(
                    audit_log_id=audit_id,
                    intent=intent,
                    market_snapshots=market_snapshots,
                    trigger_symbols=trigger_symbols,
                    risk_trigger_symbols=risk_trigger_symbols,
                    risk_reason=reason,
                    risk_mode=risk_mode,
                    risk_approved=is_approved,
                    estimated_pnl_pct=estimated_pnl_pct,
                    ledger_entries=ledger_entries,
                )
                self.state_store.save_symbol_journal_entries(symbol_journal_entries)
            self.state_store.save_journal(
                intent.decision,
                intent.rationale,
                intent.confidence,
                audit_log_id=audit_id,
                risk_reason=reason,
                estimated_pnl_pct=estimated_pnl_pct,
                loss_signal=loss_signal,
                rationale_items=intent.rationale_items,
            )
            final_account_state = post_account_state or effective_pre_execution_state or account_state
            final_unknown_order_count = self.state_store.count_recent_unknown_orders(
                minutes=execution_guards["unknown_order_block_minutes"]
            )
            return {
                "loop_type": loop_type,
                "trigger_active": is_trigger_mode,
                "has_positions": self._has_live_positions(final_account_state),
                "has_open_orders": self._has_live_open_orders(final_account_state),
                "has_unknown_orders": final_unknown_order_count > 0,
                "executed_order_count": execution_summary.get("executed_order_count", 0),
                "position_closed": self._position_closed_during_cycle(
                    effective_pre_execution_state,
                    post_account_state,
                ),
                "final_account_state": final_account_state,
            }
                
        except Exception as e:
            log.error(f"Error in {loop_type} cycle: {e}")
            self.state_store.save_audit(
                account={},
                market={},
                intent={
                    "decision": "HOLD",
                    "orders": [],
                    "rationale": [f"Loop error during {loop_type}."],
                    "risk_notes": ["System Error"],
                    "confidence": 0.0,
                },
                risk_ok=False,
                reason=f"Error in {loop_type} cycle: {e}",
                receipts=[],
                execution_summary={"loop_type": loop_type, "error": str(e)},
            )
            self.alert_manager.send(
                "Trading Loop Error",
                f"{loop_type} cycle failed: {e}",
                level="error",
            )
            return {
                "loop_type": loop_type,
                "trigger_active": self.active_loop_mode == "5m",
                "has_positions": True,
                "has_open_orders": True,
                "has_unknown_orders": True,
                "executed_order_count": 0,
                "position_closed": False,
                "final_account_state": None,
                "error": str(e),
            }
        finally:
            self.state_store.release_runtime_lock(loop_lock_name, owner=loop_lock_owner)

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
        log.info("Running daily memory updater batch...")
        try:
            # Reuse the current interpreter so the batch job sees the same virtualenv.
            subprocess.run([sys.executable, "batch/memory_updater.py"], check=True)
            log.info("Daily batch finished successfully.")
        except Exception as e:
            log.error(f"Daily batch failed: {e}")

    def start_scheduler(self):
        execution_guards = self._build_execution_guards()
        log.info(
            "Adaptive scheduler started. Cadence minutes: 1h={} 15m={} 5m={} snapshot={}m recovery={}m",
            self.adaptive_loop_config["base_loop_minutes"],
            self.adaptive_loop_config["position_loop_minutes"],
            self.adaptive_loop_config["trigger_loop_minutes"],
            self.snapshot_service.materialization_minutes,
            execution_guards.get("recovery_sweep_minutes", 5),
        )
        next_snapshot_at = time.time()
        next_cycle_at = time.time()
        while True:
            now_epoch = time.time()
            self._run_due_maintenance_tasks(now_epoch)
            if now_epoch >= next_snapshot_at:
                self.snapshot_service.materialize(now_epoch=now_epoch)
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
                active_mode = self._apply_adaptive_loop_mode(desired_mode, cycle_finished_at)
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

if __name__ == "__main__":
    orchestrator = TradingOrchestrator()
    orchestrator.start_scheduler()
