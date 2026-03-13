import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from dotenv import load_dotenv

from ai.llm_provider import (
    normalize_provider_name,
    resolve_decision_models,
    resolve_memory_model_name,
)
from db.state_store import StateStore
from tools.replay_audit import build_replay_engine, replay_and_optionally_save

app = Flask(__name__)
DEFAULT_DB_PATH = str(Path(__file__).resolve().parents[1] / "data" / "gpt_trader.db")
_env_loaded = False


def _resolve_db_path() -> str:
    configured = os.getenv("DB_PATH", "").strip()
    return configured or DEFAULT_DB_PATH


state_store = None


def _ensure_dashboard_environment() -> None:
    global _env_loaded
    if not _env_loaded:
        load_dotenv()
        _env_loaded = True


def _get_state_store():
    global state_store
    if state_store is None:
        _ensure_dashboard_environment()
        state_store = StateStore(_resolve_db_path())
    return state_store


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _build_trade_case_summary(rows):
    total = len(rows)
    closed = [row for row in rows if str(row.get("status") or "").lower() == "closed"]
    active = [row for row in rows if str(row.get("status") or "").lower() != "closed"]
    wins = [
        row for row in closed
        if _safe_float(row.get("realized_pnl"), default=_safe_float(row.get("estimated_pnl_pct"))) > 0
    ]
    avg_holding = 0.0
    holding_values = [_safe_float(row.get("holding_minutes"), default=None) for row in closed]
    holding_values = [value for value in holding_values if value is not None]
    if holding_values:
        avg_holding = sum(holding_values) / len(holding_values)
    return {
        "total_count": total,
        "closed_count": len(closed),
        "active_count": len(active),
        "win_count": len(wins),
        "win_rate": round((len(wins) / len(closed) * 100), 2) if closed else 0.0,
        "avg_holding_minutes": round(avg_holding, 2),
    }


def _build_retrieval_summary(rows):
    total = len(rows)
    settled = [row for row in rows if row.get("outcome_status") or row.get("outcome_trade_case_id")]
    aligned = [row for row in rows if bool(row.get("decision_symbol_aligned"))]
    positive = [
        row for row in settled
        if _safe_float(
            row.get("outcome_realized_pnl"),
            default=_safe_float(row.get("outcome_estimated_pnl_pct")),
        ) > 0
    ]
    similarity_values = [_safe_float(row.get("similarity"), default=None) for row in rows]
    similarity_values = [value for value in similarity_values if value is not None]
    final_score_values = [_safe_float(row.get("final_score"), default=None) for row in rows]
    final_score_values = [value for value in final_score_values if value is not None]
    return {
        "total_count": total,
        "settled_count": len(settled),
        "aligned_count": len(aligned),
        "positive_count": len(positive),
        "avg_similarity": round(sum(similarity_values) / len(similarity_values), 4) if similarity_values else 0.0,
        "avg_final_score": round(sum(final_score_values) / len(final_score_values), 4) if final_score_values else 0.0,
    }


def _build_memory_summary(rows):
    total = len(rows)
    retired = [row for row in rows if bool(row.get("retired"))]
    active = [row for row in rows if not bool(row.get("retired"))]
    high_confidence = [row for row in active if _safe_float(row.get("confidence_score")) >= 0.6]
    revived = [row for row in rows if int(row.get("revive_count") or 0) > 0]
    return {
        "total_count": total,
        "active_count": len(active),
        "retired_count": len(retired),
        "high_confidence_count": len(high_confidence),
        "revived_count": len(revived),
    }


def _build_execution_contract_summary(rows):
    adjustment_counts = {}
    audits_affected = set()
    total_adjustments = 0
    governed_orders = 0
    immediate_entry_orders = 0
    reduce_exit_orders = 0
    for row in rows or []:
        execution_summary = row.get("execution_summary") or {}
        execution_policy = execution_summary.get("execution_policy") or {}
        orders = execution_policy.get("orders") or []
        for order in orders:
            action_type = str((order or {}).get("action_type") or "").strip().lower()
            if action_type in {"entry", "reduce", "exit"}:
                governed_orders += 1
            if action_type == "entry" and str((order or {}).get("entry_style") or "").strip().lower() == "immediate":
                immediate_entry_orders += 1
            if action_type in {"reduce", "exit"}:
                reduce_exit_orders += 1
            adjustment = str((order or {}).get("contract_adjustment") or "").strip()
            if not adjustment:
                continue
            total_adjustments += 1
            audit_id = row.get("id") or row.get("audit_log_id")
            if audit_id is not None:
                audits_affected.add(int(audit_id))
            adjustment_counts[adjustment] = adjustment_counts.get(adjustment, 0) + 1
    top_adjustment = ""
    top_count = 0
    if adjustment_counts:
        top_adjustment, top_count = max(adjustment_counts.items(), key=lambda item: item[1])
    return {
        "total_adjustments": total_adjustments,
        "audits_affected": len(audits_affected),
        "governed_orders": governed_orders,
        "immediate_entry_orders": immediate_entry_orders,
        "reduce_exit_orders": reduce_exit_orders,
        "adjustment_rate": round(total_adjustments / governed_orders, 4) if governed_orders else 0.0,
        "top_adjustment": top_adjustment,
        "top_adjustment_count": top_count,
        "adjustment_counts": dict(sorted(adjustment_counts.items())),
    }


def _enrich_execution_evaluation_rows(rows, store):
    enriched = []
    trade_case_cache = {}
    for row in rows or []:
        item = dict(row)
        payload = item.get("payload") or {}
        trade_case_id = str(item.get("outcome_trade_case_id") or "").strip()
        trade_case = {}
        if trade_case_id and hasattr(store, "get_trade_case"):
            trade_case = trade_case_cache.get(trade_case_id)
            if trade_case is None:
                trade_case = store.get_trade_case(trade_case_id) or {}
                trade_case_cache[trade_case_id] = trade_case
        resolved_pnl_pct = trade_case.get("estimated_pnl_pct")
        if resolved_pnl_pct is None:
            resolved_pnl_pct = item.get("outcome_estimated_pnl_pct")
        resolved_label = str(item.get("outcome_label") or payload.get("outcome_label") or "").strip().lower()
        resolved_status = str(trade_case.get("status") or payload.get("case_status") or "").strip().lower()
        item["entry_style"] = str(item.get("entry_style") or payload.get("entry_style") or "").strip().lower() or None
        item["resolved_outcome_pnl_pct"] = resolved_pnl_pct
        item["resolved_outcome_label"] = resolved_label or None
        item["resolved_outcome_status"] = resolved_status or None
        item["resolved_thesis_failure_reason"] = trade_case.get("thesis_failure_reason")
        enriched.append(item)
    return enriched


def _build_entry_style_summary(rows):
    styles = {
        "immediate": {
            "style": "immediate",
            "entry_count": 0,
            "settled_count": 0,
            "win_count": 0,
            "avg_pnl_pct": 0.0,
            "no_fill_count": 0,
            "no_fill_rate": 0.0,
            "missed_move_count": 0,
            "missed_move_rate": 0.0,
        },
        "retest": {
            "style": "retest",
            "entry_count": 0,
            "settled_count": 0,
            "win_count": 0,
            "avg_pnl_pct": 0.0,
            "no_fill_count": 0,
            "no_fill_rate": 0.0,
            "missed_move_count": 0,
            "missed_move_rate": 0.0,
        },
        "value": {
            "style": "value",
            "entry_count": 0,
            "settled_count": 0,
            "win_count": 0,
            "avg_pnl_pct": 0.0,
            "no_fill_count": 0,
            "no_fill_rate": 0.0,
            "missed_move_count": 0,
            "missed_move_rate": 0.0,
        },
    }
    pnl_buckets = {key: [] for key in styles}
    for row in rows or []:
        style = str(row.get("entry_style") or "").strip().lower()
        if style not in styles:
            continue
        bucket = styles[style]
        bucket["entry_count"] += 1
        if _safe_float(row.get("filled_quantity"), default=0.0) <= 0:
            bucket["no_fill_count"] += 1
        if row.get("resolved_outcome_label") == "missed_due_to_no_fill":
            bucket["missed_move_count"] += 1
        pnl_pct = row.get("resolved_outcome_pnl_pct")
        if pnl_pct is not None:
            pnl_value = _safe_float(pnl_pct, default=None)
            if pnl_value is not None:
                bucket["settled_count"] += 1
                pnl_buckets[style].append(pnl_value)
                if pnl_value > 0:
                    bucket["win_count"] += 1
    for style, bucket in styles.items():
        entry_count = bucket["entry_count"]
        settled_count = bucket["settled_count"]
        bucket["win_rate"] = round((bucket["win_count"] / settled_count) * 100, 2) if settled_count else 0.0
        bucket["avg_pnl_pct"] = round(sum(pnl_buckets[style]) / len(pnl_buckets[style]), 4) if pnl_buckets[style] else 0.0
        bucket["no_fill_rate"] = round(bucket["no_fill_count"] / entry_count, 4) if entry_count else 0.0
        bucket["missed_move_rate"] = round(bucket["missed_move_count"] / entry_count, 4) if entry_count else 0.0
    active_styles = [bucket for bucket in styles.values() if bucket["entry_count"] > 0]
    top_style = max(active_styles, key=lambda item: item["entry_count"])["style"] if active_styles else ""
    return {
        "total_entries": sum(bucket["entry_count"] for bucket in styles.values()),
        "settled_entries": sum(bucket["settled_count"] for bucket in styles.values()),
        "styles": [styles["immediate"], styles["retest"], styles["value"]],
        "top_style": top_style,
    }


def _build_post_trade_outcome_summary(rows):
    label_counts = {
        "edge_failed": 0,
        "missed_due_to_no_fill": 0,
        "risk_exit": 0,
        "stale_entry_cancelled": 0,
    }
    for row in rows or []:
        label = str(row.get("resolved_outcome_label") or "").strip().lower()
        if label in label_counts:
            label_counts[label] += 1
    total_labeled = sum(label_counts.values())
    top_label = ""
    top_count = 0
    if total_labeled:
        top_label, top_count = max(label_counts.items(), key=lambda item: item[1])
    return {
        "total_labeled": total_labeled,
        "top_label": top_label,
        "top_count": top_count,
        "label_counts": label_counts,
    }


def _parse_dashboard_timestamp(value):
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text, text.replace("Z", "+00:00"), text.replace(" ", "T")]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC).replace(tzinfo=None)
            return parsed
        except ValueError:
            continue
    return None


def _latest_full_audit(store):
    rows = store.get_recent_audits(limit=1)
    if not rows:
        return {}
    audit_id = rows[0].get("id")
    if audit_id is None or not hasattr(store, "get_audit_log"):
        return {}
    return store.get_audit_log(int(audit_id)) or {}


