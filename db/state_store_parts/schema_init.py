
import hashlib
import json
import math
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStoreSchemaInitMixin:
    def __init__(self, db_path: str = "data/gpt_trader.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_db()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _init_db(self):
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    account_state JSON,
                    market_state JSON,
                    market_vectors JSON,
                    intent JSON,
                    risk_approved BOOLEAN,
                    risk_reason TEXT,
                    receipts JSON,
                    policy_snapshot TEXT,
                    decision_input_hash TEXT,
                    risk_mode TEXT,
                    risk_profile JSON,
                    decision_request JSON,
                    ai_raw_response TEXT,
                    execution_summary JSON
                );
                
                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    audit_log_id INTEGER,
                    decision TEXT,
                    rationale TEXT,
                    rationale_json JSON,
                    confidence REAL,
                    risk_reason TEXT,
                    estimated_pnl_pct REAL,
                    loss_signal BOOLEAN DEFAULT 0,
                    trade_case_ids JSON,
                    case_statuses JSON,
                    case_summary TEXT
                );
                
                CREATE TABLE IF NOT EXISTS long_term_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    rule TEXT,
                    version INTEGER,
                    metrics JSON
                );

                CREATE TABLE IF NOT EXISTS audit_fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_log_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    fill_timestamp DATETIME,
                    symbol TEXT,
                    order_id TEXT,
                    client_order_id TEXT,
                    trade_id TEXT,
                    side TEXT,
                    price REAL,
                    amount REAL,
                    cost REAL,
                    fee_cost REAL,
                    fee_currency TEXT,
                    realized_pnl REAL,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS position_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_log_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    symbol TEXT NOT NULL,
                    decision TEXT,
                    fill_count INTEGER DEFAULT 0,
                    total_amount REAL DEFAULT 0,
                    gross_realized_pnl REAL DEFAULT 0,
                    fee_cost REAL DEFAULT 0,
                    net_realized_pnl REAL DEFAULT 0,
                    balance_delta REAL,
                    balance_delta_pct REAL,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS symbol_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    audit_log_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    decision TEXT,
                    risk_reason TEXT,
                    risk_mode TEXT,
                    risk_approved BOOLEAN,
                    triggered BOOLEAN DEFAULT 0,
                    risk_triggered BOOLEAN DEFAULT 0,
                    net_realized_pnl REAL,
                    fee_cost REAL,
                    estimated_pnl_pct REAL,
                    trade_case_id TEXT,
                    case_status TEXT,
                    case_opened_at DATETIME,
                    case_closed_at DATETIME,
                    holding_minutes REAL,
                    evidence_signals JSON,
                    summary TEXT,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS active_trade_cases (
                    symbol TEXT PRIMARY KEY,
                    trade_case_id TEXT NOT NULL,
                    opened_at DATETIME,
                    entry_decision TEXT,
                    last_decision TEXT,
                    last_audit_log_id INTEGER,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS trade_cases (
                    trade_case_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    risk_mode TEXT,
                    opened_at DATETIME,
                    closed_at DATETIME,
                    entry_audit_log_id INTEGER,
                    exit_audit_log_id INTEGER,
                    entry_decision TEXT,
                    exit_decision TEXT,
                    entry_side TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    estimated_pnl_pct REAL,
                    realized_pnl REAL,
                    fee_cost REAL,
                    holding_minutes REAL,
                    observed_mfe_pct REAL,
                    observed_mae_pct REAL,
                    slippage_cost REAL,
                    slippage_bps REAL,
                    thesis_failure_reason TEXT,
                    evidence_signals JSON,
                    trigger_reasons JSON,
                    risk_trigger_reasons JSON,
                    feature_vector JSON,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS trade_case_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_case_id TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    audit_log_id INTEGER,
                    symbol TEXT NOT NULL,
                    current_price REAL,
                    mark_price REAL,
                    mid_price REAL,
                    spread_bps REAL,
                    volume_ratio_15m REAL,
                    unrealized_pnl_pct REAL,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS market_snapshot_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    symbol TEXT NOT NULL,
                    current_price REAL,
                    mark_price REAL,
                    mid_price REAL,
                    spread_bps REAL,
                    spread_percentile_1h REAL,
                    depth_percentile_1h REAL,
                    depth_notional_top5 REAL,
                    spread_vs_1h_avg REAL,
                    volume_ratio_15m REAL,
                    distance_to_invalid_level REAL,
                    trigger_active BOOLEAN DEFAULT 0,
                    risk_trigger_active BOOLEAN DEFAULT 0,
                    volume_surge_trigger_active BOOLEAN DEFAULT 0,
                    fast_move_trigger_active BOOLEAN DEFAULT 0,
                    range_break_trigger_active BOOLEAN DEFAULT 0,
                    trend_1h TEXT,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS trade_case_vector_index (
                    trade_case_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    status TEXT,
                    risk_mode TEXT,
                    opened_at DATETIME,
                    closed_at DATETIME,
                    estimated_pnl_pct REAL,
                    realized_pnl REAL,
                    slippage_bps REAL,
                    thesis_failure_reason TEXT,
                    vector_norm REAL,
                    feature_vector JSON,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS trade_case_vector_bands (
                    trade_case_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    band_index INTEGER NOT NULL,
                    band_hash TEXT NOT NULL,
                    closed_at DATETIME,
                    PRIMARY KEY (trade_case_id, band_index)
                );

                CREATE TABLE IF NOT EXISTS retrieval_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_log_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    context_type TEXT NOT NULL,
                    rank_index INTEGER DEFAULT 0,
                    trade_case_id TEXT,
                    case_symbol TEXT,
                    matched_symbol TEXT,
                    decision TEXT,
                    decision_symbol_aligned BOOLEAN DEFAULT 0,
                    estimated_pnl_pct REAL,
                    similarity REAL,
                    final_score REAL,
                    retrieval_source TEXT,
                    matched_band_count INTEGER DEFAULT 0,
                    risk_mode TEXT,
                    outcome_trade_case_id TEXT,
                    outcome_status TEXT,
                    outcome_realized_pnl REAL,
                    outcome_estimated_pnl_pct REAL,
                    outcome_holding_minutes REAL,
                    outcome_observed_mfe_pct REAL,
                    outcome_observed_mae_pct REAL,
                    outcome_slippage_bps REAL,
                    outcome_updated_at DATETIME,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS replay_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_log_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    original_decision TEXT,
                    replayed_decision TEXT,
                    decision_changed BOOLEAN DEFAULT 0,
                    order_count_changed BOOLEAN DEFAULT 0,
                    side_changed BOOLEAN DEFAULT 0,
                    order_type_changed BOOLEAN DEFAULT 0,
                    execution_profile_changed BOOLEAN DEFAULT 0,
                    size_delta REAL DEFAULT 0,
                    confidence_delta REAL DEFAULT 0,
                    simulated_fill_ratio REAL,
                    simulated_slippage_bps REAL,
                    simulated_latency_seconds REAL,
                    rationale_keywords_added JSON,
                    rationale_keywords_removed JSON,
                    evidence_signals_added JSON,
                    evidence_signals_removed JSON,
                    evidence_signal_count_delta INTEGER DEFAULT 0,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS execution_policy_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_log_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    symbol TEXT NOT NULL,
                    loop_mode TEXT,
                    risk_mode TEXT,
                    action_type TEXT,
                    urgency_bucket TEXT,
                    liquidity_bucket TEXT,
                    decision TEXT,
                    order_action TEXT,
                    order_type TEXT,
                    execution_profile TEXT,
                    reduce_only BOOLEAN DEFAULT 0,
                    trigger_mode BOOLEAN DEFAULT 0,
                    spread_bps REAL,
                    spread_percentile_1h REAL,
                    depth_percentile_1h REAL,
                    reference_price REAL,
                    order_price REAL,
                    average_fill_price REAL,
                    requested_quantity REAL,
                    filled_quantity REAL,
                    fill_ratio REAL,
                    slippage_bps REAL,
                    latency_seconds REAL,
                    status TEXT,
                    outcome_trade_case_id TEXT,
                    outcome_estimated_pnl_pct REAL,
                    outcome_realized_pnl REAL,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS execution_policy_profiles (
                    context_key TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    loop_mode TEXT,
                    risk_mode TEXT,
                    action_type TEXT,
                    urgency_bucket TEXT,
                    liquidity_bucket TEXT,
                    recommended_order_type TEXT,
                    recommended_execution_profile TEXT,
                    metrics JSON,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS policy_registry (
                    policy_key TEXT PRIMARY KEY,
                    domain TEXT NOT NULL,
                    policy_type TEXT NOT NULL,
                    regime_key TEXT,
                    active_candidate_policy_id INTEGER,
                    version INTEGER DEFAULT 1,
                    config JSON,
                    metrics JSON,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS candidate_policy_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    policy_type TEXT NOT NULL,
                    regime_key TEXT,
                    policy_name TEXT,
                    source TEXT,
                    status TEXT DEFAULT 'candidate',
                    rule_text TEXT,
                    config JSON,
                    metrics JSON,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    evaluated_at DATETIME,
                    promoted_at DATETIME,
                    retired_at DATETIME
                );

                CREATE TABLE IF NOT EXISTS policy_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_policy_id INTEGER NOT NULL,
                    evaluation_scope TEXT NOT NULL,
                    sample_count INTEGER DEFAULT 0,
                    score REAL DEFAULT 0,
                    metrics JSON,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS policy_promotions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    domain TEXT NOT NULL,
                    regime_key TEXT,
                    previous_candidate_policy_id INTEGER,
                    new_candidate_policy_id INTEGER NOT NULL,
                    reason TEXT,
                    metrics JSON,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS policy_bundles (
                    bundle_key TEXT PRIMARY KEY,
                    regime_key TEXT,
                    status TEXT DEFAULT 'candidate',
                    alpha_policy_key TEXT,
                    position_policy_key TEXT,
                    safety_policy_key TEXT,
                    execution_policy_key TEXT,
                    metrics JSON,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS bundle_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bundle_key TEXT NOT NULL,
                    evaluation_scope TEXT NOT NULL,
                    sample_count INTEGER DEFAULT 0,
                    score REAL DEFAULT 0,
                    metrics JSON,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS bundle_promotions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    regime_key TEXT,
                    previous_bundle_key TEXT,
                    new_bundle_key TEXT NOT NULL,
                    reason TEXT,
                    metrics JSON,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS shadow_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_log_id INTEGER NOT NULL,
                    candidate_policy_id INTEGER NOT NULL,
                    domain TEXT NOT NULL,
                    symbol TEXT,
                    loop_mode TEXT,
                    trade_case_id TEXT,
                    divergence_type TEXT,
                    outcome_status TEXT,
                    outcome_estimated_pnl_pct REAL,
                    payload JSON,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS learning_artifacts (
                    artifact_key TEXT PRIMARY KEY,
                    artifact_type TEXT,
                    payload JSON,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS order_state_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_log_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    symbol TEXT,
                    client_order_id TEXT,
                    exchange_order_id TEXT,
                    replace_group_id TEXT,
                    lineage_role TEXT,
                    lineage_parent_order_id TEXT,
                    lineage_parent_client_order_id TEXT,
                    from_state TEXT,
                    to_state TEXT,
                    event_type TEXT,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS position_state_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_log_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    symbol TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    event_type TEXT,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS trigger_state (
                    symbol TEXT PRIMARY KEY,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    payload JSON
                );

                CREATE TABLE IF NOT EXISTS decision_cache (
                    prompt_hash TEXT PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    input_hash TEXT,
                    raw_response TEXT NOT NULL,
                    intent JSON NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_used_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    hit_count INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS runtime_locks (
                    lock_name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    acquired_at_epoch REAL NOT NULL,
                    expires_at_epoch REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entry_mutex (
                    symbol TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    acquired_at_epoch REAL NOT NULL,
                    expires_at_epoch REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS decision_fingerprints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    symbol TEXT NOT NULL,
                    action TEXT NOT NULL,
                    size_band TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    audit_log_id INTEGER,
                    status TEXT
                );

                CREATE TABLE IF NOT EXISTS order_runtime_state (
                    order_key TEXT PRIMARY KEY,
                    client_order_id TEXT,
                    exchange_order_id TEXT,
                    symbol TEXT,
                    replace_group_id TEXT,
                    lineage_role TEXT,
                    lineage_parent_order_id TEXT,
                    lineage_parent_client_order_id TEXT,
                    state TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    last_audit_log_id INTEGER,
                    payload JSON
                );

                CREATE INDEX IF NOT EXISTS idx_symbol_journal_symbol_id
                ON symbol_journal(symbol, id);

                CREATE INDEX IF NOT EXISTS idx_symbol_journal_timestamp_id
                ON symbol_journal(timestamp, id);

                CREATE INDEX IF NOT EXISTS idx_symbol_journal_audit_log_id
                ON symbol_journal(audit_log_id);

                CREATE INDEX IF NOT EXISTS idx_active_trade_cases_trade_case_id
                ON active_trade_cases(trade_case_id);

                CREATE INDEX IF NOT EXISTS idx_trade_cases_symbol_closed_at
                ON trade_cases(symbol, closed_at);

                CREATE INDEX IF NOT EXISTS idx_trade_cases_status_closed_at
                ON trade_cases(status, closed_at);

                CREATE INDEX IF NOT EXISTS idx_trade_case_snapshots_case_time
                ON trade_case_snapshots(trade_case_id, timestamp);

                CREATE INDEX IF NOT EXISTS idx_market_snapshot_history_symbol_time
                ON market_snapshot_history(symbol, timestamp, id);

                CREATE INDEX IF NOT EXISTS idx_trade_case_vector_symbol_closed_at
                ON trade_case_vector_index(symbol, closed_at);

                CREATE INDEX IF NOT EXISTS idx_trade_case_vector_status_closed_at
                ON trade_case_vector_index(status, closed_at);

                CREATE INDEX IF NOT EXISTS idx_trade_case_vector_bands_lookup
                ON trade_case_vector_bands(symbol, band_index, band_hash, closed_at);

                CREATE INDEX IF NOT EXISTS idx_trade_case_vector_bands_hash_lookup
                ON trade_case_vector_bands(band_index, band_hash, closed_at);

                CREATE INDEX IF NOT EXISTS idx_retrieval_evaluations_audit
                ON retrieval_evaluations(audit_log_id, id);

                CREATE INDEX IF NOT EXISTS idx_retrieval_evaluations_context_created
                ON retrieval_evaluations(context_type, created_at, id);

                CREATE INDEX IF NOT EXISTS idx_retrieval_evaluations_trade_case
                ON retrieval_evaluations(trade_case_id, created_at, id);

                CREATE INDEX IF NOT EXISTS idx_execution_policy_eval_context
                ON execution_policy_evaluations(symbol, loop_mode, action_type, urgency_bucket, liquidity_bucket, id);

                CREATE INDEX IF NOT EXISTS idx_execution_policy_profiles_lookup
                ON execution_policy_profiles(symbol, loop_mode, action_type, urgency_bucket, liquidity_bucket, updated_at);

                CREATE INDEX IF NOT EXISTS idx_candidate_policy_domain_status
                ON candidate_policy_versions(domain, status, created_at);

                CREATE INDEX IF NOT EXISTS idx_policy_registry_domain_regime
                ON policy_registry(domain, regime_key, updated_at);

                CREATE INDEX IF NOT EXISTS idx_candidate_policy_domain_regime_status
                ON candidate_policy_versions(domain, regime_key, status, created_at);

                CREATE INDEX IF NOT EXISTS idx_policy_evaluations_candidate
                ON policy_evaluations(candidate_policy_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_shadow_decisions_candidate
                ON shadow_decisions(candidate_policy_id, created_at);

                CREATE INDEX IF NOT EXISTS idx_policy_bundles_regime_status
                ON policy_bundles(regime_key, status, updated_at);

                CREATE INDEX IF NOT EXISTS idx_bundle_evaluations_bundle
                ON bundle_evaluations(bundle_key, created_at);

                CREATE INDEX IF NOT EXISTS idx_decision_fingerprint_lookup
                ON decision_fingerprints(symbol, action, size_band, id);

                CREATE INDEX IF NOT EXISTS idx_order_runtime_state_state_updated
                ON order_runtime_state(state, updated_at);
            """)
        self._ensure_column("audit_logs", "policy_snapshot", "TEXT")
        self._ensure_column("audit_logs", "decision_input_hash", "TEXT")
        self._ensure_column("audit_logs", "risk_mode", "TEXT")
        self._ensure_column("audit_logs", "risk_profile", "JSON")
        self._ensure_column("audit_logs", "decision_request", "JSON")
        self._ensure_column("audit_logs", "ai_raw_response", "TEXT")
        self._ensure_column("audit_logs", "execution_summary", "JSON")
        self._ensure_column("audit_logs", "market_vectors", "JSON")
        self._ensure_column("policy_bundles", "safety_policy_key", "TEXT")
        self._ensure_column("journal", "audit_log_id", "INTEGER")
        self._ensure_column("journal", "rationale_json", "JSON")
        self._ensure_column("journal", "risk_reason", "TEXT")
        self._ensure_column("journal", "estimated_pnl_pct", "REAL")
        self._ensure_column("journal", "loss_signal", "BOOLEAN DEFAULT 0")
        self._ensure_column("journal", "trade_case_ids", "JSON")
        self._ensure_column("journal", "case_statuses", "JSON")
        self._ensure_column("journal", "case_summary", "TEXT")
        self._ensure_column("symbol_journal", "trade_case_id", "TEXT")
        self._ensure_column("symbol_journal", "case_status", "TEXT")
        self._ensure_column("symbol_journal", "case_opened_at", "DATETIME")
        self._ensure_column("symbol_journal", "case_closed_at", "DATETIME")
        self._ensure_column("symbol_journal", "holding_minutes", "REAL")
        self._ensure_column("trade_cases", "risk_mode", "TEXT")
        self._ensure_column("trade_cases", "opened_at", "DATETIME")
        self._ensure_column("trade_cases", "closed_at", "DATETIME")
        self._ensure_column("trade_cases", "entry_audit_log_id", "INTEGER")
        self._ensure_column("trade_cases", "exit_audit_log_id", "INTEGER")
        self._ensure_column("trade_cases", "entry_decision", "TEXT")
        self._ensure_column("trade_cases", "exit_decision", "TEXT")
        self._ensure_column("trade_cases", "entry_side", "TEXT")
        self._ensure_column("trade_cases", "entry_price", "REAL")
        self._ensure_column("trade_cases", "exit_price", "REAL")
        self._ensure_column("trade_cases", "estimated_pnl_pct", "REAL")
        self._ensure_column("trade_cases", "realized_pnl", "REAL")
        self._ensure_column("trade_cases", "fee_cost", "REAL")
        self._ensure_column("trade_cases", "holding_minutes", "REAL")
        self._ensure_column("trade_cases", "observed_mfe_pct", "REAL")
        self._ensure_column("trade_cases", "observed_mae_pct", "REAL")
        self._ensure_column("trade_cases", "slippage_cost", "REAL")
        self._ensure_column("trade_cases", "slippage_bps", "REAL")
        self._ensure_column("trade_cases", "thesis_failure_reason", "TEXT")
        self._ensure_column("trade_cases", "thesis_id", "TEXT")
        self._ensure_column("trade_cases", "management_template_id", "TEXT")
        self._ensure_column("trade_cases", "exit_reason", "TEXT")
        self._ensure_column("trade_cases", "evidence_signals", "JSON")
        self._ensure_column("trade_cases", "trigger_reasons", "JSON")
        self._ensure_column("trade_cases", "risk_trigger_reasons", "JSON")
        self._ensure_column("trade_cases", "feature_vector", "JSON")
        self._ensure_column("trade_cases", "payload", "JSON")
        self._ensure_column("active_trade_cases", "thesis_id", "TEXT")
        self._ensure_column("active_trade_cases", "management_template_id", "TEXT")
        self._ensure_column("trade_case_vector_index", "status", "TEXT")
        self._ensure_column("trade_case_vector_index", "risk_mode", "TEXT")
        self._ensure_column("trade_case_vector_index", "opened_at", "DATETIME")
        self._ensure_column("trade_case_vector_index", "closed_at", "DATETIME")
        self._ensure_column("trade_case_vector_index", "estimated_pnl_pct", "REAL")
        self._ensure_column("trade_case_vector_index", "realized_pnl", "REAL")
        self._ensure_column("trade_case_vector_index", "slippage_bps", "REAL")
        self._ensure_column("trade_case_vector_index", "thesis_failure_reason", "TEXT")
        self._ensure_column("trade_case_vector_index", "vector_norm", "REAL")
        self._ensure_column("trade_case_vector_index", "feature_vector", "JSON")
        self._ensure_column("trade_case_vector_index", "payload", "JSON")
        self._ensure_column("retrieval_evaluations", "outcome_trade_case_id", "TEXT")
        self._ensure_column("retrieval_evaluations", "outcome_status", "TEXT")
        self._ensure_column("retrieval_evaluations", "outcome_realized_pnl", "REAL")
        self._ensure_column("retrieval_evaluations", "outcome_estimated_pnl_pct", "REAL")
        self._ensure_column("retrieval_evaluations", "outcome_holding_minutes", "REAL")
        self._ensure_column("retrieval_evaluations", "outcome_observed_mfe_pct", "REAL")
        self._ensure_column("retrieval_evaluations", "outcome_observed_mae_pct", "REAL")
        self._ensure_column("retrieval_evaluations", "outcome_slippage_bps", "REAL")
        self._ensure_column("retrieval_evaluations", "outcome_updated_at", "DATETIME")
        self._ensure_column("replay_runs", "side_changed", "BOOLEAN DEFAULT 0")
        self._ensure_column("replay_runs", "order_type_changed", "BOOLEAN DEFAULT 0")
        self._ensure_column("replay_runs", "execution_profile_changed", "BOOLEAN DEFAULT 0")
        self._ensure_column("replay_runs", "size_delta", "REAL DEFAULT 0")
        self._ensure_column("replay_runs", "simulated_fill_ratio", "REAL")
        self._ensure_column("replay_runs", "simulated_slippage_bps", "REAL")
        self._ensure_column("replay_runs", "simulated_latency_seconds", "REAL")
        self._ensure_column("replay_runs", "rationale_keywords_added", "JSON")
        self._ensure_column("replay_runs", "rationale_keywords_removed", "JSON")
        self._ensure_column("replay_runs", "evidence_signals_added", "JSON")
        self._ensure_column("replay_runs", "evidence_signals_removed", "JSON")
        self._ensure_column("replay_runs", "evidence_signal_count_delta", "INTEGER DEFAULT 0")
        self._ensure_column("policy_registry", "regime_key", "TEXT")
        self._ensure_column("candidate_policy_versions", "regime_key", "TEXT")
        self._ensure_column("policy_promotions", "regime_key", "TEXT")
        self._ensure_column("order_state_transitions", "replace_group_id", "TEXT")
        self._ensure_column("order_state_transitions", "lineage_role", "TEXT")
        self._ensure_column("order_state_transitions", "lineage_parent_order_id", "TEXT")
        self._ensure_column("order_state_transitions", "lineage_parent_client_order_id", "TEXT")
        self._ensure_column("order_runtime_state", "replace_group_id", "TEXT")
        self._ensure_column("order_runtime_state", "lineage_role", "TEXT")
        self._ensure_column("order_runtime_state", "lineage_parent_order_id", "TEXT")
        self._ensure_column("order_runtime_state", "lineage_parent_client_order_id", "TEXT")
        self._ensure_column("market_snapshot_history", "depth_notional_top5", "REAL")
        self._ensure_column("market_snapshot_history", "spread_vs_1h_avg", "REAL")
        self._ensure_column("market_snapshot_history", "volume_surge_trigger_active", "BOOLEAN DEFAULT 0")
        self._ensure_column("market_snapshot_history", "fast_move_trigger_active", "BOOLEAN DEFAULT 0")
        self._ensure_column("market_snapshot_history", "range_break_trigger_active", "BOOLEAN DEFAULT 0")
        self._ensure_column("market_snapshot_history", "trend_1h", "TEXT")

