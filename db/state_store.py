import hashlib
import json
import math
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger

class StateStore:
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

    def _ensure_column(self, table_name: str, column_name: str, column_type: str):
        cursor = self.conn.execute(f"PRAGMA table_info({table_name})")
        columns = {row["name"] for row in cursor.fetchall()}
        if column_name not in columns:
            with self.conn:
                self.conn.execute(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                )

    def save_audit(
        self,
        account: dict,
        market: dict,
        intent: dict,
        risk_ok: bool,
        reason: str,
        receipts: list,
        policy_snapshot: Optional[str] = None,
        decision_input_hash: Optional[str] = None,
        risk_mode: Optional[str] = None,
        risk_profile: Optional[dict] = None,
        decision_request: Optional[dict] = None,
        ai_raw_response: Optional[str] = None,
        execution_summary: Optional[dict] = None,
        market_vectors: Optional[dict] = None,
    ) -> Optional[int]:
        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    INSERT INTO audit_logs (
                        account_state,
                        market_state,
                        market_vectors,
                        intent,
                        risk_approved,
                        risk_reason,
                        receipts,
                        policy_snapshot,
                        decision_input_hash,
                        risk_mode,
                        risk_profile,
                        decision_request,
                        ai_raw_response,
                        execution_summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        json.dumps(account),
                        json.dumps(market),
                        json.dumps(market_vectors) if market_vectors is not None else None,
                        json.dumps(intent),
                        risk_ok,
                        reason,
                        json.dumps(receipts),
                        policy_snapshot,
                        decision_input_hash,
                        risk_mode,
                        json.dumps(risk_profile) if risk_profile is not None else None,
                        json.dumps(decision_request) if decision_request is not None else None,
                        ai_raw_response,
                        json.dumps(execution_summary) if execution_summary is not None else None,
                    ),
                )
                return int(cursor.lastrowid)
        except Exception as e:
            logger.error(f"Failed to save audit log: {e}")
            return None

    def save_journal(
        self,
        decision: str,
        rationale: List[str],
        confidence: float,
        audit_log_id: Optional[int] = None,
        rationale_items: Optional[List[Dict[str, Any]]] = None,
        risk_reason: str = "",
        estimated_pnl_pct: Optional[float] = None,
        loss_signal: bool = False,
        trade_case_ids: Optional[List[str]] = None,
        case_statuses: Optional[Dict[str, str]] = None,
        case_summary: str = "",
    ):
        try:
            serialized_rationale_items = self._serialize_json_payload(rationale_items)
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO journal (
                        audit_log_id,
                        decision,
                        rationale,
                        rationale_json,
                        confidence,
                        risk_reason,
                        estimated_pnl_pct,
                        loss_signal,
                        trade_case_ids,
                        case_statuses,
                        case_summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        audit_log_id,
                        decision,
                        "\\n".join(rationale),
                        json.dumps(serialized_rationale_items) if serialized_rationale_items is not None else None,
                        confidence,
                        risk_reason,
                        estimated_pnl_pct,
                        int(loss_signal),
                        json.dumps(trade_case_ids or []),
                        json.dumps(case_statuses or {}),
                        case_summary,
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to save journal log: {e}")

    def _serialize_json_payload(self, payload: Any) -> Any:
        if hasattr(payload, "model_dump"):
            return payload.model_dump(mode="json")
        if isinstance(payload, dict):
            return {str(key): self._serialize_json_payload(value) for key, value in payload.items()}
        if isinstance(payload, (list, tuple)):
            return [self._serialize_json_payload(item) for item in payload]
        return payload

    def save_retrieval_evaluations(self, audit_log_id: int, entries: List[Dict[str, Any]]):
        if not audit_log_id or not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO retrieval_evaluations (
                        audit_log_id,
                        context_type,
                        rank_index,
                        trade_case_id,
                        case_symbol,
                        matched_symbol,
                        decision,
                        decision_symbol_aligned,
                        estimated_pnl_pct,
                        similarity,
                        final_score,
                        retrieval_source,
                        matched_band_count,
                        risk_mode,
                        outcome_trade_case_id,
                        outcome_status,
                        outcome_realized_pnl,
                        outcome_estimated_pnl_pct,
                        outcome_holding_minutes,
                        outcome_observed_mfe_pct,
                        outcome_observed_mae_pct,
                        outcome_slippage_bps,
                        outcome_updated_at,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_log_id,
                            entry.get("context_type"),
                            entry.get("rank_index"),
                            entry.get("trade_case_id"),
                            entry.get("case_symbol"),
                            entry.get("matched_symbol"),
                            entry.get("decision"),
                            int(bool(entry.get("decision_symbol_aligned"))),
                            entry.get("estimated_pnl_pct"),
                            entry.get("similarity"),
                            entry.get("final_score"),
                            entry.get("retrieval_source"),
                            int(entry.get("matched_band_count") or 0),
                            entry.get("risk_mode"),
                            entry.get("outcome_trade_case_id"),
                            entry.get("outcome_status"),
                            entry.get("outcome_realized_pnl"),
                            entry.get("outcome_estimated_pnl_pct"),
                            entry.get("outcome_holding_minutes"),
                            entry.get("outcome_observed_mfe_pct"),
                            entry.get("outcome_observed_mae_pct"),
                            entry.get("outcome_slippage_bps"),
                            entry.get("outcome_updated_at"),
                            json.dumps(entry.get("payload") or {}),
                        )
                        for entry in entries
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save retrieval evaluations: {e}")

    def update_retrieval_evaluations_for_trade_case(
        self,
        trade_case_id: str,
        case_symbol: str,
        entry_audit_log_id: Optional[int] = None,
        outcome_status: Optional[str] = None,
        outcome_realized_pnl: Optional[float] = None,
        outcome_estimated_pnl_pct: Optional[float] = None,
        outcome_holding_minutes: Optional[float] = None,
        outcome_observed_mfe_pct: Optional[float] = None,
        outcome_observed_mae_pct: Optional[float] = None,
        outcome_slippage_bps: Optional[float] = None,
        outcome_updated_at: Optional[str] = None,
    ) -> None:
        if not trade_case_id or not case_symbol:
            return
        try:
            with self.conn:
                params: List[Any] = [
                    trade_case_id,
                    outcome_status,
                    outcome_realized_pnl,
                    outcome_estimated_pnl_pct,
                    outcome_holding_minutes,
                    outcome_observed_mfe_pct,
                    outcome_observed_mae_pct,
                    outcome_slippage_bps,
                    outcome_updated_at,
                    trade_case_id,
                ]
                query = """
                    UPDATE retrieval_evaluations
                    SET
                        outcome_trade_case_id = ?,
                        outcome_status = COALESCE(?, outcome_status),
                        outcome_realized_pnl = COALESCE(?, outcome_realized_pnl),
                        outcome_estimated_pnl_pct = COALESCE(?, outcome_estimated_pnl_pct),
                        outcome_holding_minutes = COALESCE(?, outcome_holding_minutes),
                        outcome_observed_mfe_pct = COALESCE(?, outcome_observed_mfe_pct),
                        outcome_observed_mae_pct = COALESCE(?, outcome_observed_mae_pct),
                        outcome_slippage_bps = COALESCE(?, outcome_slippage_bps),
                        outcome_updated_at = COALESCE(?, outcome_updated_at)
                    WHERE outcome_trade_case_id = ?
                """
                if entry_audit_log_id is not None:
                    query += " OR (audit_log_id = ? AND case_symbol = ?)"
                    params.extend([entry_audit_log_id, case_symbol])
                self.conn.execute(query, params)
        except Exception as e:
            logger.error(f"Failed to update retrieval evaluations for trade case: {e}")

    def get_recent_journals(self, limit: int = 10) -> List[str]:
        try:
            return [
                self._format_journal_summary(item)
                for item in self.get_recent_journal_context(limit=limit)
            ]
        except Exception as e:
            logger.error(f"Failed to fetch journals: {e}")
            return []

    def get_recent_journal_context(self, limit: int = 10) -> List[Dict[str, Any]]:
        query = """
            SELECT
                j.id,
                j.timestamp,
                j.audit_log_id,
                j.decision,
                j.rationale,
                j.rationale_json,
                j.confidence,
                j.risk_reason,
                j.estimated_pnl_pct,
                j.loss_signal,
                j.trade_case_ids,
                j.case_statuses,
                j.case_summary,
                COALESCE(ledger.net_realized_pnl, 0) AS ledger_net_realized_pnl,
                COALESCE(ledger.fee_cost, 0) AS ledger_fee_cost,
                a.risk_approved,
                a.risk_mode,
                a.market_state,
                a.execution_summary
            FROM journal j
            LEFT JOIN (
                SELECT
                    audit_log_id,
                    COALESCE(SUM(net_realized_pnl), 0) AS net_realized_pnl,
                    COALESCE(SUM(fee_cost), 0) AS fee_cost
                FROM position_ledger
                GROUP BY audit_log_id
            ) ledger ON ledger.audit_log_id = j.audit_log_id
            LEFT JOIN audit_logs a ON a.id = j.audit_log_id
            ORDER BY j.id DESC
            LIMIT ?
        """
        rows = []
        cursor = self.conn.execute(query, (limit,))
        for row in cursor.fetchall():
            rows.append(self._decode_journal_context_row(dict(row)))
        return rows

    def get_long_term_memory(self) -> List[Dict[str, Any]]:
        try:
            cursor = self.conn.execute(
                """
                SELECT id, timestamp, rule, version, metrics
                FROM long_term_memory
                ORDER BY id DESC
                LIMIT 200
                """
            )
            rows = [self._format_memory_row(dict(row)) for row in cursor.fetchall()]
            rows.sort(
                key=lambda row: (
                    1 if bool(row.get("retired")) else 0,
                    -float(row.get("confidence_score") or 0.0),
                    -float(row.get("validity_score") or 0.0),
                    -float(row.get("freshness_score") or 0.0),
                    -int(row.get("id") or 0),
                )
            )
            if not rows:
                return [
                    {
                        "rule": "No long term lessons recorded yet. Be conservative.",
                        "category": "risk_control",
                        "status": "rule",
                        "support": 0,
                        "regime": "general",
                        "retired": False,
                        "confidence_score": 0.0,
                        "validity_score": 0.0,
                        "freshness_score": 0.0,
                        "apply_when": {
                            "regime": ["general"],
                            "volatility": ["unknown"],
                            "trend": ["unknown"],
                        },
                        "ignore_when": ["No validated long-term evidence yet."],
                        "evidence_journal_ids": [],
                        "metrics": {},
                    }
                ]
            return rows
        except Exception as e:
            logger.error(f"Failed to fetch long term memory: {e}")
            return []

    def get_journal_entries_since(
        self,
        since: datetime,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT
                j.id,
                j.timestamp,
                j.audit_log_id,
                j.decision,
                j.rationale,
                j.rationale_json,
                j.confidence,
                j.risk_reason,
                j.estimated_pnl_pct,
                j.loss_signal,
                j.trade_case_ids,
                j.case_statuses,
                j.case_summary,
                COALESCE(ledger.net_realized_pnl, 0) AS ledger_net_realized_pnl,
                COALESCE(ledger.fee_cost, 0) AS ledger_fee_cost,
                a.risk_approved,
                a.risk_mode,
                a.market_state,
                a.execution_summary
            FROM journal j
            LEFT JOIN (
                SELECT
                    audit_log_id,
                    COALESCE(SUM(net_realized_pnl), 0) AS net_realized_pnl,
                    COALESCE(SUM(fee_cost), 0) AS fee_cost
                FROM position_ledger
                GROUP BY audit_log_id
            ) ledger ON ledger.audit_log_id = j.audit_log_id
            LEFT JOIN audit_logs a ON a.id = j.audit_log_id
            WHERE j.timestamp >= ?
            ORDER BY j.id ASC
        """
        params: List[Any] = [since.strftime("%Y-%m-%d %H:%M:%S")]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            rows.append(self._decode_journal_context_row(dict(row)))
        return rows

    def get_retrieval_evaluations_since(
        self,
        since: datetime,
        context_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT
                id,
                audit_log_id,
                created_at,
                context_type,
                rank_index,
                trade_case_id,
                case_symbol,
                matched_symbol,
                decision,
                decision_symbol_aligned,
                estimated_pnl_pct,
                similarity,
                final_score,
                retrieval_source,
                matched_band_count,
                risk_mode,
                outcome_trade_case_id,
                outcome_status,
                outcome_realized_pnl,
                outcome_estimated_pnl_pct,
                outcome_holding_minutes,
                outcome_observed_mfe_pct,
                outcome_observed_mae_pct,
                outcome_slippage_bps,
                outcome_updated_at,
                payload
            FROM retrieval_evaluations
            WHERE created_at >= ?
            ORDER BY created_at ASC, id ASC
        """
        params: List[Any] = [since.strftime("%Y-%m-%d %H:%M:%S")]
        if context_type:
            query = query.replace("WHERE created_at >= ?", "WHERE created_at >= ? AND context_type = ?")
            params.append(context_type)
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_replay_runs_since(
        self,
        since: datetime,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT
                id,
                audit_log_id,
                created_at,
                original_decision,
                replayed_decision,
                decision_changed,
                order_count_changed,
                side_changed,
                order_type_changed,
                execution_profile_changed,
                size_delta,
                confidence_delta,
                simulated_fill_ratio,
                simulated_slippage_bps,
                simulated_latency_seconds,
                rationale_keywords_added,
                rationale_keywords_removed,
                evidence_signals_added,
                evidence_signals_removed,
                evidence_signal_count_delta,
                payload
            FROM replay_runs
            WHERE created_at >= ?
            ORDER BY created_at ASC, id ASC
        """
        params: List[Any] = [since.strftime("%Y-%m-%d %H:%M:%S")]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["rationale_keywords_added"] = self._safe_json_loads(item.get("rationale_keywords_added"), default=[])
            item["rationale_keywords_removed"] = self._safe_json_loads(item.get("rationale_keywords_removed"), default=[])
            item["evidence_signals_added"] = self._safe_json_loads(item.get("evidence_signals_added"), default=[])
            item["evidence_signals_removed"] = self._safe_json_loads(item.get("evidence_signals_removed"), default=[])
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def save_long_term_memory_entries(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                for entry in entries:
                    if entry.get("id") is not None:
                        self.conn.execute(
                            """
                            UPDATE long_term_memory
                            SET timestamp = CURRENT_TIMESTAMP,
                                rule = ?,
                                version = ?,
                                metrics = ?
                            WHERE id = ?
                            """,
                            (
                                entry["rule"],
                                int(entry.get("version", 1)),
                                json.dumps(entry.get("metrics", {})),
                                int(entry["id"]),
                            ),
                        )
                    else:
                        self.conn.execute(
                            """
                            INSERT INTO long_term_memory (rule, version, metrics)
                            VALUES (?, ?, ?)
                            """,
                            (
                                entry["rule"],
                                int(entry.get("version", 1)),
                                json.dumps(entry.get("metrics", {})),
                            ),
                        )
        except Exception as e:
            logger.error(f"Failed to save long-term memory entries: {e}")

    def save_execution_fills(self, audit_log_id: int, fills: List[Dict[str, Any]]):
        if not fills:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO audit_fills (
                        audit_log_id,
                        fill_timestamp,
                        symbol,
                        order_id,
                        client_order_id,
                        trade_id,
                        side,
                        price,
                        amount,
                        cost,
                        fee_cost,
                        fee_currency,
                        realized_pnl,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_log_id,
                            fill.get("timestamp"),
                            fill.get("symbol"),
                            fill.get("order_id"),
                            fill.get("client_order_id"),
                            fill.get("trade_id"),
                            fill.get("side"),
                            fill.get("price"),
                            fill.get("amount"),
                            fill.get("cost"),
                            fill.get("fee_cost"),
                            fill.get("fee_currency"),
                            fill.get("realized_pnl"),
                            json.dumps(fill),
                        )
                        for fill in fills
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save execution fills: {e}")

    def save_position_ledger_entries(self, audit_log_id: int, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO position_ledger (
                        audit_log_id,
                        symbol,
                        decision,
                        fill_count,
                        total_amount,
                        gross_realized_pnl,
                        fee_cost,
                        net_realized_pnl,
                        balance_delta,
                        balance_delta_pct,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_log_id,
                            entry.get("symbol"),
                            entry.get("decision"),
                            entry.get("fill_count", 0),
                            entry.get("total_amount", 0.0),
                            entry.get("gross_realized_pnl", 0.0),
                            entry.get("fee_cost", 0.0),
                            entry.get("net_realized_pnl", 0.0),
                            entry.get("balance_delta"),
                            entry.get("balance_delta_pct"),
                            json.dumps(entry),
                        )
                        for entry in entries
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save position ledger entries: {e}")

    def save_symbol_journal_entries(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO symbol_journal (
                        audit_log_id,
                        symbol,
                        decision,
                        risk_reason,
                        risk_mode,
                        risk_approved,
                        triggered,
                        risk_triggered,
                        net_realized_pnl,
                        fee_cost,
                        estimated_pnl_pct,
                        trade_case_id,
                        case_status,
                        case_opened_at,
                        case_closed_at,
                        holding_minutes,
                        evidence_signals,
                        summary,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            int(entry.get("audit_log_id") or 0),
                            entry.get("symbol"),
                            entry.get("decision"),
                            entry.get("risk_reason"),
                            entry.get("risk_mode"),
                            int(bool(entry.get("risk_approved"))),
                            int(bool(entry.get("triggered"))),
                            int(bool(entry.get("risk_triggered"))),
                            entry.get("net_realized_pnl"),
                            entry.get("fee_cost"),
                            entry.get("estimated_pnl_pct"),
                            entry.get("trade_case_id"),
                            entry.get("case_status"),
                            entry.get("case_opened_at"),
                            entry.get("case_closed_at"),
                            entry.get("holding_minutes"),
                            json.dumps(entry.get("evidence_signals") or []),
                            entry.get("summary"),
                            json.dumps(entry.get("payload") or {}),
                        )
                        for entry in entries
                        if entry.get("symbol")
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save symbol journal entries: {e}")

    def get_recent_symbol_journal(self, limit: int = 50, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        query = """
            SELECT
                id,
                timestamp,
                audit_log_id,
                symbol,
                decision,
                risk_reason,
                risk_mode,
                risk_approved,
                triggered,
                risk_triggered,
                net_realized_pnl,
                fee_cost,
                estimated_pnl_pct,
                trade_case_id,
                case_status,
                case_opened_at,
                case_closed_at,
                holding_minutes,
                evidence_signals,
                summary,
                payload
            FROM symbol_journal
        """
        params: List[Any] = []
        if symbol:
            query += " WHERE symbol = ?"
            params.append(symbol)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            rows.append(self._decode_symbol_journal_row(dict(row)))
        return rows

    def get_symbol_journal_entries_since(
        self,
        since: datetime,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT
                id,
                timestamp,
                audit_log_id,
                symbol,
                decision,
                risk_reason,
                risk_mode,
                risk_approved,
                triggered,
                risk_triggered,
                net_realized_pnl,
                fee_cost,
                estimated_pnl_pct,
                trade_case_id,
                case_status,
                case_opened_at,
                case_closed_at,
                holding_minutes,
                evidence_signals,
                summary,
                payload
            FROM symbol_journal
            WHERE timestamp >= ?
            ORDER BY id ASC
        """
        params: List[Any] = [since.strftime("%Y-%m-%d %H:%M:%S")]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            rows.append(self._decode_symbol_journal_row(dict(row)))
        return rows

    def get_active_trade_case(self, symbol: str) -> Dict[str, Any]:
        if not symbol:
            return {}
        cursor = self.conn.execute(
            """
            SELECT
                symbol,
                trade_case_id,
                opened_at,
                entry_decision,
                thesis_id,
                management_template_id,
                last_decision,
                last_audit_log_id,
                payload
            FROM active_trade_cases
            WHERE symbol = ?
            """,
            (symbol,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = dict(row)
        item["payload"] = self._safe_json_loads(item.get("payload"), default={})
        return item

    def get_all_active_trade_cases(self) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                symbol,
                trade_case_id,
                opened_at,
                entry_decision,
                thesis_id,
                management_template_id,
                last_decision,
                last_audit_log_id,
                payload
            FROM active_trade_cases
            ORDER BY opened_at ASC, symbol ASC
            """
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def upsert_active_trade_case(
        self,
        symbol: str,
        trade_case_id: str,
        opened_at: Optional[str],
        entry_decision: Optional[str] = None,
        thesis_id: Optional[str] = None,
        management_template_id: Optional[str] = None,
        last_decision: Optional[str] = None,
        last_audit_log_id: Optional[int] = None,
        payload: Optional[Dict[str, Any]] = None,
    ):
        if not symbol or not trade_case_id:
            return
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO active_trade_cases (
                        symbol,
                        trade_case_id,
                        opened_at,
                        entry_decision,
                        thesis_id,
                        management_template_id,
                        last_decision,
                        last_audit_log_id,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        trade_case_id = excluded.trade_case_id,
                        opened_at = excluded.opened_at,
                        entry_decision = excluded.entry_decision,
                        thesis_id = excluded.thesis_id,
                        management_template_id = excluded.management_template_id,
                        last_decision = excluded.last_decision,
                        last_audit_log_id = excluded.last_audit_log_id,
                        payload = excluded.payload
                    """,
                    (
                        symbol,
                        trade_case_id,
                        opened_at,
                        entry_decision,
                        thesis_id,
                        management_template_id,
                        last_decision,
                        last_audit_log_id,
                        json.dumps(payload or {}),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to upsert active trade case: {e}")

    def close_active_trade_case(
        self,
        symbol: str,
        closed_at: Optional[str] = None,
        last_decision: Optional[str] = None,
        last_audit_log_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        existing = self.get_active_trade_case(symbol)
        if not existing:
            return {}
        payload = dict(existing.get("payload") or {})
        if closed_at:
            payload["closed_at"] = closed_at
        if last_decision:
            payload["last_decision"] = last_decision
        if last_audit_log_id is not None:
            payload["last_audit_log_id"] = last_audit_log_id
        existing["payload"] = payload
        try:
            with self.conn:
                self.conn.execute(
                    "DELETE FROM active_trade_cases WHERE symbol = ?",
                    (symbol,),
                )
        except Exception as e:
            logger.error(f"Failed to close active trade case: {e}")
        return existing

    def upsert_trade_case(self, record: Dict[str, Any]):
        trade_case_id = str(record.get("trade_case_id") or "").strip()
        symbol = str(record.get("symbol") or "").strip()
        status = str(record.get("status") or "open").strip() or "open"
        if not trade_case_id or not symbol:
            return
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO trade_cases (
                        trade_case_id,
                        symbol,
                        status,
                        risk_mode,
                        opened_at,
                        closed_at,
                        entry_audit_log_id,
                        exit_audit_log_id,
                        entry_decision,
                        exit_decision,
                        entry_side,
                        entry_price,
                        exit_price,
                        estimated_pnl_pct,
                        realized_pnl,
                        fee_cost,
                        holding_minutes,
                        observed_mfe_pct,
                        observed_mae_pct,
                        slippage_cost,
                        slippage_bps,
                        thesis_failure_reason,
                        thesis_id,
                        management_template_id,
                        exit_reason,
                        evidence_signals,
                        trigger_reasons,
                        risk_trigger_reasons,
                        feature_vector,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_case_id) DO UPDATE SET
                        symbol = excluded.symbol,
                        status = excluded.status,
                        risk_mode = excluded.risk_mode,
                        opened_at = excluded.opened_at,
                        closed_at = excluded.closed_at,
                        entry_audit_log_id = excluded.entry_audit_log_id,
                        exit_audit_log_id = excluded.exit_audit_log_id,
                        entry_decision = excluded.entry_decision,
                        exit_decision = excluded.exit_decision,
                        entry_side = excluded.entry_side,
                        entry_price = excluded.entry_price,
                        exit_price = excluded.exit_price,
                        estimated_pnl_pct = excluded.estimated_pnl_pct,
                        realized_pnl = excluded.realized_pnl,
                        fee_cost = excluded.fee_cost,
                        holding_minutes = excluded.holding_minutes,
                        observed_mfe_pct = excluded.observed_mfe_pct,
                        observed_mae_pct = excluded.observed_mae_pct,
                        slippage_cost = excluded.slippage_cost,
                        slippage_bps = excluded.slippage_bps,
                        thesis_failure_reason = excluded.thesis_failure_reason,
                        thesis_id = excluded.thesis_id,
                        management_template_id = excluded.management_template_id,
                        exit_reason = excluded.exit_reason,
                        evidence_signals = excluded.evidence_signals,
                        trigger_reasons = excluded.trigger_reasons,
                        risk_trigger_reasons = excluded.risk_trigger_reasons,
                        feature_vector = excluded.feature_vector,
                        payload = excluded.payload
                    """,
                    (
                        trade_case_id,
                        symbol,
                        status,
                        record.get("risk_mode"),
                        record.get("opened_at"),
                        record.get("closed_at"),
                        record.get("entry_audit_log_id"),
                        record.get("exit_audit_log_id"),
                        record.get("entry_decision"),
                        record.get("exit_decision"),
                        record.get("entry_side"),
                        record.get("entry_price"),
                        record.get("exit_price"),
                        record.get("estimated_pnl_pct"),
                        record.get("realized_pnl"),
                        record.get("fee_cost"),
                        record.get("holding_minutes"),
                        record.get("observed_mfe_pct"),
                        record.get("observed_mae_pct"),
                        record.get("slippage_cost"),
                        record.get("slippage_bps"),
                        record.get("thesis_failure_reason"),
                        record.get("thesis_id"),
                        record.get("management_template_id"),
                        record.get("exit_reason"),
                        json.dumps(record.get("evidence_signals") or []),
                        json.dumps(record.get("trigger_reasons") or []),
                        json.dumps(record.get("risk_trigger_reasons") or []),
                        json.dumps(record.get("feature_vector") or []),
                        json.dumps(record.get("payload") or {}),
                    ),
                )
                feature_vector = record.get("feature_vector") or []
                self.conn.execute(
                    """
                    INSERT INTO trade_case_vector_index (
                        trade_case_id,
                        symbol,
                        status,
                        risk_mode,
                        opened_at,
                        closed_at,
                        estimated_pnl_pct,
                        realized_pnl,
                        slippage_bps,
                        thesis_failure_reason,
                        vector_norm,
                        feature_vector,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trade_case_id) DO UPDATE SET
                        symbol = excluded.symbol,
                        status = excluded.status,
                        risk_mode = excluded.risk_mode,
                        opened_at = excluded.opened_at,
                        closed_at = excluded.closed_at,
                        estimated_pnl_pct = excluded.estimated_pnl_pct,
                        realized_pnl = excluded.realized_pnl,
                        slippage_bps = excluded.slippage_bps,
                        thesis_failure_reason = excluded.thesis_failure_reason,
                        vector_norm = excluded.vector_norm,
                        feature_vector = excluded.feature_vector,
                        payload = excluded.payload
                    """,
                    (
                        trade_case_id,
                        symbol,
                        status,
                        record.get("risk_mode"),
                        record.get("opened_at"),
                        record.get("closed_at"),
                        record.get("estimated_pnl_pct"),
                        record.get("realized_pnl"),
                        record.get("slippage_bps"),
                        record.get("thesis_failure_reason"),
                        self._vector_norm(feature_vector),
                        json.dumps(feature_vector),
                        json.dumps(
                            {
                                "trigger_reasons": record.get("trigger_reasons") or [],
                                "risk_trigger_reasons": record.get("risk_trigger_reasons") or [],
                                "evidence_signals": record.get("evidence_signals") or [],
                                "payload": record.get("payload") or {},
                            }
                        ),
                    ),
                )
                self.conn.execute(
                    "DELETE FROM trade_case_vector_bands WHERE trade_case_id = ?",
                    (trade_case_id,),
                )
                band_entries = self._build_trade_case_vector_band_entries(
                    trade_case_id=trade_case_id,
                    symbol=symbol,
                    feature_vector=feature_vector,
                    closed_at=record.get("closed_at"),
                )
                if band_entries:
                    self.conn.executemany(
                        """
                        INSERT INTO trade_case_vector_bands (
                            trade_case_id,
                            symbol,
                            band_index,
                            band_hash,
                            closed_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        band_entries,
                    )
        except Exception as e:
            logger.error(f"Failed to upsert trade case: {e}")

    def get_trade_case(self, trade_case_id: str) -> Dict[str, Any]:
        if not trade_case_id:
            return {}
        cursor = self.conn.execute(
            """
            SELECT *
            FROM trade_cases
            WHERE trade_case_id = ?
            LIMIT 1
            """,
            (trade_case_id,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        return self._decode_trade_case_row(dict(row))

    def get_recent_trade_cases(
        self,
        limit: int = 50,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT *
            FROM trade_cases
        """
        params: List[Any] = []
        clauses: List[str] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY COALESCE(closed_at, opened_at) DESC, trade_case_id DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(query, params)
        return [self._decode_trade_case_row(dict(row)) for row in cursor.fetchall()]

    def get_trade_cases_since(
        self,
        since: datetime,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT *
            FROM trade_cases
            WHERE COALESCE(closed_at, opened_at) >= ?
            ORDER BY COALESCE(closed_at, opened_at) ASC, trade_case_id ASC
        """
        params: List[Any] = [since.strftime("%Y-%m-%d %H:%M:%S")]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        return [self._decode_trade_case_row(dict(row)) for row in cursor.fetchall()]

    def get_recent_closed_trade_case_for_symbol(
        self,
        symbol: str,
        close_cooldown_minutes: int = 0,
        loss_cooldown_minutes: int = 0,
        loss_threshold_pct: float = 0.0,
        require_no_active_case: bool = False,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        symbol = str(symbol or "").strip().upper()
        if not symbol:
            return {}
        if require_no_active_case and self.get_active_trade_case(symbol):
            return {}
        cursor = self.conn.execute(
            """
            SELECT *
            FROM trade_cases
            WHERE symbol = ?
              AND status = 'closed'
              AND closed_at IS NOT NULL
            ORDER BY closed_at DESC, trade_case_id DESC
            LIMIT 1
            """,
            (symbol,),
        )
        row = cursor.fetchone()
        if row is None:
            return {}
        item = self._decode_trade_case_row(dict(row))
        closed_at_text = str(item.get("closed_at") or "").strip()
        if not closed_at_text:
            return {}
        try:
            closed_at = datetime.strptime(closed_at_text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return {}
        now = now or self._utcnow()
        elapsed_minutes = max(0.0, (now - closed_at).total_seconds() / 60.0)
        estimated_pnl_pct = item.get("estimated_pnl_pct")
        try:
            estimated_pnl_pct = float(estimated_pnl_pct) if estimated_pnl_pct is not None else None
        except (TypeError, ValueError):
            estimated_pnl_pct = None

        active = False
        reason = ""
        remaining_minutes = 0
        if (
            loss_cooldown_minutes > 0
            and estimated_pnl_pct is not None
            and estimated_pnl_pct <= float(loss_threshold_pct)
            and elapsed_minutes < float(loss_cooldown_minutes)
        ):
            active = True
            reason = "recent_symbol_loss_guard"
            remaining_minutes = max(0, int(round(float(loss_cooldown_minutes) - elapsed_minutes)))
        elif close_cooldown_minutes > 0 and elapsed_minutes < float(close_cooldown_minutes):
            active = True
            reason = "recent_symbol_close_guard"
            remaining_minutes = max(0, int(round(float(close_cooldown_minutes) - elapsed_minutes)))

        return {
            "active": active,
            "reason": reason,
            "remaining_minutes": remaining_minutes,
            "trade_case_id": item.get("trade_case_id"),
            "symbol": symbol,
            "closed_at": closed_at_text,
            "estimated_pnl_pct": estimated_pnl_pct,
            "exit_reason": item.get("exit_reason"),
            "elapsed_minutes": round(elapsed_minutes, 2),
        }

    def get_trade_case_vector_candidates(
        self,
        limit: int = 200,
        symbols: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT *
            FROM trade_case_vector_index
        """
        params: List[Any] = []
        clauses: List[str] = []
        clean_symbols = [str(symbol).strip() for symbol in (symbols or []) if str(symbol).strip()]
        clean_statuses = [str(status).strip() for status in (statuses or []) if str(status).strip()]
        if clean_symbols:
            placeholders = ", ".join("?" for _ in clean_symbols)
            clauses.append(f"symbol IN ({placeholders})")
            params.extend(clean_symbols)
        if clean_statuses:
            placeholders = ", ".join("?" for _ in clean_statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(clean_statuses)
        if since is not None:
            clauses.append("COALESCE(closed_at, opened_at) >= ?")
            params.append(since.strftime("%Y-%m-%d %H:%M:%S"))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY COALESCE(closed_at, opened_at) DESC, trade_case_id DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(query, params)
        return [self._decode_trade_case_vector_row(dict(row)) for row in cursor.fetchall()]

    def get_trade_case_vector_band_candidates(
        self,
        current_feature_vectors: Dict[str, List[float]],
        limit: int = 200,
        statuses: Optional[List[str]] = None,
        since: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        probes: List[tuple[str, int, str]] = []
        for symbol, feature_vector in (current_feature_vectors or {}).items():
            symbol_text = str(symbol).strip()
            if not symbol_text:
                continue
            for band_index, band_hash in self._build_vector_band_hashes(feature_vector):
                probes.append((symbol_text, band_index, band_hash))
        if not probes:
            return []
        query = """
            SELECT
                v.*,
                COUNT(*) AS matched_band_count
            FROM trade_case_vector_bands b
            JOIN trade_case_vector_index v
                ON v.trade_case_id = b.trade_case_id
            WHERE
        """
        params: List[Any] = []
        probe_clauses = []
        for symbol, band_index, band_hash in probes:
            probe_clauses.append("(b.symbol = ? AND b.band_index = ? AND b.band_hash = ?)")
            params.extend([symbol, band_index, band_hash])
        query += " (" + " OR ".join(probe_clauses) + ")"

        clean_statuses = [str(status).strip() for status in (statuses or []) if str(status).strip()]
        if clean_statuses:
            placeholders = ", ".join("?" for _ in clean_statuses)
            query += f" AND v.status IN ({placeholders})"
            params.extend(clean_statuses)
        if since is not None:
            query += " AND COALESCE(v.closed_at, v.opened_at) >= ?"
            params.append(since.strftime("%Y-%m-%d %H:%M:%S"))

        query += """
            GROUP BY v.trade_case_id
            ORDER BY matched_band_count DESC, COALESCE(v.closed_at, v.opened_at) DESC, v.trade_case_id DESC
            LIMIT ?
        """
        params.append(limit)
        cursor = self.conn.execute(query, params)
        return [self._decode_trade_case_vector_row(dict(row)) for row in cursor.fetchall()]

    def save_trade_case_snapshots(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO trade_case_snapshots (
                        trade_case_id,
                        timestamp,
                        audit_log_id,
                        symbol,
                        current_price,
                        mark_price,
                        mid_price,
                        spread_bps,
                        volume_ratio_15m,
                        unrealized_pnl_pct,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            entry.get("trade_case_id"),
                            entry.get("timestamp"),
                            entry.get("audit_log_id"),
                            entry.get("symbol"),
                            entry.get("current_price"),
                            entry.get("mark_price"),
                            entry.get("mid_price"),
                            entry.get("spread_bps"),
                            entry.get("volume_ratio_15m"),
                            entry.get("unrealized_pnl_pct"),
                            json.dumps(entry.get("payload") or {}),
                        )
                        for entry in entries
                        if entry.get("trade_case_id") and entry.get("symbol")
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save trade case snapshots: {e}")

    def get_trade_case_snapshots(self, trade_case_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        query = """
            SELECT
                id,
                trade_case_id,
                timestamp,
                audit_log_id,
                symbol,
                current_price,
                mark_price,
                mid_price,
                spread_bps,
                volume_ratio_15m,
                unrealized_pnl_pct,
                payload
            FROM trade_case_snapshots
            WHERE trade_case_id = ?
            ORDER BY timestamp ASC, id ASC
        """
        params: List[Any] = [trade_case_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_trade_case_snapshots_between(
        self,
        trade_case_id: str,
        start_timestamp: Optional[str] = None,
        end_timestamp: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        query = """
            SELECT
                id,
                trade_case_id,
                timestamp,
                audit_log_id,
                symbol,
                current_price,
                mark_price,
                mid_price,
                spread_bps,
                volume_ratio_15m,
                unrealized_pnl_pct,
                payload
            FROM trade_case_snapshots
            WHERE trade_case_id = ?
        """
        params: List[Any] = [trade_case_id]
        if start_timestamp:
            query += " AND timestamp >= ?"
            params.append(start_timestamp)
        if end_timestamp:
            query += " AND timestamp <= ?"
            params.append(end_timestamp)
        query += " ORDER BY timestamp ASC, id ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def save_replay_run(self, audit_log_id: int, payload: Dict[str, Any]):
        comparison = payload.get("comparison") or {}
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO replay_runs (
                        audit_log_id,
                        original_decision,
                        replayed_decision,
                        decision_changed,
                        order_count_changed,
                        side_changed,
                        order_type_changed,
                        execution_profile_changed,
                        size_delta,
                        confidence_delta,
                        simulated_fill_ratio,
                        simulated_slippage_bps,
                        simulated_latency_seconds,
                        rationale_keywords_added,
                        rationale_keywords_removed,
                        evidence_signals_added,
                        evidence_signals_removed,
                        evidence_signal_count_delta,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        audit_log_id,
                        comparison.get("original_decision"),
                        comparison.get("replayed_decision"),
                        int(bool(comparison.get("decision_changed"))),
                        int(bool(comparison.get("order_count_changed"))),
                        int(bool(comparison.get("side_changed"))),
                        int(bool(comparison.get("order_type_changed"))),
                        int(bool(comparison.get("execution_profile_changed"))),
                        float(comparison.get("size_delta") or 0.0),
                        float(comparison.get("confidence_delta") or 0.0),
                        payload.get("simulated_fill_ratio"),
                        payload.get("simulated_slippage_bps"),
                        payload.get("simulated_latency_seconds"),
                        json.dumps(comparison.get("rationale_keywords_added") or []),
                        json.dumps(comparison.get("rationale_keywords_removed") or []),
                        json.dumps(comparison.get("evidence_signals_added") or []),
                        json.dumps(comparison.get("evidence_signals_removed") or []),
                        int(comparison.get("evidence_signal_count_delta") or 0),
                        json.dumps(payload),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to save replay run: {e}")

    def _build_execution_policy_context_key(
        self,
        symbol: str,
        loop_mode: str,
        action_type: str,
        urgency_bucket: str,
        liquidity_bucket: str,
        risk_mode: str,
    ) -> str:
        return "|".join(
            [
                str(symbol or "*").strip() or "*",
                str(loop_mode or "").strip() or "unknown",
                str(action_type or "").strip() or "unknown",
                str(urgency_bucket or "").strip() or "unknown",
                str(liquidity_bucket or "").strip() or "unknown",
                str(risk_mode or "").strip() or "unknown",
            ]
        )

    def save_execution_policy_evaluations(self, audit_log_id: int, entries: List[Dict[str, Any]]):
        if not audit_log_id or not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO execution_policy_evaluations (
                        audit_log_id,
                        symbol,
                        loop_mode,
                        risk_mode,
                        action_type,
                        urgency_bucket,
                        liquidity_bucket,
                        decision,
                        order_action,
                        order_type,
                        execution_profile,
                        reduce_only,
                        trigger_mode,
                        spread_bps,
                        spread_percentile_1h,
                        depth_percentile_1h,
                        reference_price,
                        order_price,
                        average_fill_price,
                        requested_quantity,
                        filled_quantity,
                        fill_ratio,
                        slippage_bps,
                        latency_seconds,
                        status,
                        outcome_trade_case_id,
                        outcome_estimated_pnl_pct,
                        outcome_realized_pnl,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_log_id,
                            entry.get("symbol"),
                            entry.get("loop_mode"),
                            entry.get("risk_mode"),
                            entry.get("action_type"),
                            entry.get("urgency_bucket"),
                            entry.get("liquidity_bucket"),
                            entry.get("decision"),
                            entry.get("order_action"),
                            entry.get("order_type"),
                            entry.get("execution_profile"),
                            int(bool(entry.get("reduce_only"))),
                            int(bool(entry.get("trigger_mode"))),
                            entry.get("spread_bps"),
                            entry.get("spread_percentile_1h"),
                            entry.get("depth_percentile_1h"),
                            entry.get("reference_price"),
                            entry.get("order_price"),
                            entry.get("average_fill_price"),
                            entry.get("requested_quantity"),
                            entry.get("filled_quantity"),
                            entry.get("fill_ratio"),
                            entry.get("slippage_bps"),
                            entry.get("latency_seconds"),
                            entry.get("status"),
                            entry.get("outcome_trade_case_id"),
                            entry.get("outcome_estimated_pnl_pct"),
                            entry.get("outcome_realized_pnl"),
                            json.dumps(entry.get("payload") or {}),
                        )
                        for entry in entries
                        if entry.get("symbol")
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save execution policy evaluations: {e}")

    def get_execution_policy_evaluations_since(self, since: datetime) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM execution_policy_evaluations
            WHERE created_at >= ?
            ORDER BY id ASC
            """,
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def upsert_execution_policy_profile(self, record: Dict[str, Any]):
        symbol = str(record.get("symbol") or "*").strip() or "*"
        loop_mode = str(record.get("loop_mode") or "unknown").strip() or "unknown"
        action_type = str(record.get("action_type") or "unknown").strip() or "unknown"
        urgency_bucket = str(record.get("urgency_bucket") or "unknown").strip() or "unknown"
        liquidity_bucket = str(record.get("liquidity_bucket") or "unknown").strip() or "unknown"
        risk_mode = str(record.get("risk_mode") or "unknown").strip() or "unknown"
        context_key = self._build_execution_policy_context_key(
            symbol=symbol,
            loop_mode=loop_mode,
            action_type=action_type,
            urgency_bucket=urgency_bucket,
            liquidity_bucket=liquidity_bucket,
            risk_mode=risk_mode,
        )
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO execution_policy_profiles (
                        context_key,
                        symbol,
                        loop_mode,
                        risk_mode,
                        action_type,
                        urgency_bucket,
                        liquidity_bucket,
                        recommended_order_type,
                        recommended_execution_profile,
                        metrics,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(context_key) DO UPDATE SET
                        symbol = excluded.symbol,
                        loop_mode = excluded.loop_mode,
                        risk_mode = excluded.risk_mode,
                        action_type = excluded.action_type,
                        urgency_bucket = excluded.urgency_bucket,
                        liquidity_bucket = excluded.liquidity_bucket,
                        recommended_order_type = excluded.recommended_order_type,
                        recommended_execution_profile = excluded.recommended_execution_profile,
                        metrics = excluded.metrics,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        context_key,
                        symbol,
                        loop_mode,
                        risk_mode,
                        action_type,
                        urgency_bucket,
                        liquidity_bucket,
                        record.get("recommended_order_type"),
                        record.get("recommended_execution_profile"),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to upsert execution policy profile: {e}")

    def save_execution_policy_profiles(self, entries: List[Dict[str, Any]]):
        for entry in entries or []:
            self.upsert_execution_policy_profile(entry)

    def get_execution_policy_profile(
        self,
        symbol: str,
        loop_mode: str,
        action_type: str,
        urgency_bucket: str,
        liquidity_bucket: str,
        risk_mode: str,
    ) -> Dict[str, Any]:
        context_key = self._build_execution_policy_context_key(
            symbol=symbol,
            loop_mode=loop_mode,
            action_type=action_type,
            urgency_bucket=urgency_bucket,
            liquidity_bucket=liquidity_bucket,
            risk_mode=risk_mode,
        )
        cursor = self.conn.execute(
            """
            SELECT *
            FROM execution_policy_profiles
            WHERE context_key = ?
            LIMIT 1
            """,
            (context_key,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = dict(row)
        item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
        return item

    def get_recent_execution_policy_profiles(self, limit: int = 50) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM execution_policy_profiles
            ORDER BY updated_at DESC, context_key DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def save_candidate_policy_version(self, record: Dict[str, Any]) -> Optional[int]:
        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    INSERT INTO candidate_policy_versions (
                        domain,
                        policy_type,
                        regime_key,
                        policy_name,
                        source,
                        status,
                        rule_text,
                        config,
                        metrics,
                        evaluated_at,
                        promoted_at,
                        retired_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("domain"),
                        record.get("policy_type"),
                        record.get("regime_key"),
                        record.get("policy_name"),
                        record.get("source"),
                        record.get("status") or "candidate",
                        record.get("rule_text"),
                        json.dumps(record.get("config") or {}),
                        json.dumps(record.get("metrics") or {}),
                        record.get("evaluated_at"),
                        record.get("promoted_at"),
                        record.get("retired_at"),
                    ),
                )
                return int(cursor.lastrowid)
        except Exception as e:
            logger.error(f"Failed to save candidate policy version: {e}")
            return None

    def get_candidate_policy_versions(
        self,
        domain: Optional[str] = None,
        regime_key: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM candidate_policy_versions"
        clauses = []
        params: List[Any] = []
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if regime_key is not None:
            if str(regime_key).strip():
                clauses.append("COALESCE(regime_key, '') = ?")
                params.append(str(regime_key).strip())
            else:
                clauses.append("(regime_key IS NULL OR regime_key = '')")
        clean_statuses = [str(status).strip() for status in (statuses or []) if str(status).strip()]
        if clean_statuses:
            placeholders = ", ".join("?" for _ in clean_statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(clean_statuses)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["config"] = self._safe_json_loads(item.get("config"), default={})
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def save_policy_evaluation(self, record: Dict[str, Any]) -> Optional[int]:
        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    INSERT INTO policy_evaluations (
                        candidate_policy_id,
                        evaluation_scope,
                        sample_count,
                        score,
                        metrics
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("candidate_policy_id"),
                        record.get("evaluation_scope"),
                        record.get("sample_count", 0),
                        record.get("score", 0.0),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
                with self.conn:
                    self.conn.execute(
                        """
                        UPDATE candidate_policy_versions
                        SET evaluated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (record.get("candidate_policy_id"),),
                    )
                return int(cursor.lastrowid)
        except Exception as e:
            logger.error(f"Failed to save policy evaluation: {e}")
            return None

    def get_policy_evaluations(self, candidate_policy_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM policy_evaluations
            WHERE candidate_policy_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (candidate_policy_id,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def upsert_policy_registry(self, record: Dict[str, Any]):
        policy_key = str(record.get("policy_key") or "").strip()
        if not policy_key:
            return
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO policy_registry (
                        policy_key,
                        domain,
                        policy_type,
                        regime_key,
                        active_candidate_policy_id,
                        version,
                        config,
                        metrics,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(policy_key) DO UPDATE SET
                        domain = excluded.domain,
                        policy_type = excluded.policy_type,
                        regime_key = excluded.regime_key,
                        active_candidate_policy_id = excluded.active_candidate_policy_id,
                        version = excluded.version,
                        config = excluded.config,
                        metrics = excluded.metrics,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        policy_key,
                        record.get("domain"),
                        record.get("policy_type"),
                        record.get("regime_key"),
                        record.get("active_candidate_policy_id"),
                        record.get("version", 1),
                        json.dumps(record.get("config") or {}),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to upsert policy registry: {e}")

    def get_policy_registry_entry(self, policy_key: str) -> Dict[str, Any]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM policy_registry
            WHERE policy_key = ?
            LIMIT 1
            """,
            (policy_key,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = dict(row)
        item["config"] = self._safe_json_loads(item.get("config"), default={})
        item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
        return item

    def build_policy_key(self, domain: str, regime_key: Optional[str] = None) -> str:
        domain_text = str(domain or "").strip()
        regime_text = str(regime_key or "").strip()
        if regime_text:
            return f"champion:{domain_text}:{regime_text}"
        return f"champion:{domain_text}"

    def get_policy_registry_entry_for_regime(self, domain: str, regime_key: Optional[str] = None) -> Dict[str, Any]:
        regime_text = str(regime_key or "").strip()
        if regime_text:
            scoped = self.get_policy_registry_entry(self.build_policy_key(domain, regime_text))
            if scoped:
                return scoped
        return self.get_policy_registry_entry(self.build_policy_key(domain))

    def list_policy_registry(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        query = "SELECT * FROM policy_registry"
        params: List[Any] = []
        if domain:
            query += " WHERE domain = ?"
            params.append(domain)
        query += " ORDER BY updated_at DESC, policy_key DESC"
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["config"] = self._safe_json_loads(item.get("config"), default={})
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def save_policy_promotion(self, record: Dict[str, Any]):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO policy_promotions (
                        domain,
                        regime_key,
                        previous_candidate_policy_id,
                        new_candidate_policy_id,
                        reason,
                        metrics
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("domain"),
                        record.get("regime_key"),
                        record.get("previous_candidate_policy_id"),
                        record.get("new_candidate_policy_id"),
                        record.get("reason"),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
                self.conn.execute(
                    """
                    UPDATE candidate_policy_versions
                    SET status = 'promoted', promoted_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (record.get("new_candidate_policy_id"),),
                )
        except Exception as e:
            logger.error(f"Failed to save policy promotion: {e}")

    def save_shadow_decisions(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO shadow_decisions (
                        audit_log_id,
                        candidate_policy_id,
                        domain,
                        symbol,
                        loop_mode,
                        trade_case_id,
                        divergence_type,
                        outcome_status,
                        outcome_estimated_pnl_pct,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            entry.get("audit_log_id"),
                            entry.get("candidate_policy_id"),
                            entry.get("domain"),
                            entry.get("symbol"),
                            entry.get("loop_mode"),
                            entry.get("trade_case_id"),
                            entry.get("divergence_type"),
                            entry.get("outcome_status"),
                            entry.get("outcome_estimated_pnl_pct"),
                            json.dumps(entry.get("payload") or {}),
                        )
                        for entry in entries
                        if entry.get("audit_log_id") and entry.get("candidate_policy_id")
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save shadow decisions: {e}")

    def upsert_policy_bundle(self, record: Dict[str, Any]):
        bundle_key = str(record.get("bundle_key") or "").strip()
        if not bundle_key:
            return
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO policy_bundles (
                        bundle_key,
                        regime_key,
                        status,
                        alpha_policy_key,
                        position_policy_key,
                        safety_policy_key,
                        execution_policy_key,
                        metrics,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(bundle_key) DO UPDATE SET
                        regime_key = excluded.regime_key,
                        status = excluded.status,
                        alpha_policy_key = excluded.alpha_policy_key,
                        position_policy_key = excluded.position_policy_key,
                        safety_policy_key = excluded.safety_policy_key,
                        execution_policy_key = excluded.execution_policy_key,
                        metrics = excluded.metrics,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        bundle_key,
                        record.get("regime_key"),
                        record.get("status") or "candidate",
                        record.get("alpha_policy_key"),
                        record.get("position_policy_key"),
                        record.get("safety_policy_key"),
                        record.get("execution_policy_key"),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to upsert policy bundle: {e}")

    def get_policy_bundle(self, bundle_key: str) -> Dict[str, Any]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM policy_bundles
            WHERE bundle_key = ?
            LIMIT 1
            """,
            (bundle_key,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = dict(row)
        item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
        return item

    def get_active_policy_bundle(self, regime_key: Optional[str] = None) -> Dict[str, Any]:
        regime_text = str(regime_key or "").strip()
        if regime_text:
            cursor = self.conn.execute(
                """
                SELECT *
                FROM policy_bundles
                WHERE regime_key = ? AND status = 'active'
                ORDER BY updated_at DESC, bundle_key DESC
                LIMIT 1
                """,
                (regime_text,),
            )
            row = cursor.fetchone()
            if row:
                item = dict(row)
                item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
                return item
        cursor = self.conn.execute(
            """
            SELECT *
            FROM policy_bundles
            WHERE (regime_key IS NULL OR regime_key = '') AND status = 'active'
            ORDER BY updated_at DESC, bundle_key DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        if not row:
            return {}
        item = dict(row)
        item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
        return item

    def get_policy_bundles(
        self,
        regime_key: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM policy_bundles"
        clauses = []
        params: List[Any] = []
        regime_text = str(regime_key or "").strip()
        if regime_key is not None:
            if regime_text:
                clauses.append("COALESCE(regime_key, '') = ?")
                params.append(regime_text)
            else:
                clauses.append("(regime_key IS NULL OR regime_key = '')")
        clean_statuses = [str(status).strip() for status in (statuses or []) if str(status).strip()]
        if clean_statuses:
            placeholders = ", ".join("?" for _ in clean_statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(clean_statuses)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_at DESC, bundle_key DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def save_bundle_evaluation(self, record: Dict[str, Any]) -> Optional[int]:
        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    INSERT INTO bundle_evaluations (
                        bundle_key,
                        evaluation_scope,
                        sample_count,
                        score,
                        metrics
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("bundle_key"),
                        record.get("evaluation_scope"),
                        record.get("sample_count", 0),
                        record.get("score", 0.0),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
                return int(cursor.lastrowid)
        except Exception as e:
            logger.error(f"Failed to save bundle evaluation: {e}")
            return None

    def get_bundle_evaluations(self, bundle_key: str) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM bundle_evaluations
            WHERE bundle_key = ?
            ORDER BY created_at DESC, id DESC
            """,
            (bundle_key,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def save_bundle_promotion(self, record: Dict[str, Any]):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO bundle_promotions (
                        regime_key,
                        previous_bundle_key,
                        new_bundle_key,
                        reason,
                        metrics
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        record.get("regime_key"),
                        record.get("previous_bundle_key"),
                        record.get("new_bundle_key"),
                        record.get("reason"),
                        json.dumps(record.get("metrics") or {}),
                    ),
                )
                previous_bundle_key = str(record.get("previous_bundle_key") or "").strip()
                if previous_bundle_key:
                    self.conn.execute(
                        """
                        UPDATE policy_bundles
                        SET status = 'shadow', updated_at = CURRENT_TIMESTAMP
                        WHERE bundle_key = ?
                        """,
                        (previous_bundle_key,),
                    )
                self.conn.execute(
                    """
                    UPDATE policy_bundles
                    SET status = 'active', updated_at = CURRENT_TIMESTAMP
                    WHERE bundle_key = ?
                    """,
                    (record.get("new_bundle_key"),),
                )
        except Exception as e:
            logger.error(f"Failed to save bundle promotion: {e}")

    def get_recent_bundle_promotions(self, regime_key: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
        query = "SELECT * FROM bundle_promotions"
        params: List[Any] = []
        regime_text = str(regime_key or "").strip()
        if regime_key is not None:
            if regime_text:
                query += " WHERE COALESCE(regime_key, '') = ?"
                params.append(regime_text)
            else:
                query += " WHERE (regime_key IS NULL OR regime_key = '')"
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        cursor = self.conn.execute(query, params)
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["metrics"] = self._safe_json_loads(item.get("metrics"), default={})
            rows.append(item)
        return rows

    def get_shadow_decisions_since(self, since: datetime) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM shadow_decisions
            WHERE created_at >= ?
            ORDER BY id ASC
            """,
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def update_shadow_decisions_for_trade_case(
        self,
        trade_case_id: str,
        outcome_status: Optional[str] = None,
        outcome_estimated_pnl_pct: Optional[float] = None,
    ):
        if not trade_case_id:
            return
        try:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE shadow_decisions
                    SET
                        outcome_status = COALESCE(?, outcome_status),
                        outcome_estimated_pnl_pct = COALESCE(?, outcome_estimated_pnl_pct)
                    WHERE trade_case_id = ?
                    """,
                    (
                        outcome_status,
                        outcome_estimated_pnl_pct,
                        trade_case_id,
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to update shadow decision outcomes: {e}")

    def save_order_state_transitions(self, audit_log_id: int, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                for entry in entries:
                    client_order_id = entry.get("client_order_id")
                    exchange_order_id = entry.get("exchange_order_id")
                    order_key = client_order_id or exchange_order_id
                    from_state = self._normalize_order_state(entry.get("from_state"))
                    to_state = self._normalize_order_state(entry.get("to_state"))
                    if order_key:
                        runtime_row = self.conn.execute(
                            """
                            SELECT state
                            FROM order_runtime_state
                            WHERE order_key = ?
                            LIMIT 1
                            """,
                            (order_key,),
                        ).fetchone()
                        if runtime_row and runtime_row["state"]:
                            from_state = self._normalize_order_state(runtime_row["state"])
                    if not self._is_valid_order_state_transition(from_state, to_state):
                        logger.warning(
                            "Invalid order state transition detected (audit_id={}): {} -> {}. Coercing to unknown.",
                            audit_log_id,
                            from_state,
                            to_state,
                        )
                        to_state = "unknown"

                    payload = entry.get("payload", {})
                    self.conn.execute(
                        """
                        INSERT INTO order_state_transitions (
                            audit_log_id,
                            symbol,
                            client_order_id,
                            exchange_order_id,
                            replace_group_id,
                            lineage_role,
                            lineage_parent_order_id,
                            lineage_parent_client_order_id,
                            from_state,
                            to_state,
                            event_type,
                            payload
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            audit_log_id,
                            entry.get("symbol"),
                            client_order_id,
                            exchange_order_id,
                            entry.get("replace_group_id") or payload.get("replace_group_id"),
                            entry.get("lineage_role") or payload.get("lineage_role"),
                            entry.get("lineage_parent_order_id") or payload.get("lineage_parent_order_id"),
                            entry.get("lineage_parent_client_order_id") or payload.get("lineage_parent_client_order_id"),
                            from_state,
                            to_state,
                            entry.get("event_type"),
                            json.dumps(payload),
                        ),
                    )
                    self._upsert_order_runtime_state(
                        order_key=order_key,
                        client_order_id=client_order_id,
                        exchange_order_id=exchange_order_id,
                        symbol=entry.get("symbol"),
                        replace_group_id=entry.get("replace_group_id") or payload.get("replace_group_id"),
                        lineage_role=entry.get("lineage_role") or payload.get("lineage_role"),
                        lineage_parent_order_id=entry.get("lineage_parent_order_id") or payload.get("lineage_parent_order_id"),
                        lineage_parent_client_order_id=entry.get("lineage_parent_client_order_id") or payload.get("lineage_parent_client_order_id"),
                        state=to_state,
                        audit_log_id=audit_log_id,
                        payload=payload,
                    )
        except Exception as e:
            logger.error(f"Failed to save order state transitions: {e}")

    def try_acquire_runtime_lock(self, lock_name: str, owner: str, ttl_seconds: int = 240) -> bool:
        now_epoch = time.time()
        expires_epoch = now_epoch + max(1, int(ttl_seconds or 1))
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO runtime_locks (
                        lock_name,
                        owner,
                        acquired_at_epoch,
                        expires_at_epoch
                    ) VALUES (?, '', 0, 0)
                    """,
                    (lock_name,),
                )
                updated = self.conn.execute(
                    """
                    UPDATE runtime_locks
                    SET owner = ?, acquired_at_epoch = ?, expires_at_epoch = ?
                    WHERE lock_name = ?
                      AND (expires_at_epoch <= ? OR owner = ?)
                    """,
                    (
                        owner,
                        now_epoch,
                        expires_epoch,
                        lock_name,
                        now_epoch,
                        owner,
                    ),
                )
                return updated.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to acquire runtime lock {lock_name}: {e}")
            return False

    def release_runtime_lock(self, lock_name: str, owner: str):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE runtime_locks
                    SET owner = '', acquired_at_epoch = 0, expires_at_epoch = 0
                    WHERE lock_name = ? AND owner = ?
                    """,
                    (lock_name, owner),
                )
        except Exception as e:
            logger.error(f"Failed to release runtime lock {lock_name}: {e}")

    def acquire_entry_mutex(self, symbol: str, owner: str, ttl_seconds: int = 180) -> bool:
        now_epoch = time.time()
        expires_epoch = now_epoch + max(1, int(ttl_seconds or 1))
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO entry_mutex (
                        symbol,
                        owner,
                        acquired_at_epoch,
                        expires_at_epoch
                    ) VALUES (?, '', 0, 0)
                    """,
                    (symbol,),
                )
                updated = self.conn.execute(
                    """
                    UPDATE entry_mutex
                    SET owner = ?, acquired_at_epoch = ?, expires_at_epoch = ?
                    WHERE symbol = ?
                      AND (expires_at_epoch <= ? OR owner = ?)
                    """,
                    (
                        owner,
                        now_epoch,
                        expires_epoch,
                        symbol,
                        now_epoch,
                        owner,
                    ),
                )
                return updated.rowcount > 0
        except Exception as e:
            logger.error(f"Failed to acquire entry mutex {symbol}: {e}")
            return False

    def release_entry_mutex(self, symbol: str, owner: str):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    UPDATE entry_mutex
                    SET owner = '', acquired_at_epoch = 0, expires_at_epoch = 0
                    WHERE symbol = ? AND owner = ?
                    """,
                    (symbol, owner),
                )
        except Exception as e:
            logger.error(f"Failed to release entry mutex {symbol}: {e}")

    def save_decision_fingerprints(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO decision_fingerprints (
                        symbol,
                        action,
                        size_band,
                        fingerprint,
                        audit_log_id,
                        status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            str(entry.get("symbol") or "").upper(),
                            str(entry.get("action") or "").upper(),
                            str(entry.get("size_band") or "unknown"),
                            str(entry.get("fingerprint") or ""),
                            entry.get("audit_log_id"),
                            entry.get("status") or "planned",
                        )
                        for entry in entries
                        if entry.get("symbol") and entry.get("action") and entry.get("fingerprint")
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save decision fingerprints: {e}")

    def has_recent_decision_fingerprint(
        self,
        symbol: str,
        action: str,
        size_band: str,
        within_minutes: int = 10,
    ) -> bool:
        minutes = max(1, int(within_minutes or 1))
        since = (self._utcnow() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute(
            """
            SELECT 1
            FROM decision_fingerprints
            WHERE symbol = ?
              AND action = ?
              AND size_band = ?
              AND created_at >= ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                str(symbol or "").upper(),
                str(action or "").upper(),
                str(size_band or "unknown"),
                since,
            ),
        ).fetchone()
        return row is not None

    def count_recent_unknown_orders(self, minutes: int = 30) -> int:
        lookback_minutes = max(1, int(minutes or 1))
        since = (self._utcnow() - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM order_runtime_state
            WHERE state = 'unknown'
              AND updated_at >= ?
            """,
            (since,),
        ).fetchone()
        if row is None:
            return 0
        return int(row["count"] or 0)

    def get_unknown_order_runtime_states(self, limit: int = 50) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                order_key,
                client_order_id,
                exchange_order_id,
                symbol,
                state,
                updated_at,
                last_audit_log_id,
                payload
            FROM order_runtime_state
            WHERE state = 'unknown'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def save_position_state_transitions(self, audit_log_id: int, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO position_state_transitions (
                        audit_log_id,
                        symbol,
                        from_state,
                        to_state,
                        event_type,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            audit_log_id,
                            entry.get("symbol"),
                            entry.get("from_state"),
                            entry.get("to_state"),
                            entry.get("event_type"),
                            json.dumps(entry.get("payload", {})),
                        )
                        for entry in entries
                    ],
                )
        except Exception as e:
            logger.error(f"Failed to save position state transitions: {e}")

    def get_recent_long_term_memory(self, limit: int = 10) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT id, timestamp, rule, version, metrics
            FROM long_term_memory
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            rows.append(self._format_memory_row(dict(row)))
        rows.sort(
            key=lambda row: (
                1 if bool(row.get("retired")) else 0,
                -float(row.get("confidence_score") or 0.0),
                -float(row.get("validity_score") or 0.0),
                -int(row.get("id") or 0),
            )
        )
        return rows

    def get_monitoring_snapshot(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._utcnow()
        since = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        fill_stats = self.get_fill_stats(hours=24, now=now)

        cursor = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total_audits,
                SUM(CASE WHEN risk_approved = 0 THEN 1 ELSE 0 END) AS rejected_audits,
                SUM(CASE WHEN risk_reason LIKE 'Error in %' THEN 1 ELSE 0 END) AS loop_errors
            FROM audit_logs
            WHERE timestamp >= ?
            """,
            (since,),
        )
        stats = dict(cursor.fetchone() or {})

        unresolved_timeouts = self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM audit_logs
            WHERE timestamp >= ?
              AND receipts LIKE '%timeout_unresolved%'
            """,
            (since,),
        ).fetchone()

        latest_audit_row = self.conn.execute(
            """
            SELECT timestamp, intent, risk_approved, risk_reason, execution_summary
            FROM audit_logs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        latest_memory_row = self.conn.execute(
            """
            SELECT timestamp, rule, metrics
            FROM long_term_memory
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        recent_logs = self.get_recent_audits(limit=20)
        semantic_validation_summary = self._build_semantic_validation_summary(now=now)

        snapshot = {
            "total_audits_24h": int(stats.get("total_audits") or 0),
            "rejected_24h": int(stats.get("rejected_audits") or 0),
            "loop_errors_24h": int(stats.get("loop_errors") or 0),
            "timeouts_unresolved_24h": int((unresolved_timeouts or {})["count"] or 0),
            "unknown_order_states_30m": self.count_recent_unknown_orders(minutes=30),
            "fee_cost_24h": fill_stats["fee_cost"],
            "fill_count_24h": fill_stats["fill_count"],
            "realized_pnl_24h": fill_stats["realized_pnl"],
            "ledger_net_pnl_24h": self.get_position_ledger_stats(hours=24, now=now)["net_realized_pnl"],
            "latest_audit": dict(latest_audit_row) if latest_audit_row else None,
            "latest_memory": self._decode_metrics_row(latest_memory_row),
            "recent_logs": recent_logs,
            "semantic_validation_summary": semantic_validation_summary,
            "recent_memory": self.get_recent_long_term_memory(limit=5),
            "recent_symbol_journal": self.get_recent_symbol_journal(limit=10),
            "recent_fills": self.get_recent_fills(limit=10),
            "recent_ledger": self.get_recent_position_ledger(limit=10),
            "recent_replays": self.get_recent_replay_runs(limit=10),
            "recent_order_states": self.get_recent_order_state_transitions(limit=10),
            "recent_position_states": self.get_recent_position_state_transitions(limit=10),
        }
        return snapshot

    def _build_semantic_validation_summary(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._utcnow()
        last_24h_logs = self.get_recent_audits(limit=500, since=now - timedelta(hours=24))
        last_7d_logs = self.get_recent_audits(limit=5000, since=now - timedelta(days=7))
        latest_50_logs = self.get_recent_audits(limit=50)
        return {
            "last_24h": self._summarize_semantic_validation_issues(last_24h_logs),
            "last_7d": self._summarize_semantic_validation_issues(last_7d_logs),
            "latest_50": self._summarize_semantic_validation_issues(latest_50_logs),
        }

    def _summarize_semantic_validation_issues(self, recent_logs: List[Dict[str, Any]]) -> Dict[str, Any]:
        issue_counts: Dict[str, int] = {}
        model_failure_counts: Dict[str, int] = {}
        model_total_counts: Dict[str, int] = {}
        validation_failures = 0
        for row in recent_logs:
            execution_summary = row.get("execution_summary") or {}
            ai_usage = execution_summary.get("ai_usage") or {}
            model_name = str(ai_usage.get("model_name") or "unknown").strip() or "unknown"
            if ai_usage:
                model_total_counts[model_name] = model_total_counts.get(model_name, 0) + 1
            if not ai_usage.get("semantic_validation_error"):
                continue
            validation_failures += 1
            model_failure_counts[model_name] = model_failure_counts.get(model_name, 0) + 1
            for issue in ai_usage.get("issues") or []:
                key = str(issue).strip()
                if not key:
                    continue
                issue_counts[key] = issue_counts.get(key, 0) + 1
        top_issues = [
            {"issue": issue, "count": count}
            for issue, count in sorted(issue_counts.items(), key=lambda item: (-item[1], item[0]))[:5]
        ]
        top_models = []
        for model_name, failure_count in sorted(model_failure_counts.items(), key=lambda item: (-item[1], item[0]))[:5]:
            total_count = model_total_counts.get(model_name, failure_count)
            failure_rate = (failure_count / total_count) if total_count > 0 else 0.0
            top_models.append(
                {
                    "model_name": model_name,
                    "count": failure_count,
                    "total_calls": total_count,
                    "failure_rate": round(failure_rate, 4),
                }
            )
        return {
            "total_calls": sum(model_total_counts.values()),
            "failure_count": validation_failures,
            "top_issues": top_issues,
            "top_models": top_models,
        }

    def get_recent_audits(self, limit: int = 20, since: Optional[datetime] = None) -> List[Dict[str, Any]]:
        if since is not None:
            since_value = since.strftime("%Y-%m-%d %H:%M:%S")
            cursor = self.conn.execute(
                """
                SELECT
                    id,
                    timestamp,
                    intent,
                    risk_approved,
                    risk_reason,
                    decision_input_hash,
                    risk_mode,
                    execution_summary,
                    market_vectors
                FROM audit_logs
                WHERE timestamp >= ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (since_value, limit),
            )
        else:
            cursor = self.conn.execute(
                """
                SELECT
                    id,
                    timestamp,
                    intent,
                    risk_approved,
                    risk_reason,
                    decision_input_hash,
                    risk_mode,
                    execution_summary,
                    market_vectors
                FROM audit_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["intent"] = self._safe_json_loads(item.get("intent"), default={})
            item["execution_summary"] = self._safe_json_loads(item.get("execution_summary"), default={})
            item["market_vectors"] = self._safe_json_loads(item.get("market_vectors"), default={})
            rows.append(item)
        return rows

    def get_audit_log(self, audit_id: int) -> Optional[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT *
            FROM audit_logs
            WHERE id = ?
            LIMIT 1
            """,
            (audit_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        decoded = self._decode_audit_row(dict(row))
        decoded["fills"] = self.get_execution_fills(audit_id)
        decoded["ledger_entries"] = self.get_position_ledger_for_audit(audit_id)
        decoded["replay_runs"] = self.get_replay_runs_for_audit(audit_id, limit=10)
        decoded["order_state_transitions"] = self.get_order_state_transitions_for_audit(audit_id)
        decoded["position_state_transitions"] = self.get_position_state_transitions_for_audit(audit_id)
        decoded["symbol_journal_entries"] = self.get_symbol_journal_for_audit(audit_id)
        decoded["trade_cases"] = self.get_trade_cases_for_audit(audit_id)
        decoded["retrieval_evaluations"] = self.get_retrieval_evaluations_for_audit(audit_id, limit=25)
        return decoded

    def get_symbol_journal_for_audit(self, audit_log_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                id,
                timestamp,
                audit_log_id,
                symbol,
                decision,
                risk_reason,
                risk_mode,
                risk_approved,
                triggered,
                risk_triggered,
                net_realized_pnl,
                fee_cost,
                estimated_pnl_pct,
                trade_case_id,
                case_status,
                case_opened_at,
                case_closed_at,
                holding_minutes,
                evidence_signals,
                summary,
                payload
            FROM symbol_journal
            WHERE audit_log_id = ?
            ORDER BY id ASC
            """,
            (audit_log_id,),
        )
        return [self._decode_symbol_journal_row(dict(row)) for row in cursor.fetchall()]

    def get_trade_cases_for_audit(self, audit_log_id: int) -> List[Dict[str, Any]]:
        rows = self.get_symbol_journal_for_audit(audit_log_id)
        ordered_ids: List[str] = []
        seen: set[str] = set()
        for row in rows:
            trade_case_id = str(row.get("trade_case_id") or "").strip()
            if not trade_case_id or trade_case_id in seen:
                continue
            seen.add(trade_case_id)
            ordered_ids.append(trade_case_id)
        cases: List[Dict[str, Any]] = []
        for trade_case_id in ordered_ids:
            trade_case = self.get_trade_case(trade_case_id)
            if trade_case:
                cases.append(trade_case)
        return cases

    def get_retrieval_evaluations_for_audit(
        self,
        audit_log_id: int,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                id,
                audit_log_id,
                created_at,
                context_type,
                rank_index,
                trade_case_id,
                case_symbol,
                matched_symbol,
                decision,
                decision_symbol_aligned,
                estimated_pnl_pct,
                similarity,
                final_score,
                retrieval_source,
                matched_band_count,
                risk_mode,
                outcome_trade_case_id,
                outcome_status,
                outcome_realized_pnl,
                outcome_estimated_pnl_pct,
                outcome_holding_minutes,
                outcome_observed_mfe_pct,
                outcome_observed_mae_pct,
                outcome_slippage_bps,
                outcome_updated_at,
                payload
            FROM retrieval_evaluations
            WHERE audit_log_id = ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (audit_log_id, limit),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_replay_candidates(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT id, timestamp, intent, risk_approved, risk_reason, execution_summary, market_vectors
            FROM audit_logs
            WHERE decision_request IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["intent"] = self._safe_json_loads(item.get("intent"), default={})
            item["execution_summary"] = self._safe_json_loads(item.get("execution_summary"), default={})
            item["market_vectors"] = self._safe_json_loads(item.get("market_vectors"), default={})
            if not self.is_learning_eligible_audit_record(item):
                continue
            rows.append(item)
        return rows

    def get_replay_batch_candidates(
        self,
        *,
        since: datetime,
        limit: int = 40,
    ) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            WITH latest_replay AS (
                SELECT audit_log_id, MAX(created_at) AS latest_created_at
                FROM replay_runs
                GROUP BY audit_log_id
            )
            SELECT
                audit.id,
                audit.timestamp,
                audit.intent,
                audit.risk_approved,
                audit.risk_reason,
                audit.risk_mode,
                audit.execution_summary,
                audit.market_vectors,
                replay.latest_created_at
            FROM audit_logs AS audit
            LEFT JOIN latest_replay AS replay
                ON replay.audit_log_id = audit.id
            WHERE audit.decision_request IS NOT NULL
              AND audit.timestamp >= ?
            ORDER BY audit.id DESC
            """,
            (since.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["intent"] = self._safe_json_loads(item.get("intent"), default={})
            item["execution_summary"] = self._safe_json_loads(item.get("execution_summary"), default={})
            item["market_vectors"] = self._safe_json_loads(item.get("market_vectors"), default={})
            if not self.is_learning_eligible_audit_record(item):
                continue
            rows.append(item)

        def sort_key(item: Dict[str, Any]) -> tuple[int, int, int, int]:
            intent = item.get("intent") or {}
            decision = str(intent.get("decision") or "HOLD").upper()
            risk_ok = bool(item.get("risk_approved"))
            has_replay = bool(item.get("latest_created_at"))
            is_trade = int(decision in ("BUY", "SELL", "REDUCE", "EXIT"))
            is_rejected_entry = int(not risk_ok and decision in ("BUY", "SELL"))
            is_non_hold = int(decision != "HOLD")
            return (
                is_trade * 5 + is_rejected_entry * 4 + is_non_hold * 2 - int(has_replay) * 3,
                int(item.get("id") or 0),
                int(risk_ok),
                0,
            )

        rows.sort(key=sort_key, reverse=True)
        return rows[: max(1, int(limit or 40))]

    def get_recent_rag_candidates(self, limit: int = 500) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                id,
                timestamp,
                market_state,
                account_state,
                market_vectors,
                intent,
                risk_approved
            FROM audit_logs
            WHERE market_state IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["market_state"] = self._safe_json_loads(item.get("market_state"), default={})
            item["account_state"] = self._safe_json_loads(item.get("account_state"), default={})
            item["market_vectors"] = self._safe_json_loads(item.get("market_vectors"), default={})
            item["intent"] = self._safe_json_loads(item.get("intent"), default={})
            rows.append(item)
        return rows

    def get_execution_fills(self, audit_log_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                id,
                fill_timestamp,
                symbol,
                order_id,
                client_order_id,
                trade_id,
                side,
                price,
                amount,
                cost,
                fee_cost,
                fee_currency,
                realized_pnl,
                payload
            FROM audit_fills
            WHERE audit_log_id = ?
            ORDER BY id ASC
            """,
            (audit_log_id,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_recent_fills(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                fill_timestamp,
                symbol,
                side,
                price,
                amount,
                fee_cost,
                fee_currency,
                realized_pnl
            FROM audit_fills
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_recent_position_ledger(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                decision,
                fill_count,
                total_amount,
                gross_realized_pnl,
                fee_cost,
                net_realized_pnl,
                balance_delta,
                balance_delta_pct
            FROM position_ledger
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_position_ledger_for_audit(self, audit_log_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                decision,
                fill_count,
                total_amount,
                gross_realized_pnl,
                fee_cost,
                net_realized_pnl,
                balance_delta,
                balance_delta_pct,
                payload
            FROM position_ledger
            WHERE audit_log_id = ?
            ORDER BY id ASC
            """,
            (audit_log_id,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_fill_stats(self, hours: int = 24, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._utcnow()
        since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS fill_count,
                COALESCE(SUM(fee_cost), 0) AS fee_cost,
                COALESCE(SUM(realized_pnl), 0) AS realized_pnl
            FROM audit_fills
            WHERE created_at >= ?
            """,
            (since,),
        ).fetchone()
        stats = dict(row or {})
        return {
            "fill_count": int(stats.get("fill_count") or 0),
            "fee_cost": round(float(stats.get("fee_cost") or 0.0), 8),
            "realized_pnl": round(float(stats.get("realized_pnl") or 0.0), 8),
        }

    def get_position_ledger_stats(self, hours: int = 24, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or self._utcnow()
        since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS row_count,
                COALESCE(SUM(net_realized_pnl), 0) AS net_realized_pnl,
                COALESCE(SUM(gross_realized_pnl), 0) AS gross_realized_pnl,
                COALESCE(SUM(fee_cost), 0) AS fee_cost
            FROM position_ledger
            WHERE created_at >= ?
            """,
            (since,),
        ).fetchone()
        stats = dict(row or {})
        return {
            "row_count": int(stats.get("row_count") or 0),
            "net_realized_pnl": round(float(stats.get("net_realized_pnl") or 0.0), 8),
            "gross_realized_pnl": round(float(stats.get("gross_realized_pnl") or 0.0), 8),
            "fee_cost": round(float(stats.get("fee_cost") or 0.0), 8),
        }

    def get_symbol_performance_report(self, hours: int = 24, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or self._utcnow()
        since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.execute(
            """
            SELECT
                symbol,
                COUNT(*) AS row_count,
                COALESCE(SUM(fill_count), 0) AS fill_count,
                COALESCE(SUM(total_amount), 0) AS total_amount,
                COALESCE(SUM(gross_realized_pnl), 0) AS gross_realized_pnl,
                COALESCE(SUM(fee_cost), 0) AS fee_cost,
                COALESCE(SUM(net_realized_pnl), 0) AS net_realized_pnl,
                AVG(CASE WHEN net_realized_pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate
            FROM position_ledger
            WHERE created_at >= ?
            GROUP BY symbol
            ORDER BY net_realized_pnl DESC, symbol ASC
            """,
            (since,),
        )
        report = []
        for row in cursor.fetchall():
            item = dict(row)
            item["fill_count"] = int(item.get("fill_count") or 0)
            item["row_count"] = int(item.get("row_count") or 0)
            item["total_amount"] = round(float(item.get("total_amount") or 0.0), 8)
            item["gross_realized_pnl"] = round(float(item.get("gross_realized_pnl") or 0.0), 8)
            item["fee_cost"] = round(float(item.get("fee_cost") or 0.0), 8)
            item["net_realized_pnl"] = round(float(item.get("net_realized_pnl") or 0.0), 8)
            item["win_rate"] = round(float(item.get("win_rate") or 0.0) * 100, 2)
            report.append(item)
        return report

    def get_daily_ledger_report(self, days: int = 7, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or self._utcnow()
        since = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = self.conn.execute(
            """
            SELECT
                substr(created_at, 1, 10) AS day,
                COUNT(*) AS row_count,
                COALESCE(SUM(net_realized_pnl), 0) AS net_realized_pnl,
                COALESCE(SUM(gross_realized_pnl), 0) AS gross_realized_pnl,
                COALESCE(SUM(fee_cost), 0) AS fee_cost
            FROM position_ledger
            WHERE created_at >= ?
            GROUP BY substr(created_at, 1, 10)
            ORDER BY day DESC
            """,
            (since,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["row_count"] = int(item.get("row_count") or 0)
            item["net_realized_pnl"] = round(float(item.get("net_realized_pnl") or 0.0), 8)
            item["gross_realized_pnl"] = round(float(item.get("gross_realized_pnl") or 0.0), 8)
            item["fee_cost"] = round(float(item.get("fee_cost") or 0.0), 8)
            rows.append(item)
        return rows

    def get_recent_replay_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                id,
                audit_log_id,
                created_at,
                original_decision,
                replayed_decision,
                decision_changed,
                order_count_changed,
                side_changed,
                order_type_changed,
                execution_profile_changed,
                size_delta,
                confidence_delta,
                simulated_fill_ratio,
                simulated_slippage_bps,
                simulated_latency_seconds,
                rationale_keywords_added,
                rationale_keywords_removed,
                evidence_signals_added,
                evidence_signals_removed,
                evidence_signal_count_delta,
                payload
            FROM replay_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["rationale_keywords_added"] = self._safe_json_loads(
                item.get("rationale_keywords_added"),
                default=[],
            )
            item["rationale_keywords_removed"] = self._safe_json_loads(
                item.get("rationale_keywords_removed"),
                default=[],
            )
            item["evidence_signals_added"] = self._safe_json_loads(
                item.get("evidence_signals_added"),
                default=[],
            )
            item["evidence_signals_removed"] = self._safe_json_loads(
                item.get("evidence_signals_removed"),
                default=[],
            )
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_replay_runs_for_audit(self, audit_log_id: int, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                id,
                audit_log_id,
                created_at,
                original_decision,
                replayed_decision,
                decision_changed,
                order_count_changed,
                side_changed,
                order_type_changed,
                execution_profile_changed,
                size_delta,
                confidence_delta,
                simulated_fill_ratio,
                simulated_slippage_bps,
                simulated_latency_seconds,
                rationale_keywords_added,
                rationale_keywords_removed,
                evidence_signals_added,
                evidence_signals_removed,
                evidence_signal_count_delta,
                payload
            FROM replay_runs
            WHERE audit_log_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (audit_log_id, limit),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["rationale_keywords_added"] = self._safe_json_loads(
                item.get("rationale_keywords_added"),
                default=[],
            )
            item["rationale_keywords_removed"] = self._safe_json_loads(
                item.get("rationale_keywords_removed"),
                default=[],
            )
            item["evidence_signals_added"] = self._safe_json_loads(
                item.get("evidence_signals_added"),
                default=[],
            )
            item["evidence_signals_removed"] = self._safe_json_loads(
                item.get("evidence_signals_removed"),
                default=[],
            )
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_recent_order_state_transitions(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                client_order_id,
                exchange_order_id,
                from_state,
                to_state,
                event_type,
                payload
            FROM order_state_transitions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_order_state_transitions_for_audit(self, audit_log_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                client_order_id,
                exchange_order_id,
                from_state,
                to_state,
                event_type,
                payload
            FROM order_state_transitions
            WHERE audit_log_id = ?
            ORDER BY id ASC
            """,
            (audit_log_id,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_recent_position_state_transitions(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                from_state,
                to_state,
                event_type,
                payload
            FROM position_state_transitions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def get_position_state_transitions_for_audit(self, audit_log_id: int) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                audit_log_id,
                created_at,
                symbol,
                from_state,
                to_state,
                event_type,
                payload
            FROM position_state_transitions
            WHERE audit_log_id = ?
            ORDER BY id ASC
            """,
            (audit_log_id,),
        )
        rows = []
        for row in cursor.fetchall():
            item = dict(row)
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def _decode_metrics_row(self, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return self._format_memory_row(dict(row))

    def get_risk_state(
        self,
        current_balance: float,
        cooldown_minutes: int,
        cooldown_config: Optional[Dict[str, Any]] = None,
        drawdown_activate_pct: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        now = now or self._utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_start_balance = self._get_daily_start_balance(day_start) or current_balance
        daily_pnl_pct = 0.0
        if daily_start_balance > 0:
            daily_pnl_pct = ((current_balance - daily_start_balance) / daily_start_balance) * 100
        drawdown_active = False
        if drawdown_activate_pct is not None:
            drawdown_active = daily_pnl_pct <= -abs(float(drawdown_activate_pct))

        recent_losses = self._get_recent_consecutive_losses()
        dynamic_config = self._build_dynamic_cooldown_config(
            cooldown_minutes=cooldown_minutes,
            cooldown_config=cooldown_config,
        )
        last_loss_record = self._get_last_loss_record()
        cooldown_until = last_loss_record.get("timestamp")
        cooldown_active = False
        cooldown_remaining_minutes = 0
        last_loss_estimated_pnl_pct = last_loss_record.get("estimated_pnl_pct")
        failure_pattern = self._get_recent_failure_pattern(
            lookback=int(dynamic_config.get("repeated_failure_lookback", 5) or 5),
            minimum_count=int(dynamic_config.get("repeated_failure_min_count", 2) or 2),
        )
        failure_pattern_score = self._score_failure_pattern(
            consecutive_losses=recent_losses,
            last_loss_estimated_pnl_pct=last_loss_estimated_pnl_pct,
            dominant_failure_count=int(failure_pattern.get("count") or 0),
            dynamic_config=dynamic_config,
        )
        cooldown_minutes_effective = int(dynamic_config.get("base_minutes", cooldown_minutes) or cooldown_minutes)
        cooldown_components: List[str] = []
        abs_last_loss_pct = abs(float(last_loss_estimated_pnl_pct or 0.0))
        if abs_last_loss_pct >= float(dynamic_config.get("medium_loss_pct", 0.5) or 0.5):
            cooldown_minutes_effective += int(dynamic_config.get("medium_loss_add_minutes", 0) or 0)
            cooldown_components.append("medium_loss")
        if abs_last_loss_pct >= float(dynamic_config.get("large_loss_pct", 1.0) or 1.0):
            cooldown_minutes_effective += int(dynamic_config.get("large_loss_add_minutes", 0) or 0)
            cooldown_components.append("large_loss")
        if abs_last_loss_pct >= float(dynamic_config.get("severe_loss_pct", 2.0) or 2.0):
            cooldown_minutes_effective += int(dynamic_config.get("severe_loss_add_minutes", 0) or 0)
            cooldown_components.append("severe_loss")
        if recent_losses >= 3:
            cooldown_minutes_effective += int(
                dynamic_config.get("consecutive_loss_3_plus_add_minutes", 0) or 0
            )
            cooldown_components.append("consecutive_losses_3_plus")
        elif recent_losses >= 2:
            cooldown_minutes_effective += int(
                dynamic_config.get("consecutive_loss_2_add_minutes", 0) or 0
            )
            cooldown_components.append("consecutive_losses_2")
        if bool(failure_pattern.get("active")):
            cooldown_minutes_effective += int(dynamic_config.get("repeated_failure_add_minutes", 0) or 0)
            cooldown_components.append("repeated_failure")
        cooldown_minutes_effective = max(
            1,
            min(
                int(dynamic_config.get("max_minutes", cooldown_minutes_effective) or cooldown_minutes_effective),
                cooldown_minutes_effective,
            ),
        )
        if cooldown_until is not None:
            cooldown_until = cooldown_until + timedelta(minutes=cooldown_minutes_effective)
            if cooldown_until > now:
                cooldown_active = True
                cooldown_remaining_minutes = max(
                    1,
                    int((cooldown_until - now).total_seconds() // 60),
                )

        return {
            "daily_start_balance": daily_start_balance,
            "daily_pnl_pct": daily_pnl_pct,
            "drawdown_active": drawdown_active,
            "drawdown_activate_pct": drawdown_activate_pct,
            "consecutive_losses": recent_losses,
            "last_loss_estimated_pnl_pct": last_loss_estimated_pnl_pct,
            "cooldown_minutes_effective": cooldown_minutes_effective,
            "cooldown_components": cooldown_components,
            "cooldown_active": cooldown_active,
            "cooldown_remaining_minutes": cooldown_remaining_minutes,
            "failure_pattern_score": failure_pattern_score,
            "dominant_failure_reason": failure_pattern.get("reason"),
            "dominant_failure_count": int(failure_pattern.get("count") or 0),
        }

    def get_symbol_trigger_state(self, symbol: str) -> Dict[str, Any]:
        cursor = self.conn.execute(
            """
            SELECT payload
            FROM trigger_state
            WHERE symbol = ?
            """,
            (symbol,),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        return self._safe_json_loads(row["payload"], default={})

    def save_symbol_trigger_state(self, symbol: str, state: Dict[str, Any]):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO trigger_state (symbol, updated_at, payload)
                    VALUES (?, CURRENT_TIMESTAMP, ?)
                    ON CONFLICT(symbol) DO UPDATE SET
                        updated_at = CURRENT_TIMESTAMP,
                        payload = excluded.payload
                    """,
                    (
                        symbol,
                        json.dumps(state or {}),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to save trigger state for {symbol}: {e}")

    def get_cached_decision(self, prompt_hash: str) -> Optional[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT
                prompt_hash,
                model_name,
                input_hash,
                raw_response,
                intent,
                created_at,
                last_used_at,
                hit_count
            FROM decision_cache
            WHERE prompt_hash = ?
            """,
            (prompt_hash,),
        )
        row = cursor.fetchone()
        if not row:
            return None

        with self.conn:
            self.conn.execute(
                """
                UPDATE decision_cache
                SET
                    last_used_at = CURRENT_TIMESTAMP,
                    hit_count = hit_count + 1
                WHERE prompt_hash = ?
                """,
                (prompt_hash,),
            )

        item = dict(row)
        item["intent"] = self._safe_json_loads(item.get("intent"), default={})
        item["hit_count"] = int(item.get("hit_count") or 0) + 1
        return item

    def save_cached_decision(
        self,
        prompt_hash: str,
        model_name: str,
        input_hash: str,
        raw_response: str,
        intent: Dict[str, Any],
    ):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO decision_cache (
                        prompt_hash,
                        model_name,
                        input_hash,
                        raw_response,
                        intent,
                        created_at,
                        last_used_at,
                        hit_count
                    ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
                    ON CONFLICT(prompt_hash) DO UPDATE SET
                        model_name = excluded.model_name,
                        input_hash = excluded.input_hash,
                        raw_response = excluded.raw_response,
                        intent = excluded.intent,
                        last_used_at = CURRENT_TIMESTAMP
                    """,
                    (
                        prompt_hash,
                        model_name,
                        input_hash,
                        raw_response,
                        json.dumps(intent),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to save cached decision for {prompt_hash}: {e}")

    def _get_daily_start_balance(self, day_start: datetime) -> Optional[float]:
        cursor = self.conn.execute(
            """
            SELECT account_state
            FROM audit_logs
            WHERE timestamp >= ?
            ORDER BY id ASC
            LIMIT 1
            """,
            (day_start.strftime("%Y-%m-%d %H:%M:%S"),),
        )
        row = cursor.fetchone()
        if not row or not row["account_state"]:
            return None
        try:
            payload = json.loads(row["account_state"])
            return float(payload.get("total_balance", 0.0))
        except Exception:
            return None

    def _get_recent_consecutive_losses(self) -> int:
        cursor = self.conn.execute(
            """
            SELECT loss_signal
            FROM journal
            WHERE estimated_pnl_pct IS NOT NULL
            ORDER BY id DESC
            LIMIT 20
            """
        )
        streak = 0
        for row in cursor.fetchall():
            if int(row["loss_signal"] or 0) == 1:
                streak += 1
            else:
                break
        return streak

    def _build_dynamic_cooldown_config(
        self,
        cooldown_minutes: int,
        cooldown_config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        base_cooldown = max(1, int(cooldown_minutes or 1))
        config = {
            "enabled": True,
            "base_minutes": 15,
            "medium_loss_pct": 0.5,
            "medium_loss_add_minutes": 10,
            "large_loss_pct": 1.0,
            "large_loss_add_minutes": 15,
            "severe_loss_pct": 2.0,
            "severe_loss_add_minutes": 15,
            "consecutive_loss_2_add_minutes": 15,
            "consecutive_loss_3_plus_add_minutes": 30,
            "repeated_failure_lookback": 5,
            "repeated_failure_min_count": 2,
            "repeated_failure_add_minutes": 15,
            "max_minutes": max(base_cooldown, 90),
        }
        config.update(cooldown_config or {})
        enabled = bool(config.get("enabled", True))
        if not enabled:
            config["base_minutes"] = base_cooldown
            config["max_minutes"] = base_cooldown
        config["base_minutes"] = max(1, int(config.get("base_minutes", base_cooldown) or base_cooldown))
        config["repeated_failure_lookback"] = max(
            1,
            int(config.get("repeated_failure_lookback", 5) or 5),
        )
        config["repeated_failure_min_count"] = max(
            2,
            int(config.get("repeated_failure_min_count", 2) or 2),
        )
        config["max_minutes"] = max(
            config["base_minutes"],
            int(config.get("max_minutes", max(base_cooldown, config["base_minutes"])) or max(base_cooldown, config["base_minutes"])),
        )
        return config

    def _get_last_loss_record(self) -> Dict[str, Any]:
        cursor = self.conn.execute(
            """
            SELECT timestamp, estimated_pnl_pct, risk_reason
            FROM journal
            WHERE loss_signal = 1
            ORDER BY id DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        if not row:
            return {}
        timestamp = None
        if row["timestamp"]:
            try:
                timestamp = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                timestamp = None
        estimated_pnl_pct = None
        if row["estimated_pnl_pct"] is not None:
            try:
                estimated_pnl_pct = float(row["estimated_pnl_pct"])
            except (TypeError, ValueError):
                estimated_pnl_pct = None
        return {
            "timestamp": timestamp,
            "estimated_pnl_pct": estimated_pnl_pct,
            "risk_reason": row["risk_reason"],
        }

    def _get_recent_failure_pattern(self, lookback: int = 5, minimum_count: int = 2) -> Dict[str, Any]:
        cursor = self.conn.execute(
            """
            SELECT thesis_failure_reason
            FROM trade_cases
            WHERE (
                (estimated_pnl_pct IS NOT NULL AND estimated_pnl_pct < 0)
                OR (realized_pnl IS NOT NULL AND realized_pnl < 0)
            )
            ORDER BY COALESCE(closed_at, opened_at) DESC, trade_case_id DESC
            LIMIT ?
            """,
            (max(1, int(lookback or 1)),),
        )
        reasons = []
        for row in cursor.fetchall():
            reason = str(row["thesis_failure_reason"] or "").strip()
            if reason:
                reasons.append(reason)
        if not reasons:
            cursor = self.conn.execute(
                """
                SELECT risk_reason
                FROM journal
                WHERE loss_signal = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, int(lookback or 1)),),
            )
            for row in cursor.fetchall():
                reason = str(row["risk_reason"] or "").strip()
                if reason:
                    reasons.append(reason)
        counts: Dict[str, int] = {}
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
        if not counts:
            return {"active": False, "reason": None, "count": 0}
        dominant_reason, dominant_count = max(counts.items(), key=lambda item: (item[1], item[0]))
        return {
            "active": dominant_count >= max(2, int(minimum_count or 2)),
            "reason": dominant_reason,
            "count": dominant_count,
        }

    def _score_failure_pattern(
        self,
        consecutive_losses: int,
        last_loss_estimated_pnl_pct: Optional[float],
        dominant_failure_count: int,
        dynamic_config: Dict[str, Any],
    ) -> float:
        score = 0.0
        score += min(max(int(consecutive_losses or 0), 0), 3) / 3.0 * 0.5
        if dominant_failure_count >= int(dynamic_config.get("repeated_failure_min_count", 2) or 2):
            score += min(dominant_failure_count, 3) / 3.0 * 0.3
        abs_loss_pct = abs(float(last_loss_estimated_pnl_pct or 0.0))
        large_loss_pct = max(0.0001, float(dynamic_config.get("large_loss_pct", 1.0) or 1.0))
        severe_loss_pct = max(large_loss_pct, float(dynamic_config.get("severe_loss_pct", 2.0) or 2.0))
        if abs_loss_pct > 0:
            if abs_loss_pct >= severe_loss_pct:
                score += 0.2
            else:
                score += min(abs_loss_pct / severe_loss_pct, 1.0) * 0.2
        return round(min(1.0, score), 4)

    def _get_last_loss_timestamp(self) -> Optional[datetime]:
        record = self._get_last_loss_record()
        timestamp = record.get("timestamp")
        if isinstance(timestamp, datetime):
            return timestamp
        return None

    def _normalize_order_state(self, value: Any) -> str:
        state = str(value or "unknown").strip().lower()
        aliases = {
            "none": "none",
            "deferred": "deferred",
            "submitted": "submitted",
            "cancel_requested": "cancel_requested",
            "partial": "partial_filled",
            "partial_filled": "partial_filled",
            "partially_filled": "partial_filled",
            "partiallyfilled": "partial_filled",
            "completed": "completed",
            "filled": "completed",
            "closed": "completed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
            "failed": "failed",
            "rejected": "failed",
            "unknown": "unknown",
        }
        return aliases.get(state, "unknown")

    def _is_valid_order_state_transition(self, from_state: str, to_state: str) -> bool:
        normalized_from = self._normalize_order_state(from_state)
        normalized_to = self._normalize_order_state(to_state)
        allowed = {
            "none": {"deferred", "submitted", "unknown"},
            "deferred": {"deferred", "submitted", "failed", "unknown"},
            "cancel_requested": {"cancel_requested", "cancelled", "failed", "unknown"},
            "submitted": {"submitted", "cancel_requested", "partial_filled", "completed", "cancelled", "failed", "unknown"},
            "partial_filled": {"partial_filled", "cancel_requested", "completed", "cancelled", "failed", "unknown"},
            "unknown": {"unknown", "submitted", "cancel_requested", "partial_filled", "completed", "cancelled", "failed"},
            "cancelled": {"cancelled"},
            "failed": {"failed"},
            "completed": {"completed"},
        }
        return normalized_to in allowed.get(normalized_from, {"unknown"})

    def _upsert_order_runtime_state(
        self,
        order_key: Optional[str],
        client_order_id: Optional[str],
        exchange_order_id: Optional[str],
        symbol: Optional[str],
        replace_group_id: Optional[str],
        lineage_role: Optional[str],
        lineage_parent_order_id: Optional[str],
        lineage_parent_client_order_id: Optional[str],
        state: str,
        audit_log_id: int,
        payload: Dict[str, Any],
    ):
        if not order_key:
            return
        self.conn.execute(
            """
            INSERT INTO order_runtime_state (
                order_key,
                client_order_id,
                exchange_order_id,
                symbol,
                replace_group_id,
                lineage_role,
                lineage_parent_order_id,
                lineage_parent_client_order_id,
                state,
                updated_at,
                last_audit_log_id,
                payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
            ON CONFLICT(order_key) DO UPDATE SET
                client_order_id = excluded.client_order_id,
                exchange_order_id = excluded.exchange_order_id,
                symbol = excluded.symbol,
                replace_group_id = excluded.replace_group_id,
                lineage_role = excluded.lineage_role,
                lineage_parent_order_id = excluded.lineage_parent_order_id,
                lineage_parent_client_order_id = excluded.lineage_parent_client_order_id,
                state = excluded.state,
                updated_at = CURRENT_TIMESTAMP,
                last_audit_log_id = excluded.last_audit_log_id,
                payload = excluded.payload
            """,
            (
                order_key,
                client_order_id,
                exchange_order_id,
                symbol,
                replace_group_id,
                lineage_role,
                lineage_parent_order_id,
                lineage_parent_client_order_id,
                self._normalize_order_state(state),
                audit_log_id,
                json.dumps(payload or {}),
            ),
        )

    def _utcnow(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    @staticmethod
    def is_learning_eligible_audit_record(row: Dict[str, Any]) -> bool:
        record = dict(row or {})
        risk_reason = str(record.get("risk_reason") or "").strip().lower()
        if risk_reason.startswith("error in "):
            return False

        execution_summary = record.get("execution_summary") or {}
        if hasattr(execution_summary, "model_dump"):
            execution_summary = execution_summary.model_dump(mode="json")
        if not isinstance(execution_summary, dict):
            execution_summary = {}
        if str(execution_summary.get("error") or "").strip():
            return False

        ai_usage = execution_summary.get("ai_usage") or {}
        if hasattr(ai_usage, "model_dump"):
            ai_usage = ai_usage.model_dump(mode="json")
        if not isinstance(ai_usage, dict):
            ai_usage = {}
        if bool(ai_usage.get("validation_error")) or bool(ai_usage.get("semantic_validation_error")):
            return False

        intent = record.get("intent") or {}
        if hasattr(intent, "model_dump"):
            intent = intent.model_dump(mode="json")
        if not isinstance(intent, dict):
            intent = {}

        text_markers = (
            "system error",
            "structured rationale validation failed",
            "fallback due to json validation error from ai",
            "fallback due to invalid structured rationale from ai",
            "loop error during",
        )
        text_values: List[str] = []
        for note in intent.get("risk_notes") or []:
            text_values.append(str(note or "").strip().lower())
        rationale = intent.get("rationale")
        if rationale is None:
            rationale = record.get("rationale")
        if isinstance(rationale, list):
            text_values.extend(str(item or "").strip().lower() for item in rationale)
        elif rationale:
            text_values.append(str(rationale).strip().lower())
        for text in text_values:
            if any(marker in text for marker in text_markers):
                return False
        return True

    def _decode_audit_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        decoded = dict(row)
        defaults = {
            "account_state": {},
            "market_state": {},
            "market_vectors": {},
            "intent": {},
            "receipts": [],
            "risk_profile": {},
            "decision_request": {},
            "execution_summary": {},
        }
        for key, default in defaults.items():
            decoded[key] = self._safe_json_loads(decoded.get(key), default=default)
        return decoded

    def _format_memory_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        metrics = self._safe_json_loads(item.get("metrics"), default={})
        apply_when = metrics.get("apply_when") or {
            "regime": [metrics.get("regime")] if metrics.get("regime") else [],
            "volatility": [],
            "trend": [],
        }
        ignore_when = metrics.get("ignore_when") or [
            "No explicit ignore condition stored; treat as low-priority guidance."
        ]
        item["metrics"] = metrics
        item["category"] = metrics.get("category", "lesson")
        item["status"] = metrics.get("status", "rule")
        item["support"] = int(metrics.get("support") or 0)
        item["regime"] = metrics.get("regime", "general")
        item["apply_when"] = apply_when
        item["ignore_when"] = ignore_when
        item["evidence_journal_ids"] = metrics.get("evidence_journal_ids") or []
        item["confidence_score"] = float(metrics.get("confidence_score") or 0.0)
        item["validity_score"] = float(metrics.get("validity_score") or item["confidence_score"] or 0.0)
        item["decay_score"] = float(metrics.get("decay_score") or 0.0)
        item["freshness_score"] = float(metrics.get("freshness_score") or 0.0)
        item["lifecycle_status"] = metrics.get("lifecycle_status") or ("retired" if metrics.get("retired") else "active")
        item["retired"] = bool(metrics.get("retired"))
        item["retirement_reason"] = metrics.get("retirement_reason")
        item["canonical_key"] = metrics.get("canonical_key")
        item["merge_count"] = int(metrics.get("merge_count") or 0)
        item["revive_count"] = int(metrics.get("revive_count") or 0)
        return item

    def _format_journal_summary(self, row: Dict[str, Any]) -> str:
        decision = row.get("decision") or "UNKNOWN"
        rationale_value = row.get("rationale") or []
        if isinstance(rationale_value, list):
            rationale_text = " | ".join(str(item) for item in rationale_value if item)
        else:
            rationale_text = str(rationale_value).strip()
        rationale_json = row.get("rationale_json") or row.get("rationale_items") or []
        risk_reason = (row.get("risk_reason") or "").strip()
        estimated_pnl_pct = row.get("estimated_pnl_pct")
        ledger_net_realized_pnl = row.get("ledger_net_realized_pnl")
        ledger_fee_cost = row.get("ledger_fee_cost")
        risk_mode = row.get("risk_mode")
        risk_approved = row.get("risk_approved")
        trigger_symbols = row.get("trigger_symbols") or []
        risk_trigger_symbols = row.get("risk_trigger_symbols") or []
        evidence_quality = row.get("evidence_quality") or {}
        entry_size_cap = row.get("entry_size_cap") or {}
        evidence_parts = []
        for item in rationale_json[:2]:
            evidence = (item or {}).get("evidence") or []
            if not evidence:
                continue
            first = evidence[0]
            signal = first.get("signal") or "signal"
            timeframe = first.get("timeframe") or "context"
            observed = first.get("observed_value")
            operator = first.get("operator") or "note"
            threshold = first.get("threshold")
            if threshold is None:
                evidence_parts.append(f"{signal}@{timeframe}={observed}")
            else:
                evidence_parts.append(f"{signal}@{timeframe}={observed} {operator} {threshold}")

        parts = [f"{decision}:"]
        if rationale_text:
            parts.append(rationale_text.replace("\n", " | "))
        if evidence_parts:
            parts.append(f"evidence={' ; '.join(evidence_parts)}")
        if risk_reason:
            parts.append(f"risk={risk_reason}")
        if risk_mode:
            parts.append(f"mode={risk_mode}")
        if risk_approved is not None:
            parts.append(f"approved={bool(risk_approved)}")
        if estimated_pnl_pct is not None:
            parts.append(f"pnl={float(estimated_pnl_pct):.2f}%")
        if ledger_net_realized_pnl is not None:
            parts.append(f"ledger_net={float(ledger_net_realized_pnl):.4f}")
        if ledger_fee_cost is not None:
            parts.append(f"fees={float(ledger_fee_cost):.4f}")
        if evidence_quality:
            parts.append(f"eq={float(evidence_quality.get('factor') or 0.0):.2f}")
        if entry_size_cap:
            parts.append(
                "cap="
                f"{float(entry_size_cap.get('base_cap_pct') or 0.0):.1f}"
                "->"
                f"{float(entry_size_cap.get('effective_cap_pct') or 0.0):.1f}"
            )
        if trigger_symbols:
            parts.append(f"trigger={','.join(trigger_symbols)}")
        if risk_trigger_symbols:
            parts.append(f"risk_trigger={','.join(risk_trigger_symbols)}")
        case_summary = str(row.get("case_summary") or "").strip()
        if case_summary:
            parts.append(f"cases={case_summary}")
        return " ".join(parts)

    def _decode_journal_context_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        item["rationale_json"] = self._safe_json_loads(item.get("rationale_json"), default=[])
        item["market_state"] = self._safe_json_loads(item.get("market_state"), default={})
        item["execution_summary"] = self._safe_json_loads(item.get("execution_summary"), default={})
        item["trade_case_ids"] = self._safe_json_loads(item.get("trade_case_ids"), default=[])
        item["case_statuses"] = self._safe_json_loads(item.get("case_statuses"), default={})
        rationale_text = item.get("rationale") or ""
        item["rationale"] = [line for line in str(rationale_text).split("\n") if line]
        item["rationale_items"] = item["rationale_json"]
        item["journal_id"] = int(item.get("id") or 0)
        item["trigger_symbols"] = (item["market_state"].get("trigger_symbols") or [])
        item["risk_trigger_symbols"] = (item["market_state"].get("risk_trigger_symbols") or [])
        item["evidence_quality"] = item["execution_summary"].get("evidence_quality") or {}
        item["entry_size_cap"] = item["execution_summary"].get("entry_size_cap") or {}
        item["summary"] = self._format_journal_summary(item)
        return item

    def _decode_symbol_journal_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        item["evidence_signals"] = self._safe_json_loads(item.get("evidence_signals"), default=[])
        item["payload"] = self._safe_json_loads(item.get("payload"), default={})
        return item

    def _decode_trade_case_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        item["evidence_signals"] = self._safe_json_loads(item.get("evidence_signals"), default=[])
        item["trigger_reasons"] = self._safe_json_loads(item.get("trigger_reasons"), default=[])
        item["risk_trigger_reasons"] = self._safe_json_loads(item.get("risk_trigger_reasons"), default=[])
        item["feature_vector"] = self._safe_json_loads(item.get("feature_vector"), default=[])
        item["payload"] = self._safe_json_loads(item.get("payload"), default={})
        return item

    def _decode_trade_case_vector_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        item["feature_vector"] = self._safe_json_loads(item.get("feature_vector"), default=[])
        item["payload"] = self._safe_json_loads(item.get("payload"), default={})
        item["matched_band_count"] = int(item.get("matched_band_count") or 0)
        return item

    def _vector_norm(self, vector: Any) -> float:
        values = vector if isinstance(vector, list) else []
        total = 0.0
        for value in values:
            try:
                total += float(value) * float(value)
            except (TypeError, ValueError):
                continue
        return total ** 0.5

    def _coerce_vector_values(self, vector: Any) -> List[float]:
        values = vector if isinstance(vector, list) else []
        normalized: List[float] = []
        for value in values:
            try:
                normalized.append(float(value))
            except (TypeError, ValueError):
                normalized.append(0.0)
        return normalized

    def _build_vector_band_hashes(
        self,
        feature_vector: Any,
        band_count: int = 4,
        dims_per_band: int = 4,
    ) -> List[tuple[int, str]]:
        values = self._coerce_vector_values(feature_vector)
        if not values:
            return []
        norm = self._vector_norm(values) or 1.0
        unit_values = [value / norm for value in values]
        required_dims = band_count * dims_per_band
        if len(unit_values) < required_dims:
            unit_values = unit_values + ([0.0] * (required_dims - len(unit_values)))
        else:
            unit_values = unit_values[:required_dims]
        hashes: List[tuple[int, str]] = []
        for band_index in range(band_count):
            chunk = unit_values[band_index::band_count][:dims_per_band]
            if len(chunk) < dims_per_band:
                chunk = chunk + ([0.0] * (dims_per_band - len(chunk)))
            quantized = []
            for value in chunk:
                bucket = max(-6, min(6, int(round(value / 0.2))))
                quantized.append(str(bucket))
            digest = hashlib.sha1("|".join(quantized).encode("utf-8")).hexdigest()[:16]
            hashes.append((band_index, digest))
        return hashes

    def _build_trade_case_vector_band_entries(
        self,
        trade_case_id: str,
        symbol: str,
        feature_vector: Any,
        closed_at: Any = None,
    ) -> List[tuple[Any, ...]]:
        if not trade_case_id or not symbol:
            return []
        band_hashes = self._build_vector_band_hashes(feature_vector)
        return [
            (
                trade_case_id,
                symbol,
                band_index,
                band_hash,
                closed_at,
            )
            for band_index, band_hash in band_hashes
        ]

    def _safe_json_loads(self, value: Any, default: Any):
        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except Exception:
            return default