def _build_live_protection_summary(latest_audit, now: datetime):
    account_state = (latest_audit or {}).get("account_state") or {}
    positions = list(account_state.get("positions") or [])
    open_orders = list(account_state.get("open_orders") or [])
    protective_orders = [row for row in open_orders if bool(row.get("reduce_only"))]
    entry_orders = [row for row in open_orders if not bool(row.get("reduce_only"))]
    tp_orders = [row for row in protective_orders if row.get("take_profit") not in (None, "")]
    sl_orders = [row for row in protective_orders if row.get("stop_loss") not in (None, "")]
    age_values = []
    for row in open_orders:
        timestamp_value = row.get("timestamp")
        try:
            raw_value = float(timestamp_value)
        except (TypeError, ValueError):
            continue
        if raw_value > 10_000_000_000:
            raw_value /= 1000.0
        age_values.append(max(0.0, now.timestamp() - raw_value) / 60.0)
    oldest_age = max(age_values) if age_values else 0.0
    return {
        "position_count": len(positions),
        "open_order_count": len(open_orders),
        "protective_order_count": len(protective_orders),
        "entry_order_count": len(entry_orders),
        "take_profit_count": len(tp_orders),
        "stop_loss_count": len(sl_orders),
        "orphan_protective_order_count": len(protective_orders) if not positions else 0,
        "oldest_order_age_minutes": round(oldest_age, 2),
        "positions": positions[:6],
        "protective_orders": protective_orders[:12],
    }


