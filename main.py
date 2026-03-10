from dotenv import load_dotenv

from orchestrator.bootstrap import RuntimeConfig, StartupConfigurationError, TradingBootstrapMixin
from orchestrator.context_service import TradingContextServiceMixin
from orchestrator.cycle_runner import TradingCycleRunnerMixin
from orchestrator.runtime_support import TradingRuntimeSupportMixin
from orchestrator.trade_case_service import TradingTradeCaseServiceMixin
from utils.logger import log

load_dotenv()


class TradingOrchestrator(
    TradingBootstrapMixin,
    TradingRuntimeSupportMixin,
    TradingContextServiceMixin,
    TradingTradeCaseServiceMixin,
    TradingCycleRunnerMixin,
):
    ALERT_STATE_UNSET = object()
    DEFAULT_EXECUTION_GUARDS = {
        "loop_lock_ttl_seconds": 240,
        "decision_cooldown_minutes": 10,
        "entry_mutex_ttl_seconds": 180,
        "unknown_order_block_minutes": 30,
        "recovery_sweep_minutes": 5,
        "symbol_reentry_close_cooldown_minutes": 10,
        "symbol_reentry_loss_cooldown_minutes": 30,
        "symbol_reentry_loss_threshold_pct": 0.0,
    }
    DEFAULT_CONTEXT_BUDGET = {
        "journal_fetch_limit": 24,
        "journal_anchor_limit": 24,
        "journal_keep_limit": 6,
        "memory_keep_limit": 8,
        "similar_case_keep_limit": 5,
        "similar_case_normal_keep_limit": 2,
        "max_rationale_items_per_journal": 2,
        "max_evidence_per_rationale_item": 3,
        "max_ignore_when_per_memory": 2,
    }
    DEFAULT_REPLAY_BATCH = {
        "enabled": True,
        "window_hours": 24,
        "limit": 30,
        "max_limit": 60,
        "coverage_target": 0.5,
        "hold_quota": 8,
        "candidate_pool_limit": 240,
        "save": True,
    }
    DEFAULT_DAILY_BATCH = {
        "enabled": True,
        "run_hour_local": 4,
        "run_minute_local": 0,
        "replay_timeout_seconds": 1800,
        "memory_timeout_seconds": 1800,
        "deepseek_memory_timeout_seconds": 5400,
    }
    DEFAULT_ADAPTIVE_LOOP = {
        "base_loop_minutes": 60,
        "position_loop_minutes": 15,
        "trigger_loop_minutes": 5,
        "trigger_clear_cycles": 2,
        "position_min_duration_minutes": 30,
        "flat_clear_cycles": 4,
        "recent_execution_window_minutes": 20,
        "recent_close_window_minutes": 20,
        "poll_seconds": 1,
        "trade_case_snapshot_poll_seconds": 5,
    }
    DEFAULT_PRIVATE_ACCOUNT_WS = {
        "reconcile_ttl_ms": 600000,
        "ws_stale_after_seconds": 35,
        "ws_recover_stability_seconds": 12,
        "forced_reconcile_on_recover": True,
        "execution_cache_size": 200,
    }
    DEFAULT_PUBLIC_MARKET_WS = {
        "orderbook_depth": 50,
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


def main() -> int:
    try:
        orchestrator = TradingOrchestrator()
    except StartupConfigurationError as exc:
        log.error("Startup failed: {}", exc)
        return 1
    orchestrator.start_scheduler()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