def _build_divergence_summary(store, latest_audit):
    account_state = (latest_audit or {}).get("account_state") or {}
    exchange_open_orders = len(account_state.get("open_orders") or [])
    exchange_positions = len(account_state.get("positions") or [])
    active_trade_cases = len(store.get_all_active_trade_cases()) if hasattr(store, "get_all_active_trade_cases") else 0
    unknown_orders = store.count_recent_unknown_orders(minutes=30) if hasattr(store, "count_recent_unknown_orders") else 0
    runtime_active_orders = 0
    conn = getattr(store, "conn", None)
    if conn is not None:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM order_runtime_state
            WHERE state NOT IN ('completed', 'cancelled', 'failed')
            """
        ).fetchone()
        runtime_active_orders = int((row["count"] if row is not None else 0) or 0)
    order_mismatch = runtime_active_orders != exchange_open_orders
    case_mismatch = active_trade_cases != exchange_positions
    drift_reasons = []
    if order_mismatch:
        drift_reasons.append("open_orders_mismatch")
    if case_mismatch:
        drift_reasons.append("trade_case_position_mismatch")
    if unknown_orders:
        drift_reasons.append("unknown_runtime_orders")
    return {
        "exchange_open_order_count": exchange_open_orders,
        "runtime_active_order_count": runtime_active_orders,
        "exchange_position_count": exchange_positions,
        "active_trade_case_count": active_trade_cases,
        "unknown_order_count_30m": unknown_orders,
        "order_mismatch": order_mismatch,
        "case_mismatch": case_mismatch,
        "drift_status": "drift" if drift_reasons else "aligned",
        "drift_reasons": drift_reasons,
    }


def _classify_hold_reason(row):
    intent = row.get("intent") or {}
    execution_summary = row.get("execution_summary") or {}
    ai_usage = execution_summary.get("ai_usage") or {}
    rationale_text = " ".join(str(item) for item in (intent.get("rationale") or []) if item).lower()
    risk_reason = str(row.get("risk_reason") or "").lower()
    risk_notes = " ".join(str(item) for item in (intent.get("risk_notes") or []) if item).lower()
    model_name = str(execution_summary.get("model_name") or ai_usage.get("model_name") or "").lower()
    combined = " ".join([rationale_text, risk_reason, risk_notes, model_name]).strip()
    if ai_usage.get("fallback_safe_intent") or ai_usage.get("semantic_validation_error"):
        return "fallback_hold"
    if "deterministic hold" in combined or "deterministic_skip" in combined or "kill switch" in combined:
        return "deterministic_skip"
    if "cooldown" in combined or "drawdown" in combined or "risk_mode:" in risk_notes:
        return "risk_mode"
    if "working order" in combined:
        return "working_orders"
    if any(token in combined for token in ("execution quality", "wide_spread", "spread", "depth", "liquidity")):
        return "execution_quality"
    if any(token in combined for token in ("mixed", "conflicting", "trend alignment", "trend")):
        return "mixed_trend"
    return "weak_evidence"


def _build_hold_reason_summary(rows):
    counts = {
        "fallback_hold": 0,
        "deterministic_skip": 0,
        "risk_mode": 0,
        "working_orders": 0,
        "execution_quality": 0,
        "mixed_trend": 0,
        "weak_evidence": 0,
    }
    total = 0
    for row in rows or []:
        decision = str(((row.get("intent") or {}).get("decision") or "")).upper().strip()
        if decision != "HOLD":
            continue
        total += 1
        counts[_classify_hold_reason(row)] += 1
    top_reason = ""
    top_count = 0
    if total:
        top_reason, top_count = max(counts.items(), key=lambda item: item[1])
    return {
        "total_hold_count": total,
        "top_reason": top_reason,
        "top_count": top_count,
        "reason_counts": counts,
    }


def _build_decision_funnel_summary(audit_rows, execution_eval_rows):
    total_audits = len(audit_rows or [])
    approved = sum(1 for row in audit_rows or [] if bool(row.get("risk_approved")))
    action_intents = sum(
        1
        for row in audit_rows or []
        if str(((row.get("intent") or {}).get("decision") or "")).upper().strip() not in {"", "HOLD"}
    )
    executed_audits = {int(row.get("audit_log_id")) for row in execution_eval_rows or [] if row.get("audit_log_id") is not None}
    filled_audits = {
        int(row.get("audit_log_id"))
        for row in execution_eval_rows or []
        if row.get("audit_log_id") is not None and _safe_float(row.get("filled_quantity"), default=0.0) > 0
    }
    labeled_audits = {
        int(row.get("audit_log_id"))
        for row in execution_eval_rows or []
        if row.get("audit_log_id") is not None and (row.get("resolved_outcome_label") or row.get("outcome_trade_case_id"))
    }
    return {
        "total_audits": total_audits,
        "action_intent_count": action_intents,
        "approved_count": approved,
        "executed_count": len(executed_audits),
        "filled_count": len(filled_audits),
        "labeled_count": len(labeled_audits),
    }


def _build_memory_lifecycle_summary(rows):
    retired = [row for row in rows or [] if bool(row.get("retired"))]
    revived = [row for row in rows or [] if int(row.get("revive_count") or 0) > 0]
    reason_counts = {}
    for row in retired:
        reason = str(row.get("retirement_reason") or "unknown").strip() or "unknown"
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    top_reason = ""
    top_count = 0
    if reason_counts:
        top_reason, top_count = max(reason_counts.items(), key=lambda item: item[1])
    recent_changes = [
        row for row in rows or []
        if bool(row.get("retired")) or int(row.get("revive_count") or 0) > 0 or int(row.get("merge_count") or 0) > 1
    ]
    return {
        "retired_count": len(retired),
        "revived_count": len(revived),
        "top_retirement_reason": top_reason,
        "top_retirement_count": top_count,
        "retirement_reason_counts": reason_counts,
        "recent_changes": recent_changes[:12],
    }


def _build_runtime_freshness_summary(now: datetime, latest_audit, snapshot):
    latest_audit_ts = _parse_dashboard_timestamp((latest_audit or {}).get("timestamp"))
    account_state = (latest_audit or {}).get("account_state") or {}
    snapshot_timestamp = account_state.get("snapshot_timestamp")
    snapshot_dt = None
    try:
        if snapshot_timestamp not in (None, ""):
            raw_value = float(snapshot_timestamp)
            if raw_value > 10_000_000_000:
                raw_value /= 1000.0
            snapshot_dt = datetime.fromtimestamp(raw_value, tz=UTC).replace(tzinfo=None)
    except (TypeError, ValueError, OSError):
        snapshot_dt = None
    order_rows = snapshot.get("recent_order_states") or []
    position_rows = snapshot.get("recent_position_states") or []
    latest_order_ts = _parse_dashboard_timestamp((order_rows[0] or {}).get("created_at")) if order_rows else None
    latest_position_ts = _parse_dashboard_timestamp((position_rows[0] or {}).get("created_at")) if position_rows else None

    def age_minutes(dt):
        if dt is None:
            return None
        return round(max(0.0, (now - dt).total_seconds()) / 60.0, 2)

    snapshot_age = age_minutes(snapshot_dt)
    audit_age = age_minutes(latest_audit_ts)
    order_age = age_minutes(latest_order_ts)
    position_age = age_minutes(latest_position_ts)
    freshness_state = "fresh"
    if any(value is not None and value > 120.0 for value in (snapshot_age, audit_age)):
        freshness_state = "stale"
    elif any(value is not None and value > 30.0 for value in (snapshot_age, audit_age, order_age, position_age)):
        freshness_state = "aging"
    return {
        "freshness_state": freshness_state,
        "snapshot_age_minutes": snapshot_age,
        "latest_audit_age_minutes": audit_age,
        "latest_order_state_age_minutes": order_age,
        "latest_position_state_age_minutes": position_age,
    }


def _build_dashboard_snapshot():
    now = datetime.now(UTC).replace(tzinfo=None)
    store = _get_state_store()
    snapshot = store.get_monitoring_snapshot(now=now)
    audit_rows_24h = store.get_recent_audits(limit=400, since=now - timedelta(hours=24))
    audit_rows_7d = store.get_recent_audits(limit=2000, since=now - timedelta(days=7))
    execution_eval_rows_24h = _enrich_execution_evaluation_rows(
        store.get_execution_policy_evaluations_since(now - timedelta(hours=24)),
        store,
    )
    snapshot["symbol_report"] = store.get_symbol_performance_report(hours=24, now=now)
    snapshot["daily_report"] = store.get_daily_ledger_report(days=7, now=now)
    snapshot["recent_trade_cases"] = store.get_recent_trade_cases(limit=10)
    snapshot["active_trade_cases"] = store.get_all_active_trade_cases()
    retrieval_rows = store.get_retrieval_evaluations_since(
        now - timedelta(hours=24),
        limit=400,
    )
    snapshot["recent_retrieval_evaluations"] = list(reversed(retrieval_rows[-12:]))
    recent_memory = store.get_recent_long_term_memory(limit=12)
    memory_rows = store.get_recent_long_term_memory(limit=120)
    latest_audit = _latest_full_audit(store)
    snapshot["recent_memory"] = recent_memory
    snapshot["trade_case_summary"] = _build_trade_case_summary(
        store.get_trade_cases_since(now - timedelta(hours=24), limit=500)
    )
    snapshot["execution_contract_summary"] = _build_execution_contract_summary(audit_rows_24h)
    snapshot["entry_style_summary"] = _build_entry_style_summary(execution_eval_rows_24h)
    snapshot["post_trade_outcome_summary"] = _build_post_trade_outcome_summary(execution_eval_rows_24h)
    snapshot["retrieval_summary"] = _build_retrieval_summary(retrieval_rows)
    snapshot["memory_summary"] = _build_memory_summary(recent_memory)
    snapshot["live_protection_summary"] = _build_live_protection_summary(latest_audit, now)
    snapshot["divergence_summary"] = _build_divergence_summary(store, latest_audit)
    snapshot["hold_reason_summary_7d"] = _build_hold_reason_summary(audit_rows_7d)
    snapshot["decision_funnel_24h"] = _build_decision_funnel_summary(audit_rows_24h, execution_eval_rows_24h)
    snapshot["memory_lifecycle_summary"] = _build_memory_lifecycle_summary(memory_rows)
    snapshot["runtime_freshness_summary"] = _build_runtime_freshness_summary(now, latest_audit, snapshot)
    return snapshot


def _resolve_replay_model_name(replay_provider: str) -> str:
    replay_model = (
        os.getenv("REPLAY_MODEL", "").strip()
        or os.getenv("MEMORY_MODEL", "").strip()
        or os.getenv("AI_DEFAULT_MODEL", "").strip()
        or os.getenv("GEMINI_MEMORY_MODEL", "").strip()
        or os.getenv("GEMINI_DEFAULT_MODEL", "").strip()
    )
    if replay_model:
        return replay_model
    return resolve_memory_model_name(replay_provider)


def _build_model_routing() -> dict[str, str]:
    provider = normalize_provider_name(os.getenv("AI_PROVIDER"))
    default_model, guarded_attention_model = resolve_decision_models(provider)
    memory_provider = normalize_provider_name(os.getenv("MEMORY_PROVIDER") or provider)
    memory_model = resolve_memory_model_name(
        memory_provider,
        explicit_model=os.getenv("MEMORY_MODEL"),
    )
    replay_provider = normalize_provider_name(os.getenv("REPLAY_PROVIDER") or provider)
    replay_model = _resolve_replay_model_name(replay_provider)
    return {
        "provider": provider,
        "default_model": default_model,
        "guarded_attention_model": guarded_attention_model,
        "memory_provider": memory_provider,
        "memory_model": memory_model,
        "replay_provider": replay_provider,
        "replay_model": replay_model,
    }


def _filter_trade_cases(rows, args):
    symbol = str(args.get("symbol", "")).strip().upper()
    status = str(args.get("status", "")).strip().lower()
    exit_reason = str(args.get("exit_reason", "")).strip().lower()
    failure_reason = str(args.get("failure_reason", "")).strip().lower()
    query = str(args.get("query", "")).strip().lower()
    date_from = _parse_dashboard_timestamp(args.get("date_from"))
    date_to = _parse_dashboard_timestamp(args.get("date_to"))
    if date_to is not None:
        date_to = date_to + timedelta(days=1)
    filtered = []
    for row in rows or []:
        row_symbol = str(row.get("symbol") or "").strip().upper()
        row_status = str(row.get("status") or "").strip().lower()
        row_exit_reason = str(row.get("exit_reason") or "").strip().lower()
        row_failure_reason = str(row.get("thesis_failure_reason") or "").strip().lower()
        row_time = _parse_dashboard_timestamp(row.get("closed_at") or row.get("opened_at"))
        haystack = " ".join(
            [
                str(row.get("trade_case_id") or ""),
                row_symbol,
                row_status,
                row_exit_reason,
                row_failure_reason,
            ]
        ).lower()
        if symbol and row_symbol != symbol:
            continue
        if status and row_status != status:
            continue
        if exit_reason and exit_reason not in row_exit_reason:
            continue
        if failure_reason and failure_reason not in row_failure_reason:
            continue
        if query and query not in haystack:
            continue
        if date_from is not None and (row_time is None or row_time < date_from):
            continue
        if date_to is not None and (row_time is None or row_time >= date_to):
            continue
        filtered.append(row)
    return filtered

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GPT Trader Control Room</title>
    <style>
        :root {
            --bg: #f2efe8;
            --panel: rgba(255, 252, 247, 0.92);
            --ink: #1f1a17;
            --muted: #6f655c;
            --accent: #b64926;
            --ok: #205c3b;
            --warn: #8e4b10;
            --bad: #8c1d18;
            --line: rgba(31, 26, 23, 0.08);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            color: var(--ink);
            background:
                radial-gradient(circle at top left, rgba(182, 73, 38, 0.16), transparent 28%),
                radial-gradient(circle at right 20%, rgba(32, 92, 59, 0.12), transparent 22%),
                linear-gradient(180deg, #f8f3ea, var(--bg));
            font-family: Georgia, "Times New Roman", serif;
        }
        .shell { max-width: 1440px; margin: 0 auto; padding: 28px 20px 48px; }
        .header {
            display: flex; justify-content: space-between; gap: 16px; align-items: end; margin-bottom: 24px;
        }
        .header h1 { margin: 0; font-size: clamp(34px, 5vw, 56px); letter-spacing: -0.04em; }
        .header p { margin: 8px 0 0; color: var(--muted); max-width: 720px; font-size: 15px; }
        .refresh, .action-button {
            border: 0; background: var(--ink); color: #fff; padding: 12px 16px; border-radius: 999px; cursor: pointer; font-size: 14px;
        }
        .action-button { background: var(--accent); }
        .ghost-button {
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, 0.7);
            color: var(--ink);
            padding: 8px 12px;
            border-radius: 999px;
            cursor: pointer;
            font-size: 12px;
        }
        .grid {
            display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 14px; margin-bottom: 22px;
        }
        .card, .panel {
            background: var(--panel); backdrop-filter: blur(8px); border: 1px solid var(--line); border-radius: 22px; box-shadow: 0 16px 40px rgba(58, 39, 23, 0.08);
        }
        .card { padding: 18px 18px 20px; min-height: 140px; }
        .eyebrow { margin: 0 0 10px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.12em; color: var(--muted); }
        .value { font-size: clamp(28px, 4vw, 44px); line-height: 1; margin: 0; }
        .sub { margin-top: 12px; color: var(--muted); font-size: 14px; }
        .layout { display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 14px; margin-bottom: 14px; }
        .stack { display: grid; gap: 14px; }
        .panel { padding: 18px; }
        .panel-header {
            display: flex;
            justify-content: space-between;
            gap: 12px;
            align-items: center;
            margin-bottom: 8px;
        }
        .panel-header .eyebrow { margin-bottom: 0; }
        .toolbar {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin-bottom: 10px;
        }
        .toolbar input, .toolbar select {
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, 0.8);
            border-radius: 12px;
            padding: 8px 10px;
            font-size: 12px;
            color: var(--ink);
        }
        table { width: 100%; border-collapse: collapse; }
        th, td {
            text-align: left; padding: 12px 8px; border-bottom: 1px solid var(--line); vertical-align: top; font-size: 14px;
        }
        th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; }
        .audit-row { cursor: pointer; }
        .audit-row:hover { background: rgba(182, 73, 38, 0.06); }
        .badge {
            display: inline-flex; align-items: center; gap: 6px; border-radius: 999px; padding: 5px 10px; font-size: 12px; font-weight: bold;
        }
        .ok { background: rgba(32, 92, 59, 0.12); color: var(--ok); }
        .warn { background: rgba(142, 75, 16, 0.14); color: var(--warn); }
        .bad { background: rgba(140, 29, 24, 0.12); color: var(--bad); }
        .memory-item, .fill-item {
            padding: 14px 0; border-bottom: 1px solid var(--line);
        }
        .memory-item:last-child, .fill-item:last-child { border-bottom: 0; }
        .memory-item strong, .fill-item strong { display: block; font-size: 15px; margin-bottom: 6px; }
        .memory-meta, .fill-meta { color: var(--muted); font-size: 13px; }
        .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
        .detail-box {
            background: rgba(255,255,255,0.55); border: 1px solid var(--line); border-radius: 16px; padding: 14px;
        }
        .detail-box pre {
            margin: 0; font-size: 12px; line-height: 1.55; overflow: auto; white-space: pre-wrap; word-break: break-word;
        }
        mark.path-highlight {
            background: rgba(182, 73, 38, 0.2);
            color: var(--ink);
            padding: 2px 0;
            border-radius: 4px;
        }
        .source-link {
            border: 0;
            background: rgba(182, 73, 38, 0.1);
            color: var(--accent);
            border-radius: 999px;
            padding: 3px 8px;
            cursor: pointer;
            font-size: 11px;
        }
        .detail-actions { display: flex; gap: 10px; margin-bottom: 14px; }
        @media (max-width: 1080px) {
            .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .layout, .detail-grid { grid-template-columns: 1fr; }
        }
        @media (max-width: 680px) {
            .grid { grid-template-columns: 1fr; }
            .detail-actions { flex-direction: column; }
        }
    </style>
</head>
<body>
    <div class="shell">
        <div class="header">
            <div>
                <h1>Trader Control Room</h1>
                <p>Operational health, trade cases, retrieval outcomes, memory lifecycle, and full audit drill-down for execution review.</p>
            </div>
            <button class="refresh" onclick="window.location.reload();">Refresh</button>
        </div>

        <section class="grid">
            <article class="card">
                <p class="eyebrow">Audits 24h</p>
                <p class="value" id="val-audits">-</p>
                <p class="sub">Total trading cycle audit rows recorded today.</p>
            </article>
            <article class="card">
                <p class="eyebrow">Risk Rejections</p>
                <p class="value" id="val-rejections">-</p>
                <p class="sub">Share of intents blocked by policy enforcement.</p>
            </article>
            <article class="card">
                <p class="eyebrow">Loop Errors</p>
                <p class="value" id="val-errors">-</p>
                <p class="sub">Unhandled loop failures in the last 24 hours.</p>
            </article>
            <article class="card">
                <p class="eyebrow">Unresolved Timeouts</p>
                <p class="value" id="val-timeouts">-</p>
                <p class="sub">Timed out orders that were not recovered.</p>
            </article>
            <article class="card">
                <p class="eyebrow">Semantic Fallbacks</p>
                <p class="value" id="val-semantic-fallbacks">-</p>
                <p class="sub" id="val-semantic-top-issue">Top issue: -</p>
                <p class="sub" id="val-semantic-models">Models: -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Fees 24h</p>
                <p class="value" id="val-fees">-</p>
                <p class="sub">Total captured execution fees from synced fills.</p>
            </article>
            <article class="card">
                <p class="eyebrow">Ledger Net PnL</p>
                <p class="value" id="val-ledger-pnl">-</p>
                <p class="sub">Net realized pnl aggregated from fill ledger rows.</p>
            </article>
            <article class="card">
                <p class="eyebrow">Trade Cases 24h</p>
                <p class="value" id="val-trade-cases">-</p>
                <p class="sub" id="val-trade-cases-meta">Closed - • Win rate -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Retrieval 24h</p>
                <p class="value" id="val-retrieval">-</p>
                <p class="sub" id="val-retrieval-meta">Settled - • Aligned -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Memory Health</p>
                <p class="value" id="val-memory">-</p>
                <p class="sub" id="val-memory-meta">High confidence - • Retired -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Context Mix</p>
                <p class="value" id="val-context-mix">-</p>
                <p class="sub" id="val-context-mix-meta">good hold - • missed hold -</p>
                <p class="sub" id="val-context-mix-extra">fallback - • other hold -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Hold Quality 7d</p>
                <p class="value" id="val-hold-quality">-</p>
                <p class="sub" id="val-hold-quality-meta">good - • bad -</p>
                <p class="sub" id="val-hold-quality-extra">fallback - • unlabeled -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Replay Learn 7d</p>
                <p class="value" id="val-replay-learning">-</p>
                <p class="sub" id="val-replay-learning-meta">with - • without - • tie -</p>
                <p class="sub" id="val-replay-learning-extra">decision - • utility -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Label Quality 7d</p>
                <p class="value" id="val-label-quality">-</p>
                <p class="sub" id="val-label-quality-meta">coverage - • linked -</p>
                <p class="sub" id="val-label-quality-extra">edge - • missed - • risk - • stale -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Learning Liveness</p>
                <p class="value" id="val-learning-liveness">-</p>
                <p class="sub" id="val-learning-liveness-meta">replay coverage -</p>
                <p class="sub" id="val-learning-liveness-extra">promotion gate -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Execution Contract</p>
                <p class="value" id="val-exec-contract">-</p>
                <p class="sub" id="val-exec-contract-meta">affected audits - • governed orders -</p>
                <p class="sub" id="val-exec-contract-extra">top adjustment - • immediate -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Live Protection</p>
                <p class="value" id="val-live-protection">-</p>
                <p class="sub" id="val-live-protection-meta">positions - • protective -</p>
                <p class="sub" id="val-live-protection-extra">tp - • sl - • orphan -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Divergence</p>
                <p class="value" id="val-divergence">-</p>
                <p class="sub" id="val-divergence-meta">exchange - • runtime -</p>
                <p class="sub" id="val-divergence-extra">cases - • unknown -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Decision Funnel 24h</p>
                <p class="value" id="val-decision-funnel">-</p>
                <p class="sub" id="val-decision-funnel-meta">action - • approved - • executed -</p>
                <p class="sub" id="val-decision-funnel-extra">filled - • labeled -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Runtime Freshness</p>
                <p class="value" id="val-runtime-freshness">-</p>
                <p class="sub" id="val-runtime-freshness-meta">snapshot - • audit -</p>
                <p class="sub" id="val-runtime-freshness-extra">order - • position -</p>
            </article>
            <article class="card">
                <p class="eyebrow">HOLD Reasons 7d</p>
                <p class="value" id="val-hold-reasons">-</p>
                <p class="sub" id="val-hold-reasons-meta">top -</p>
                <p class="sub" id="val-hold-reasons-extra">fallback - • risk - • exec -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Entry Styles 24h</p>
                <p class="value" id="val-entry-styles">-</p>
                <p class="sub" id="val-entry-styles-meta">settled - • top style -</p>
                <p class="sub" id="val-entry-styles-extra">retest - • value -</p>
            </article>
            <article class="card">
                <p class="eyebrow">Outcome Labels 24h</p>
                <p class="value" id="val-outcome-labels">-</p>
                <p class="sub" id="val-outcome-labels-meta">top label -</p>
                <p class="sub" id="val-outcome-labels-extra">edge - • missed - • risk - • stale -</p>
            </article>
        </section>

        <section class="layout">
            <article class="panel">
                <p class="eyebrow">Recent Audit Logs</p>
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Decision</th>
                            <th>Status</th>
                            <th>Reason</th>
                        </tr>
                    </thead>
                    <tbody id="audit-table-body">
                        <tr><td colspan="4">Loading...</td></tr>
                    </tbody>
                </table>
            </article>
            <div class="stack">
                <article class="panel">
                    <p class="eyebrow">Recent Memory</p>
                    <div id="memory-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Live Positions vs Protective Orders</p>
                    <div id="live-protection-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">HOLD Reason Breakdown</p>
                    <div id="hold-reason-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Memory Lifecycle Changes</p>
                    <div id="memory-lifecycle-list">Loading...</div>
                </article>
                <article class="panel">
                    <div class="panel-header">
                        <p class="eyebrow" id="trade-case-heading">Recent Trade Cases</p>
                        <button class="ghost-button" id="trade-case-toggle">Load Full History</button>
                    </div>
                    <div class="toolbar">
                        <input type="text" id="trade-case-filter-symbol" placeholder="Symbol">
                        <select id="trade-case-filter-status">
                            <option value="">All status</option>
                            <option value="active">Active</option>
                            <option value="closed">Closed</option>
                        </select>
                        <input type="text" id="trade-case-filter-exit" placeholder="Exit reason">
                        <input type="text" id="trade-case-filter-failure" placeholder="Failure reason">
                        <input type="date" id="trade-case-filter-from">
                        <input type="date" id="trade-case-filter-to">
                        <input type="text" id="trade-case-filter-query" placeholder="Search text">
                        <button class="ghost-button" id="trade-case-apply">Apply Filters</button>
                        <button class="ghost-button" id="trade-case-reset">Reset</button>
                    </div>
                    <div id="trade-case-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Retrieval Evaluations</p>
                    <div id="retrieval-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Recent Fills</p>
                    <div id="fill-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Recent Ledger</p>
                    <div id="ledger-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Recent Symbol Journal</p>
                    <div id="symbol-journal-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Recent Replays</p>
                    <div id="replay-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Recent Order States</p>
                    <div id="order-state-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Recent Position States</p>
                    <div id="position-state-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Symbol Report</p>
                    <div id="symbol-report-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Daily Report</p>
                    <div id="daily-report-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Entry Style Performance</p>
                    <div id="entry-style-list">Loading...</div>
                </article>
                <article class="panel">
                    <p class="eyebrow">Post-Trade Labels</p>
                    <div id="outcome-label-list">Loading...</div>
                </article>
            </div>
        </section>

        <section class="panel">
            <p class="eyebrow">Audit Detail</p>
            <div class="detail-actions">
                <button class="action-button" id="run-replay-button" disabled>Run Replay + Save</button>
                <span class="sub" id="replay-status">Select an audit row to inspect and replay.</span>
            </div>
            <div id="audit-detail">
                <p class="sub">Select an audit row to inspect stored request, AI response, trade case links, retrieval evaluations, ledger rows, execution summary, and synced fills.</p>
            </div>
        </section>
    </div>

    <script>
        let selectedAuditId = null;
        let currentRequestPayload = {};
        let selectedSourcePath = '';
        let currentAuditDetail = null;
        let dashboardStats = null;
        let tradeCaseHistoryLoaded = false;
        let tradeCaseFilters = {};

        function renderModelRouting(models) {
            const value = models || {};
            const provider = value.provider || 'unset';
            const defaultModel = value.default_model || 'unset';
            const triggerModel = value.guarded_attention_model || defaultModel;
            const memoryProvider = value.memory_provider || provider;
            const memoryModel = value.memory_model || triggerModel || defaultModel;
            const replayProvider = value.replay_provider || provider;
            const replayModel = value.replay_model || memoryModel;
            return `decision ${provider}:${defaultModel} | guarded ${triggerModel} | memory ${memoryProvider}:${memoryModel} | replay ${replayProvider}:${replayModel}`;
        }

        function parseJson(raw, fallback = {}) {
            if (raw && typeof raw === 'object') return raw;
            try { return JSON.parse(raw || '{}'); } catch { return fallback; }
        }

        function pretty(value) {
            return JSON.stringify(value, null, 2);
        }

        function escapeHtml(value) {
            return String(value)
                .replaceAll('&', '&amp;')
                .replaceAll('<', '&lt;')
                .replaceAll('>', '&gt;')
                .replaceAll('"', '&quot;')
                .replaceAll("'", '&#39;');
        }

        function formatSigned(value, digits = 2) {
            const numeric = Number(value || 0);
            if (!Number.isFinite(numeric)) return '0';
            return numeric > 0 ? `+${numeric.toFixed(digits)}` : numeric.toFixed(digits);
        }

        function summarizeReplayCounterfactual(run) {
            const payload = parseJson((run || {}).payload, {});
            const comparison = ((((payload || {}).retrieval_counterfactual || {}).with_vs_without_comparison) || {});
            if (!Object.keys(comparison).length) {
                return 'counterfactual n/a';
            }
            const better = comparison.with_retrieval_better === true
                ? 'with retrieval better'
                : comparison.without_retrieval_better === true
                    ? 'without retrieval better'
                    : 'no outcome verdict';
            const utilityDelta = comparison.outcome_utility_delta;
            const utilityText = utilityDelta === null || utilityDelta === undefined
                ? 'utility n/a'
                : `utility ${formatSigned(utilityDelta, 3)}`;
            return `${better} • ${utilityText}`;
        }

        function renderTradeCaseSnapshot(caseRow) {
            const realized = caseRow.realized_pnl;
            const estimated = caseRow.estimated_pnl_pct;
            const pnlLabel = realized !== null && realized !== undefined
                ? `realized ${formatSigned(realized, 4)}`
                : `est ${formatSigned(estimated, 2)}%`;
            return `
                <div class="fill-item">
                    <strong>${caseRow.symbol || 'UNKNOWN'} • ${caseRow.status || 'unknown'} • ${caseRow.trade_case_id || '-'}</strong>
                    <div class="fill-meta">
                        ${pnlLabel} • hold ${caseRow.holding_minutes ?? 'n/a'}m • mfe ${caseRow.observed_mfe_pct ?? 'n/a'} • mae ${caseRow.observed_mae_pct ?? 'n/a'}
                        <br>slippage ${caseRow.slippage_bps ?? 'n/a'} bps • failure ${caseRow.thesis_failure_reason || 'n/a'}
                    </div>
                </div>
            `;
        }

        function buildTradeCaseQuery(fullHistory = false) {
            const params = new URLSearchParams();
            if (fullHistory) {
                params.set('all', '1');
            }
            Object.entries(tradeCaseFilters || {}).forEach(([key, value]) => {
                if (value !== undefined && value !== null && String(value).trim() !== '') {
                    params.set(key, String(value).trim());
                }
            });
            return params.toString();
        }

        function renderTradeCaseList(items, fullHistory = false) {
            tradeCaseHistoryLoaded = !!fullHistory;
            const heading = document.getElementById('trade-case-heading');
            const toggle = document.getElementById('trade-case-toggle');
            if (heading) {
                heading.innerText = fullHistory ? 'Trade Case History' : 'Recent Trade Cases';
            }
            if (toggle) {
                toggle.innerText = fullHistory ? 'Show Recent Only' : 'Load Full History';
            }
            const labelSuffix = Object.keys(tradeCaseFilters || {}).length ? ' (filtered)' : '';
            if (heading) {
                heading.innerText = (fullHistory ? 'Trade Case History' : 'Recent Trade Cases') + labelSuffix;
            }
            renderList(
                'trade-case-list',
                'No trade cases saved yet.',
                items || [],
                renderTradeCaseSnapshot,
            );
        }

        function loadTradeCaseHistory(fullHistory = false) {
            if (!fullHistory && !Object.keys(tradeCaseFilters || {}).length) {
                renderTradeCaseList((dashboardStats && dashboardStats.recent_trade_cases) || [], false);
                return Promise.resolve();
            }
            const query = buildTradeCaseQuery(fullHistory);
            const suffix = query ? `?${query}` : '';
            return fetch(`/api/trade-cases${suffix}`)
                .then(res => res.json())
                .then(payload => {
                    renderTradeCaseList(payload.trade_cases || [], fullHistory);
                });
        }

        function collectTradeCaseFilters() {
            const nextFilters = {};
            const symbol = document.getElementById('trade-case-filter-symbol').value;
            const status = document.getElementById('trade-case-filter-status').value;
            const exitReason = document.getElementById('trade-case-filter-exit').value;
            const failureReason = document.getElementById('trade-case-filter-failure').value;
            const dateFrom = document.getElementById('trade-case-filter-from').value;
            const dateTo = document.getElementById('trade-case-filter-to').value;
            const query = document.getElementById('trade-case-filter-query').value;
            if (symbol) nextFilters.symbol = symbol;
            if (status) nextFilters.status = status;
            if (exitReason) nextFilters.exit_reason = exitReason;
            if (failureReason) nextFilters.failure_reason = failureReason;
            if (dateFrom) nextFilters.date_from = dateFrom;
            if (dateTo) nextFilters.date_to = dateTo;
            if (query) nextFilters.query = query;
            tradeCaseFilters = nextFilters;
        }

        function renderLiveProtectionPanel(summary) {
            const positions = summary.positions || [];
            const protectiveOrders = summary.protective_orders || [];
            const root = document.getElementById('live-protection-list');
            root.innerHTML = `
                <div class="fill-item">
                    <strong>positions ${summary.position_count || 0} • protective ${summary.protective_order_count || 0} • entry ${summary.entry_order_count || 0}</strong>
                    <div class="fill-meta">
                        tp ${summary.take_profit_count || 0} • sl ${summary.stop_loss_count || 0} • orphan ${summary.orphan_protective_order_count || 0}
                        <br>oldest order ${Number(summary.oldest_order_age_minutes || 0).toFixed(1)}m
                    </div>
                </div>
                ${positions.map(row => `
                    <div class="fill-item">
                        <strong>${row.symbol || 'UNKNOWN'} • ${row.side || 'UNKNOWN'} • size ${row.size || 0}</strong>
                        <div class="fill-meta">entry ${row.entry_price ?? 'n/a'} • unrealized ${row.unrealized_pnl_pct ?? 'n/a'}%</div>
                    </div>
                `).join('')}
                ${protectiveOrders.map(row => `
                    <div class="fill-item">
                        <strong>${row.symbol || 'UNKNOWN'} protective • ${row.side || 'UNKNOWN'} ${row.order_type || 'UNKNOWN'}</strong>
                        <div class="fill-meta">qty ${row.quantity || 0} • tp ${row.take_profit ?? 'n/a'} • sl ${row.stop_loss ?? 'n/a'} • reduce_only ${row.reduce_only ? 'yes' : 'no'}</div>
                    </div>
                `).join('')}
            `;
        }

        function renderHoldReasonPanel(summary) {
            const counts = summary.reason_counts || {};
            renderList(
                'hold-reason-list',
                'No HOLD rows yet.',
                Object.entries(counts).map(([reason, count]) => ({ reason, count })).filter(row => row.count > 0),
                row => `
                    <div class="fill-item">
                        <strong>${row.reason}</strong>
                        <div class="fill-meta">count ${row.count || 0}</div>
                    </div>
                `,
            );
        }

        function renderMemoryLifecyclePanel(summary) {
            renderList(
                'memory-lifecycle-list',
                'No lifecycle changes saved yet.',
                summary.recent_changes || [],
                item => `
                    <div class="fill-item">
                        <strong>${item.rule || 'memory rule'}</strong>
                        <div class="fill-meta">
                            lifecycle ${item.lifecycle_status || 'unknown'} • revive ${item.revive_count || 0} • merge ${item.merge_count || 0}
                            <br>retirement ${item.retirement_reason || 'n/a'} • confidence ${(Number(item.confidence_score || 0)).toFixed(2)} • validity ${(Number(item.validity_score || 0)).toFixed(2)}
                        </div>
                    </div>
                `,
            );
        }

        function renderRetrievalEvaluationRow(row) {
            const outcome = row.outcome_status || (row.outcome_trade_case_id ? 'linked' : 'pending');
            const pnl = row.outcome_realized_pnl;
            const pnlLabel = pnl === null || pnl === undefined ? 'pnl n/a' : `pnl ${formatSigned(pnl, 4)}`;
            return `
                <div class="fill-item">
                    <strong>${row.context_type || 'context'} • ${row.case_symbol || row.matched_symbol || 'UNKNOWN'} • rank ${row.rank_index ?? '-'}</strong>
                    <div class="fill-meta">
                        sim ${row.similarity ?? 'n/a'} • score ${row.final_score ?? 'n/a'} • source ${row.retrieval_source || 'n/a'} • aligned ${row.decision_symbol_aligned ? 'yes' : 'no'}
                        <br>outcome ${outcome} • ${pnlLabel} • bands ${row.matched_band_count ?? 0}
                    </div>
                </div>
            `;
        }

        function summarizeCandidateLessons(metrics) {
            const lessons = (metrics && metrics.candidate_lessons) || [];
            if (!lessons.length) return 'no heuristic candidates';
            return lessons.slice(0, 2).map(item => {
                const support = item.support_count || 0;
                const seed = item.seed_signal || 'signal';
                return `${seed} (${support})`;
            }).join(' | ');
        }

        function summarizeEvidenceDiff(run) {
            const added = (run.evidence_signals_added || []).slice(0, 2).join(', ') || 'none';
            const removed = (run.evidence_signals_removed || []).slice(0, 2).join(', ') || 'none';
            const delta = run.evidence_signal_count_delta || 0;
            return `evidence Δ ${delta} • + ${added} • - ${removed}`;
        }

        function renderRationaleItems(intent) {
            const items = (intent && intent.rationale_items) || [];
            if (!items.length) {
                return '<p class="sub">No structured rationale stored.</p>';
            }
            return items.map(item => {
                const evidence = (item.evidence || []).map(ev => {
                    const symbol = ev.symbol ? `${ev.symbol} ` : '';
                    const threshold = ev.threshold === null || ev.threshold === undefined ? '' : ` ${ev.operator} ${ev.threshold}`;
                    return `
                        <div class="fill-meta">
                            ${symbol}${ev.signal}@${ev.timeframe}: ${ev.observed_value}${threshold}
                            <button class="source-link" data-source="${escapeHtml(ev.source || '')}">${escapeHtml(ev.source || 'source')}</button>
                        </div>
                    `;
                }).join('');
                return `
                    <div class="memory-item">
                        <strong>${item.summary || 'Structured rationale'}</strong>
                        <div class="memory-meta">${item.why_it_matters || ''}</div>
                        <div class="memory-meta">${item.trade_implication || ''}</div>
                        <div class="memory-meta">failure mode: ${item.failure_mode || 'n/a'}</div>
                        ${evidence}
                    </div>
                `;
            }).join('');
        }

        function renderEvidenceQuality(executionSummary) {
            const evidenceQuality = executionSummary.evidence_quality || {};
            const entrySizeCap = executionSummary.entry_size_cap || {};
            const setupQuality = entrySizeCap.setup_quality || {};
            if (!Object.keys(evidenceQuality).length && !Object.keys(entrySizeCap).length) {
                return '<p class="sub">No evidence quality summary stored.</p>';
            }
            return `
                <div class="memory-item">
                    <strong>Evidence quality factor ${evidenceQuality.factor ?? 'n/a'}</strong>
                    <div class="memory-meta">
                        signals ${evidenceQuality.unique_signal_count ?? 0} • evidence rows ${evidenceQuality.evidence_count ?? 0}
                        • covered symbols ${(evidenceQuality.covered_symbols || []).join(', ') || 'none'}
                    </div>
                    <div class="memory-meta">
                        issues ${(evidenceQuality.issues || []).join(', ') || 'none'}
                    </div>
                    <div class="memory-meta">
                        entry cap ${(entrySizeCap.base_cap_pct ?? 0)}% -> ${(entrySizeCap.effective_cap_pct ?? 0)}%
                        • adjusted ${entrySizeCap.adjusted ? 'yes' : 'no'}
                    </div>
                    <div class="memory-meta">
                        setup quality ${(setupQuality.factor ?? 1.0)} • issues ${(setupQuality.issues || []).join(', ') || 'none'}
                    </div>
                </div>
            `;
        }

        function renderReplaySourceDiffs(detail) {
            const latestRun = (detail && detail.replay_runs || [])[0];
            if (!latestRun) {
                return '<p class="sub">No replay run stored yet.</p>';
            }
            const payload = parseJson(latestRun.payload, {});
            const diffs = (((payload || {}).comparison || {}).evidence_source_diffs) || [];
            if (!diffs.length) {
                return '<p class="sub">No source-level evidence diff detected.</p>';
            }
            return `
                <table>
                    <thead>
                        <tr>
                            <th>Source</th>
                            <th>Original</th>
                            <th>Replayed</th>
                            <th>Delta</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${diffs.map(diff => {
                            const originalValue = summarizeEvidenceObservedValue(diff.original || []);
                            const replayedValue = summarizeEvidenceObservedValue(diff.replayed || []);
                            return `
                                <tr>
                                    <td>${escapeHtml(diff.source || 'source')}</td>
                                    <td>${escapeHtml(originalValue.label)}</td>
                                    <td>${escapeHtml(replayedValue.label)}</td>
                                    <td>${escapeHtml(computeObservedDeltaLabel(originalValue.numeric, replayedValue.numeric))}</td>
                                </tr>
                            `;
                        }).join('')}
                    </tbody>
                </table>
            `;
        }

        function summarizeEvidenceObservedValue(rows) {
            const values = (rows || []).map(row => row && row.observed_value).filter(value => value !== undefined && value !== null);
            if (!values.length) {
                return { label: 'n/a', numeric: null };
            }
            if (values.length === 1) {
                return { label: String(values[0]), numeric: parseNumericValue(values[0]) };
            }
            const label = values.map(value => String(value)).join(', ');
            return { label: label, numeric: null };
        }

        function parseNumericValue(value) {
            const numeric = Number(value);
            return Number.isFinite(numeric) ? numeric : null;
        }

        function computeObservedDeltaLabel(originalValue, replayedValue) {
            if (originalValue === null || replayedValue === null) {
                return 'n/a';
            }
            const delta = replayedValue - originalValue;
            return delta > 0 ? `+${delta.toFixed(4)}` : delta.toFixed(4);
        }

        function getPreviousValuePath(path) {
            if (!path || !path.includes('.')) return '';
            const parts = path.split('.');
            const metric = parts.pop();
            return `${parts.join('.')}.previous_values.${metric}`;
        }

        function collectThresholdValues(intent, path) {
            const values = [];
            for (const item of ((intent && intent.rationale_items) || [])) {
                for (const evidence of ((item && item.evidence) || [])) {
                    if ((evidence && evidence.source) !== path) continue;
                    if (evidence.threshold === null || evidence.threshold === undefined) continue;
                    values.push(evidence.threshold);
                }
            }
            return values;
        }

        function resolvePath(payload, path) {
            if (!path) return undefined;
            return path.split('.').reduce((current, part) => {
                if (current === undefined || current === null) return undefined;
                if (Array.isArray(current)) {
                    const index = Number(part);
                    return Number.isInteger(index) ? current[index] : undefined;
                }
                return current[part];
            }, payload);
        }

        function updateSourceLookup(path) {
            const root = document.getElementById('source-lookup');
            if (!root) return;
            selectedSourcePath = path || '';
            const requestPre = document.getElementById('stored-request-json');
            if (requestPre) {
                requestPre.innerHTML = highlightRequestPayload(currentRequestPayload, selectedSourcePath);
            }
            if (!path) {
                root.innerHTML = '<p class="sub">Click a source path in Structured Rationale to inspect the stored request value.</p>';
                return;
            }
            const resolved = resolvePath(currentRequestPayload, path);
            const previousPath = getPreviousValuePath(path);
            const previousResolved = previousPath ? resolvePath(currentRequestPayload, previousPath) : undefined;
            const auditResolved = resolveAuditMarketPath(currentAuditDetail, path);
            const auditPreviousResolved = previousPath ? resolveAuditMarketPath(currentAuditDetail, previousPath) : undefined;
            const originalObserved = collectObservedValues((currentAuditDetail && currentAuditDetail.intent) || {}, path);
            const originalThresholds = collectThresholdValues((currentAuditDetail && currentAuditDetail.intent) || {}, path);
            const replayObserved = collectLatestReplayObservedValues(currentAuditDetail, path);
            const latestReplay = (((currentAuditDetail && currentAuditDetail.replay_runs) || [])[0] || {});
            const replayPayload = parseJson(latestReplay.payload, {});
            const replayThresholds = collectThresholdValues((replayPayload && replayPayload.replayed_intent) || {}, path);
            root.innerHTML = `
                <div class="memory-item">
                    <strong>${escapeHtml(path)}</strong>
                    <div class="memory-meta">resolved from stored DecisionRequest</div>
                    <pre>${escapeHtml(pretty(resolved === undefined ? '(missing)' : resolved))}</pre>
                    <div class="memory-meta">previous value from stored DecisionRequest</div>
                    <pre>${escapeHtml(pretty(previousResolved === undefined ? '(missing)' : previousResolved))}</pre>
                    <div class="memory-meta">resolved from stored audit market payload</div>
                    <pre>${escapeHtml(pretty(auditResolved === undefined ? '(missing)' : auditResolved))}</pre>
                    <div class="memory-meta">previous value from stored audit market payload</div>
                    <pre>${escapeHtml(pretty(auditPreviousResolved === undefined ? '(missing)' : auditPreviousResolved))}</pre>
                    <div class="memory-meta">original intent evidence observed_value</div>
                    <pre>${escapeHtml(pretty(originalObserved.length ? originalObserved : '(missing)'))}</pre>
                    <div class="memory-meta">original intent evidence threshold</div>
                    <pre>${escapeHtml(pretty(originalThresholds.length ? originalThresholds : '(missing)'))}</pre>
                    <div class="memory-meta">latest replay evidence observed_value</div>
                    <pre>${escapeHtml(pretty(replayObserved.length ? replayObserved : '(missing)'))}</pre>
                    <div class="memory-meta">latest replay evidence threshold</div>
                    <pre>${escapeHtml(pretty(replayThresholds.length ? replayThresholds : '(missing)'))}</pre>
                </div>
            `;
        }

        function resolveAuditMarketPath(detail, path) {
            if (!detail || !path) return undefined;
            const marketState = parseJson(detail.market_state, {});
            if (path.startsWith('market_snapshots.')) {
                return resolvePath(marketState, path);
            }
            if (path.startsWith('market_snapshot.')) {
                const marketSnapshot = (marketState.market_snapshots && Object.values(marketState.market_snapshots)[0]) || marketState.market_snapshot;
                const suffix = path.slice('market_snapshot.'.length);
                return resolvePath(marketSnapshot || {}, suffix);
            }
            return undefined;
        }

        function collectObservedValues(intent, path) {
            const values = [];
            for (const item of (intent && intent.rationale_items) || []) {
                for (const evidence of (item.evidence || [])) {
                    if ((evidence.source || '') === path) {
                        values.push({
                            signal: evidence.signal || 'signal',
                            observed_value: evidence.observed_value,
                            threshold: evidence.threshold,
                            operator: evidence.operator,
                        });
                    }
                }
            }
            return values;
        }

        function collectLatestReplayObservedValues(detail, path) {
            if (!detail) return [];
            const latestRun = (detail.replay_runs || [])[0];
            if (!latestRun) return [];
            const payload = parseJson(latestRun.payload, {});
            return collectObservedValues(payload.replayed_intent || {}, path);
        }

        function highlightRequestPayload(payload, path) {
            const raw = pretty(payload);
            const escapedRaw = escapeHtml(raw);
            if (!path) {
                return escapedRaw;
            }
            const resolved = resolvePath(payload, path);
            if (resolved === undefined) {
                return escapedRaw;
            }
            const snippet = escapeHtml(pretty(resolved));
            const highlighted = `<mark class="path-highlight">${snippet}</mark>`;
            const index = escapedRaw.indexOf(snippet);
            if (index < 0) {
                return escapedRaw;
            }
            return escapedRaw.slice(0, index) + highlighted + escapedRaw.slice(index + snippet.length);
        }

        function bindSourceLinks() {
            document.querySelectorAll('.source-link').forEach(button => {
                button.addEventListener('click', () => {
                    updateSourceLookup(button.dataset.source || '');
                });
            });
        }

        function renderDetail(detail) {
            currentAuditDetail = detail;
            const executionSummary = parseJson(detail.execution_summary, {});
            const requestPayload = parseJson(detail.decision_request, {});
            currentRequestPayload = requestPayload;
            const fills = detail.fills || [];
            const replayRuns = detail.replay_runs || [];
            const ledgerEntries = detail.ledger_entries || [];
            const symbolJournalEntries = detail.symbol_journal_entries || [];
            const tradeCases = detail.trade_cases || [];
            const retrievalEvaluations = detail.retrieval_evaluations || [];
            const riskProfile = parseJson(detail.risk_profile, {});
            const orderStateTransitions = detail.order_state_transitions || [];
            const positionStateTransitions = detail.position_state_transitions || [];
            const storedIntent = detail.intent || {};
            document.getElementById('audit-detail').innerHTML = `
                <div class="detail-grid">
                    <div class="detail-box">
                        <p class="eyebrow">Candidate Input Hash</p>
                        <pre>${detail.decision_input_hash || '(empty)'}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Risk Mode / Profile</p>
                        <pre>${pretty({ risk_mode: detail.risk_mode, risk_profile: riskProfile })}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Stored Request</p>
                        <pre id="stored-request-json">${highlightRequestPayload(requestPayload, selectedSourcePath)}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Structured Rationale</p>
                        ${renderRationaleItems(storedIntent)}
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Evidence Quality</p>
                        ${renderEvidenceQuality(executionSummary)}
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Execution Summary</p>
                        <pre>${pretty(executionSummary)}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Symbol Journal Entries</p>
                        <pre>${pretty(symbolJournalEntries)}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Linked Trade Cases</p>
                        <pre>${pretty(tradeCases)}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Retrieval Evaluations</p>
                        <pre>${pretty(retrievalEvaluations)}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">AI Raw Response</p>
                        <pre>${detail.ai_raw_response || '(empty)'}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Stored Intent</p>
                        <pre>${pretty(storedIntent)}</pre>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Source Lookup</p>
                        <div id="source-lookup">
                            <p class="sub">Click a source path in Structured Rationale to inspect the stored request value.</p>
                        </div>
                    </div>
                    <div class="detail-box">
                        <p class="eyebrow">Replay Source Diff</p>
                        ${renderReplaySourceDiffs(detail)}
                    </div>
                    <div class="detail-box" style="grid-column: 1 / -1;">
                        <p class="eyebrow">Ledger Entries</p>
                        <pre>${pretty(ledgerEntries)}</pre>
                    </div>
                    <div class="detail-box" style="grid-column: 1 / -1;">
                        <p class="eyebrow">Synced Fills</p>
                        <pre>${pretty(fills)}</pre>
                    </div>
                    <div class="detail-box" style="grid-column: 1 / -1;">
                        <p class="eyebrow">Replay Runs</p>
                        <pre>${pretty(replayRuns)}</pre>
                    </div>
                    <div class="detail-box" style="grid-column: 1 / -1;">
                        <p class="eyebrow">Order State Transitions</p>
                        <pre>${pretty(orderStateTransitions)}</pre>
                    </div>
                    <div class="detail-box" style="grid-column: 1 / -1;">
                        <p class="eyebrow">Position State Transitions</p>
                        <pre>${pretty(positionStateTransitions)}</pre>
                    </div>
                </div>
            `;
            bindSourceLinks();
            updateSourceLookup('');
        }

        function loadAuditDetail(auditId) {
            selectedAuditId = auditId;
            document.getElementById('run-replay-button').disabled = false;
            fetch(`/api/audits/${auditId}`)
                .then(res => res.json())
                .then(renderDetail);
        }

        function renderList(elementId, emptyText, items, renderItem) {
            const root = document.getElementById(elementId);
            if (!items.length) {
                root.innerHTML = `<p class="sub">${emptyText}</p>`;
                return;
            }
            root.innerHTML = items.map(renderItem).join('');
        }

        function runReplay() {
            if (!selectedAuditId) return;
            const status = document.getElementById('replay-status');
            status.innerText = `Running replay for audit ${selectedAuditId}...`;
            fetch(`/api/audits/${selectedAuditId}/replay`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ save: true })
            })
                .then(async res => {
                    const data = await res.json();
                    if (!res.ok) throw new Error(data.error || 'Replay failed');
                    status.innerText = `Replay saved. Decision changed: ${data.comparison.decision_changed ? 'yes' : 'no'}`;
                    loadAuditDetail(selectedAuditId);
                    return fetch('/api/stats');
                })
                .then(res => res.json())
                .then(data => {
                    renderList('replay-list', 'No replay runs saved yet.', data.recent_replays, run => `
                        <div class="fill-item">
                            <strong>audit ${run.audit_log_id}: ${run.original_decision || 'UNKNOWN'} -> ${run.replayed_decision || 'UNKNOWN'}</strong>
                            <div class="fill-meta">
                                changed ${run.decision_changed ? 'yes' : 'no'} • side ${run.side_changed ? 'changed' : 'same'} • size ${run.size_delta || 0} • confidence ${run.confidence_delta || 0}
                            </div>
                        </div>
                    `);
                })
                .catch(err => {
                    status.innerText = err.message;
                });
        }

        document.getElementById('run-replay-button').addEventListener('click', runReplay);
        document.getElementById('trade-case-toggle').addEventListener('click', () => {
            loadTradeCaseHistory(!tradeCaseHistoryLoaded);
        });
        document.getElementById('trade-case-apply').addEventListener('click', () => {
            collectTradeCaseFilters();
            loadTradeCaseHistory(true);
        });
        document.getElementById('trade-case-reset').addEventListener('click', () => {
            tradeCaseFilters = {};
            ['trade-case-filter-symbol', 'trade-case-filter-status', 'trade-case-filter-exit', 'trade-case-filter-failure', 'trade-case-filter-from', 'trade-case-filter-to', 'trade-case-filter-query']
                .forEach(id => { document.getElementById(id).value = ''; });
            loadTradeCaseHistory(false);
        });

        fetch('/api/stats')
            .then(res => res.json())
            .then(data => {
                dashboardStats = data;
                document.getElementById('val-audits').innerText = data.total_audits_24h;
                document.getElementById('val-errors').innerText = data.loop_errors_24h || 0;
                document.getElementById('val-timeouts').innerText = data.timeouts_unresolved_24h || 0;
                const semanticSummary = data.semantic_validation_summary || {};
                const semantic24h = semanticSummary.last_24h || {};
                const semantic7d = semanticSummary.last_7d || {};
                const semanticLatest50 = semanticSummary.latest_50 || {};
                document.getElementById('val-semantic-fallbacks').innerText =
                    `${semantic24h.failure_count || 0} / ${semantic7d.failure_count || 0} / ${semanticLatest50.failure_count || 0}`;
                document.getElementById('val-fees').innerText = Number(data.fee_cost_24h || 0).toFixed(4);
                document.getElementById('val-ledger-pnl').innerText = Number(data.ledger_net_pnl_24h || 0).toFixed(4);
                const tradeCaseSummary = data.trade_case_summary || {};
                const retrievalSummary = data.retrieval_summary || {};
                const memorySummary = data.memory_summary || {};
                const contextBiasSummary = (((data.context_bias_summary || {}).last_24h || {}).journal_selected || {});
                const holdQualitySummary = data.hold_quality_summary_7d || {};
                const replayLearningSummary = data.replay_learning_summary_7d || {};
                const labelQualitySummary = data.label_quality_summary_7d || {};
                const learningLiveness = data.learning_liveness || {};
                const executionContractSummary = data.execution_contract_summary || {};
                const entryStyleSummary = data.entry_style_summary || {};
                const outcomeSummary = data.post_trade_outcome_summary || {};
                const liveProtectionSummary = data.live_protection_summary || {};
                const divergenceSummary = data.divergence_summary || {};
                const holdReasonSummary = data.hold_reason_summary_7d || {};
                const decisionFunnelSummary = data.decision_funnel_24h || {};
                const memoryLifecycleSummary = data.memory_lifecycle_summary || {};
                const runtimeFreshnessSummary = data.runtime_freshness_summary || {};
                const styleRows = entryStyleSummary.styles || [];
                const labelCounts = outcomeSummary.label_counts || {};
                const qualityLabelCounts = labelQualitySummary.label_counts || {};
                const contextGroupCounts = contextBiasSummary.group_counts || {};
                const contextGroupRatios = contextBiasSummary.group_ratios || {};
                document.getElementById('val-trade-cases').innerText = tradeCaseSummary.total_count || 0;
                document.getElementById('val-trade-cases-meta').innerText =
                    `Closed ${tradeCaseSummary.closed_count || 0} • Win rate ${(tradeCaseSummary.win_rate || 0).toFixed(1)}%`;
                document.getElementById('val-retrieval').innerText = retrievalSummary.total_count || 0;
                document.getElementById('val-retrieval-meta').innerText =
                    `Settled ${retrievalSummary.settled_count || 0} • Aligned ${retrievalSummary.aligned_count || 0}`;
                document.getElementById('val-memory').innerText = memorySummary.active_count || 0;
                document.getElementById('val-memory-meta').innerText =
                    `High confidence ${memorySummary.high_confidence_count || 0} • Retired ${memorySummary.retired_count || 0}`;
                document.getElementById('val-context-mix').innerText =
                    `${contextBiasSummary.total || 0}`;
                document.getElementById('val-context-mix-meta').innerText =
                    `good hold ${(((contextGroupRatios.good_hold || 0) * 100).toFixed(0))}% • missed hold ${(((contextGroupRatios.missed_opportunity_hold || 0) * 100).toFixed(0))}%`;
                document.getElementById('val-context-mix-extra').innerText =
                    `action ${(((contextGroupRatios.action || 0) * 100).toFixed(0))}% • fallback ${(((contextGroupRatios.fallback || 0) * 100).toFixed(0))}% • other hold ${(((contextGroupRatios.other_hold || 0) * 100).toFixed(0))}%`;
                document.getElementById('val-hold-quality').innerText = holdQualitySummary.total_hold_count || 0;
                document.getElementById('val-hold-quality-meta').innerText =
                    `good ${holdQualitySummary.good_hold_count || 0} • bad ${holdQualitySummary.bad_hold_count || 0}`;
                document.getElementById('val-hold-quality-extra').innerText =
                    `fallback ${holdQualitySummary.fallback_hold_count || 0} • unlabeled ${holdQualitySummary.unlabeled_hold_count || 0}`;
                document.getElementById('val-replay-learning').innerText = replayLearningSummary.evaluation_count || 0;
                document.getElementById('val-replay-learning-meta').innerText =
                    `with ${replayLearningSummary.with_learning_better_count || 0} • without ${replayLearningSummary.without_learning_better_count || 0} • tie ${replayLearningSummary.tie_count || 0}`;
                document.getElementById('val-replay-learning-extra').innerText =
                    `linked ${replayLearningSummary.outcome_linked_count || 0} • decision ${replayLearningSummary.decision_changed_count || 0} • utility ${(Number(replayLearningSummary.avg_outcome_utility_delta || 0)).toFixed(3)}`;
                document.getElementById('val-label-quality').innerText = labelQualitySummary.tracked_total || 0;
                document.getElementById('val-label-quality-meta').innerText =
                    `coverage ${((Number(labelQualitySummary.coverage_rate || 0)) * 100).toFixed(0)}% • linked ${((Number(labelQualitySummary.outcome_linked_rate || 0)) * 100).toFixed(0)}%`;
                document.getElementById('val-label-quality-extra').innerText =
                    `edge ${qualityLabelCounts.edge_failed || 0} • missed ${qualityLabelCounts.missed_due_to_no_fill || 0} • risk ${qualityLabelCounts.risk_exit || 0} • stale ${qualityLabelCounts.stale_entry_cancelled || 0}`;
                document.getElementById('val-learning-liveness').innerText =
                    learningLiveness.stale ? 'STALE' : 'LIVE';
                document.getElementById('val-learning-liveness-meta').innerText =
                    `replay ${(Number(learningLiveness.replay_processed_rate_24h || 0) * 100).toFixed(0)}% ??runs ${learningLiveness.replay_run_count_24h || 0}/${learningLiveness.replay_candidate_count_24h || 0}`;
                document.getElementById('val-learning-liveness-extra').innerText =
                    `promotion gate ${learningLiveness.promotion_blocked ? 'blocked' : 'open'} ??${(learningLiveness.stale_reasons || []).join(', ') || 'healthy'}`;
                document.getElementById('val-exec-contract').innerText = executionContractSummary.total_adjustments || 0;
                document.getElementById('val-exec-contract-meta').innerText =
                    `affected audits ${executionContractSummary.audits_affected || 0} • governed orders ${executionContractSummary.governed_orders || 0}`;
                document.getElementById('val-exec-contract-extra').innerText =
                    executionContractSummary.top_adjustment
                        ? `top adjustment ${executionContractSummary.top_adjustment} (${executionContractSummary.top_adjustment_count || 0}) • immediate ${executionContractSummary.immediate_entry_orders || 0} • reduce/exit ${executionContractSummary.reduce_exit_orders || 0}`
                        : `top adjustment none • immediate ${executionContractSummary.immediate_entry_orders || 0} • reduce/exit ${executionContractSummary.reduce_exit_orders || 0}`;
                document.getElementById('val-live-protection').innerText = liveProtectionSummary.open_order_count || 0;
                document.getElementById('val-live-protection-meta').innerText =
                    `positions ${liveProtectionSummary.position_count || 0} • protective ${liveProtectionSummary.protective_order_count || 0}`;
                document.getElementById('val-live-protection-extra').innerText =
                    `tp ${liveProtectionSummary.take_profit_count || 0} • sl ${liveProtectionSummary.stop_loss_count || 0} • orphan ${liveProtectionSummary.orphan_protective_order_count || 0}`;
                document.getElementById('val-divergence').innerText = divergenceSummary.drift_status || 'aligned';
                document.getElementById('val-divergence-meta').innerText =
                    `exchange ${divergenceSummary.exchange_open_order_count || 0} • runtime ${divergenceSummary.runtime_active_order_count || 0}`;
                document.getElementById('val-divergence-extra').innerText =
                    `cases ${divergenceSummary.active_trade_case_count || 0}/${divergenceSummary.exchange_position_count || 0} • unknown ${divergenceSummary.unknown_order_count_30m || 0}`;
                document.getElementById('val-decision-funnel').innerText = decisionFunnelSummary.total_audits || 0;
                document.getElementById('val-decision-funnel-meta').innerText =
                    `action ${decisionFunnelSummary.action_intent_count || 0} • approved ${decisionFunnelSummary.approved_count || 0} • executed ${decisionFunnelSummary.executed_count || 0}`;
                document.getElementById('val-decision-funnel-extra').innerText =
                    `filled ${decisionFunnelSummary.filled_count || 0} • labeled ${decisionFunnelSummary.labeled_count || 0}`;
                document.getElementById('val-runtime-freshness').innerText = runtimeFreshnessSummary.freshness_state || 'fresh';
                document.getElementById('val-runtime-freshness-meta').innerText =
                    `snapshot ${runtimeFreshnessSummary.snapshot_age_minutes ?? 'n/a'}m • audit ${runtimeFreshnessSummary.latest_audit_age_minutes ?? 'n/a'}m`;
                document.getElementById('val-runtime-freshness-extra').innerText =
                    `order ${runtimeFreshnessSummary.latest_order_state_age_minutes ?? 'n/a'}m • position ${runtimeFreshnessSummary.latest_position_state_age_minutes ?? 'n/a'}m`;
                document.getElementById('val-hold-reasons').innerText = holdReasonSummary.total_hold_count || 0;
                document.getElementById('val-hold-reasons-meta').innerText =
                    holdReasonSummary.top_reason
                        ? `top ${holdReasonSummary.top_reason} (${holdReasonSummary.top_count || 0})`
                        : 'top none';
                document.getElementById('val-hold-reasons-extra').innerText =
                    `fallback ${(holdReasonSummary.reason_counts || {}).fallback_hold || 0} • risk ${(holdReasonSummary.reason_counts || {}).risk_mode || 0} • exec ${(holdReasonSummary.reason_counts || {}).execution_quality || 0}`;
                document.getElementById('val-entry-styles').innerText = entryStyleSummary.total_entries || 0;
                document.getElementById('val-entry-styles-meta').innerText =
                    `settled ${entryStyleSummary.settled_entries || 0} • top style ${entryStyleSummary.top_style || 'none'}`;
                const retestSummary = styleRows.find(item => item.style === 'retest') || {};
                const valueSummary = styleRows.find(item => item.style === 'value') || {};
                document.getElementById('val-entry-styles-extra').innerText =
                    `retest ${(Number(retestSummary.win_rate || 0)).toFixed(1)}% win • value ${((Number(valueSummary.no_fill_rate || 0)) * 100).toFixed(1)}% no-fill`;
                document.getElementById('val-outcome-labels').innerText = outcomeSummary.total_labeled || 0;
                document.getElementById('val-outcome-labels-meta').innerText =
                    outcomeSummary.top_label
                        ? `top label ${outcomeSummary.top_label} (${outcomeSummary.top_count || 0})`
                        : 'top label none';
                document.getElementById('val-outcome-labels-extra').innerText =
                    `edge ${labelCounts.edge_failed || 0} • missed ${labelCounts.missed_due_to_no_fill || 0} • risk ${labelCounts.risk_exit || 0} • stale ${labelCounts.stale_entry_cancelled || 0}`;
                const rejectRate = data.total_audits_24h > 0
                    ? ((data.rejected_24h / data.total_audits_24h) * 100).toFixed(1) + '%'
                    : '0%';
                document.getElementById('val-rejections').innerText = rejectRate;
                const topSemanticIssue = ((semantic24h.top_issues || [])[0] || {});
                const topSemanticStage = ((semantic24h.top_validation_stages || [])[0] || {});
                document.getElementById('val-semantic-top-issue').innerText =
                    `Safe HOLD 24h ${semantic24h.safe_fallback_count || 0} • explanation ${semantic24h.explanation_fallback_count || 0} • top ${topSemanticIssue.issue || topSemanticStage.stage || '-'}${topSemanticIssue.count ? ` (${topSemanticIssue.count})` : topSemanticStage.count ? ` (${topSemanticStage.count})` : ''}`;
                const topSemanticModels = (semantic24h.top_models || [])
                    .map(item => {
                        const rate24 = Number(item.failure_rate || 0) * 100;
                        const model7d = (semantic7d.top_models || []).find(model => model.model_name === item.model_name) || {};
                        const model50 = (semanticLatest50.top_models || []).find(model => model.model_name === item.model_name) || {};
                        const rate7 = Number(model7d.failure_rate || 0) * 100;
                        const rate50 = Number(model50.failure_rate || 0) * 100;
                        return `${item.model_name} (24h ${rate24.toFixed(1)}%, 7d ${rate7.toFixed(1)}%, 50 ${rate50.toFixed(1)}%)`;
                    })
                    .join(' | ');
                document.getElementById('val-semantic-models').innerText =
                    `Models: ${topSemanticModels || '-'} • action schema ${semantic24h.action_schema_fallback_count || 0} • json ${semantic24h.json_parse_fallback_count || 0}`;
                const headerDescription = document.querySelector('.header p');
                if (headerDescription) {
                    headerDescription.innerText =
                        `Operational health, trade cases, retrieval outcomes, memory lifecycle, and full audit drill-down for execution review. Models: ${renderModelRouting(data.models)}`;
                }

                const tbody = document.getElementById('audit-table-body');
                tbody.innerHTML = '';
                data.recent_logs.forEach(log => {
                    const intent = parseJson(log.intent);
                    const decision = intent.decision || 'UNKNOWN';
                    const approved = !!log.risk_approved;
                    const badgeClass = approved ? 'ok' : 'bad';
                    const badgeText = approved ? 'Approved' : 'Rejected';
                    tbody.innerHTML += `
                        <tr class="audit-row" data-audit-id="${log.id}">
                            <td>${log.timestamp}</td>
                            <td><strong>${decision}</strong></td>
                            <td><span class="badge ${badgeClass}">${badgeText}</span></td>
                            <td>${log.risk_reason || '-'}</td>
                        </tr>
                    `;
                });
                if (!data.recent_logs.length) {
                    tbody.innerHTML = '<tr><td colspan="4">No audit logs yet.</td></tr>';
                } else {
                    document.querySelectorAll('.audit-row').forEach(row => {
                        row.addEventListener('click', () => loadAuditDetail(row.dataset.auditId));
                    });
                    loadAuditDetail(data.recent_logs[0].id);
                }

                renderList('memory-list', 'No memory rules saved yet.', data.recent_memory, item => {
                    const badgeClass = item.retired ? 'bad' : ((item.confidence_score || 0) >= 0.6 ? 'ok' : 'warn');
                    const applyWhen = item.apply_when || {};
                    const ignoreWhen = item.ignore_when || [];
                    return `
                        <div class="memory-item">
                            <span class="badge ${badgeClass}">${item.lifecycle_status || item.status || 'unknown'}</span>
                            <strong>${item.rule}</strong>
                            <div class="memory-meta">
                                support ${item.support || 0} • confidence ${(item.confidence_score || 0).toFixed(2)} • validity ${(item.validity_score || 0).toFixed(2)}
                                <br>regime ${item.regime || 'general'} • apply ${((applyWhen.trend || []).join('/') || 'n/a')} • ignore ${ignoreWhen[0] || 'n/a'}
                                <br>merge ${item.merge_count || 0} • revive ${item.revive_count || 0} • retirement ${item.retirement_reason || 'n/a'}
                            </div>
                        </div>
                    `;
                });

                renderTradeCaseList(data.recent_trade_cases || [], false);
                renderLiveProtectionPanel(liveProtectionSummary);
                renderHoldReasonPanel(holdReasonSummary);
                renderMemoryLifecyclePanel(memoryLifecycleSummary);

                renderList('retrieval-list', 'No retrieval evaluations saved yet.', data.recent_retrieval_evaluations || [], renderRetrievalEvaluationRow);

                renderList('fill-list', 'No synced fills yet.', data.recent_fills, fill => `
                    <div class="fill-item">
                        <strong>${fill.symbol || 'UNKNOWN'} ${fill.side || ''} ${fill.amount || 0}</strong>
                        <div class="fill-meta">
                            price ${fill.price || 0} • fee ${fill.fee_cost || 0} ${fill.fee_currency || ''} • pnl ${fill.realized_pnl || 0}
                        </div>
                    </div>
                `);

                renderList('ledger-list', 'No ledger rows yet.', data.recent_ledger, row => `
                    <div class="fill-item">
                        <strong>${row.symbol || 'UNKNOWN'} ${row.decision || ''}</strong>
                        <div class="fill-meta">
                            net pnl ${row.net_realized_pnl || 0} • fee ${row.fee_cost || 0} • fills ${row.fill_count || 0}
                        </div>
                    </div>
                `);

                renderList('symbol-journal-list', 'No symbol journal rows yet.', data.recent_symbol_journal || [], row => `
                    <div class="fill-item">
                        <strong>${row.symbol || 'UNKNOWN'} ${row.decision || 'UNKNOWN'} • audit ${row.audit_log_id || '-'}</strong>
                        <div class="fill-meta">
                            mode ${row.risk_mode || 'n/a'} • triggered ${row.triggered ? 'yes' : 'no'} • risk ${row.risk_triggered ? 'yes' : 'no'}
                            <br>est pnl ${row.estimated_pnl_pct ?? 'n/a'} • net pnl ${row.net_realized_pnl ?? 'n/a'} • signals ${(row.evidence_signals || []).slice(0, 3).join(', ') || 'none'}
                        </div>
                    </div>
                `);

                renderList('replay-list', 'No replay runs saved yet.', data.recent_replays, run => `
                    <div class="fill-item">
                        <strong>audit ${run.audit_log_id}: ${run.original_decision || 'UNKNOWN'} -> ${run.replayed_decision || 'UNKNOWN'}</strong>
                        <div class="fill-meta">
                            changed ${run.decision_changed ? 'yes' : 'no'} • side ${run.side_changed ? 'changed' : 'same'} • size ${run.size_delta || 0} • confidence ${run.confidence_delta || 0}
                            <br>${summarizeEvidenceDiff(run)}
                            <br>${summarizeReplayCounterfactual(run)}
                        </div>
                    </div>
                `);

                renderList('order-state-list', 'No order state transitions yet.', data.recent_order_states || [], row => `
                    <div class="fill-item">
                        <strong>${row.symbol || 'UNKNOWN'} • ${row.from_state || 'unknown'} -> ${row.to_state || 'unknown'}</strong>
                        <div class="fill-meta">
                            ${row.event_type || 'event'} • client ${row.client_order_id || '-'} • exchange ${row.exchange_order_id || '-'}
                        </div>
                    </div>
                `);

                renderList('position-state-list', 'No position state transitions yet.', data.recent_position_states || [], row => `
                    <div class="fill-item">
                        <strong>${row.symbol || 'UNKNOWN'} • ${row.from_state || 'unknown'} -> ${row.to_state || 'unknown'}</strong>
                        <div class="fill-meta">
                            ${row.event_type || 'event'} • audit ${row.audit_log_id || '-'}
                        </div>
                    </div>
                `);

                renderList('symbol-report-list', 'No symbol performance rows yet.', data.symbol_report || [], row => `
                    <div class="fill-item">
                        <strong>${row.symbol}: net ${row.net_realized_pnl}</strong>
                        <div class="fill-meta">
                            gross ${row.gross_realized_pnl} • fee ${row.fee_cost} • fills ${row.fill_count} • win rate ${row.win_rate}%
                        </div>
                    </div>
                `);

                renderList('daily-report-list', 'No daily ledger rows yet.', data.daily_report || [], row => `
                    <div class="fill-item">
                        <strong>${row.day}: net ${row.net_realized_pnl}</strong>
                        <div class="fill-meta">
                            gross ${row.gross_realized_pnl} • fee ${row.fee_cost} • rows ${row.row_count}
                        </div>
                    </div>
                `);

                renderList('entry-style-list', 'No entry-style evaluations yet.', styleRows, row => `
                    <div class="fill-item">
                        <strong>${row.style || 'unknown'} • entries ${row.entry_count || 0} • settled ${row.settled_count || 0}</strong>
                        <div class="fill-meta">
                            win ${(Number(row.win_rate || 0)).toFixed(1)}% • avg pnl ${(Number(row.avg_pnl_pct || 0)).toFixed(3)}%
                            <br>no-fill ${((Number(row.no_fill_rate || 0)) * 100).toFixed(1)}% (${row.no_fill_count || 0}) • missed on no-fill ${((Number(row.missed_move_rate || 0)) * 100).toFixed(1)}% (${row.missed_move_count || 0})
                        </div>
                    </div>
                `);

                renderList('outcome-label-list', 'No post-trade labels yet.', Object.entries(labelCounts).map(([label, count]) => ({ label, count })), row => `
                    <div class="fill-item">
                        <strong>${row.label}</strong>
                        <div class="fill-meta">count ${row.count || 0}</div>
                    </div>
                `);
            });
    </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/stats")
def api_stats():
    try:
        snapshot = _build_dashboard_snapshot()
        snapshot["models"] = _build_model_routing()
        return jsonify(snapshot)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/trade-cases")
def api_trade_cases():
    try:
        store = _get_state_store()
        load_all = str(request.args.get("all", "")).strip().lower() in {"1", "true", "yes", "y"}
        limit = request.args.get("limit", type=int)
        status = str(request.args.get("status", "")).strip() or None
        base_limit = max(1, min(limit or (5000 if load_all else 10), 20000 if load_all else 500))
        rows = store.get_recent_trade_cases(limit=base_limit, status=status)
        rows = _filter_trade_cases(rows, request.args)
        return jsonify({"trade_cases": rows, "full_history": load_all, "count": len(rows)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/audits/<int:audit_id>")
def api_audit_detail(audit_id: int):
    try:
        detail = _get_state_store().get_audit_log(audit_id)
        if detail is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(detail)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/audits/<int:audit_id>/replay", methods=["POST"])
def api_audit_replay(audit_id: int):
    try:
        payload = request.get_json(silent=True) or {}
        engine = build_replay_engine()
        store = _get_state_store()
        result = replay_and_optionally_save(
            store,
            audit_id=audit_id,
            engine=engine,
            save=bool(payload.get("save", True)),
            include_counterfactual=bool(payload.get("include_counterfactual", True)),
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except SystemExit as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
