import argparse
import copy
import json
import math
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Set

from dotenv import load_dotenv

from ai.decision_engine import DecisionEngine
from ai.llm_provider import normalize_provider_name, resolve_api_key, resolve_base_url
from core.models import DecisionRequest
from db.state_store import StateStore

load_dotenv()


STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "after",
    "before", "during", "over", "under", "risk", "mode", "signal", "price",
    "market", "position", "positions", "trade", "trading", "order", "orders",
    "setup", "hold", "exit", "reduce", "buy", "sell",
}

HOLD_OPPORTUNITY_WINDOWS = (
    ("30m", 30),
    ("2h", 120),
    ("6h", 360),
)
BASELINE_TAKER_FEE_BPS = 5.5


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_rationale_keywords(rationale: Any) -> List[str]:
    texts = rationale if isinstance(rationale, list) else [rationale]
    keywords: Set[str] = set()
    for text in texts:
        if not text:
            continue
        for token in re.findall(r"[a-zA-Z][a-zA-Z_-]{2,}", str(text).lower()):
            if token in STOPWORDS:
                continue
            keywords.add(token)
    return sorted(keywords)


def extract_order_sides(intent: Dict[str, Any]) -> List[str]:
    orders = intent.get("orders") or []
    sides = {str(order.get("action") or "").upper() for order in orders if order.get("action")}
    if not sides and intent.get("decision") in ("BUY", "SELL"):
        sides.add(str(intent.get("decision")).upper())
    return sorted(sides)


def extract_total_size(intent: Dict[str, Any]) -> float:
    total = 0.0
    for order in intent.get("orders") or []:
        total += abs(float(order.get("quantity") or 0.0))
    return round(total, 8)


def extract_order_types(intent: Dict[str, Any]) -> List[str]:
    orders = intent.get("orders") or []
    return sorted({str(order.get("order_type") or "").upper() for order in orders if order.get("order_type")})


def extract_execution_profiles(intent: Dict[str, Any]) -> List[str]:
    orders = intent.get("orders") or []
    return sorted({str(order.get("execution_profile") or "") for order in orders if order.get("execution_profile")})


def simulate_execution_metrics(audit: Dict[str, Any], intent: Dict[str, Any]) -> Dict[str, float]:
    market_state = audit.get("market_state") or audit.get("market") or {}
    market_snapshots = (market_state.get("market_snapshots") or {})
    orders = intent.get("orders") or []
    if not orders:
        return {"fill_ratio": 0.0, "slippage_bps": 0.0, "latency_seconds": 0.0}
    fill_ratios = []
    slippages = []
    latencies = []
    for order in orders:
        symbol = str(order.get("symbol") or "")
        snapshot = market_snapshots.get(symbol) or {}
        spread_percentile = float(snapshot.get("spread_percentile_1h") or 50.0)
        depth_percentile = float(snapshot.get("depth_percentile_1h") or 50.0)
        order_type = str(order.get("order_type") or "MARKET").upper()
        execution_profile = str(order.get("execution_profile") or "")
        if order_type == "LIMIT":
            fill_ratio = max(0.35, min(1.0, 0.92 - (spread_percentile / 250.0) + (depth_percentile / 200.0)))
            slippage_bps = max(0.5, 2.5 + (spread_percentile / 40.0) - (depth_percentile / 60.0))
            latency_seconds = max(1.0, 12.0 + (spread_percentile / 8.0) - (depth_percentile / 12.0))
        else:
            fill_ratio = max(0.9, min(1.0, 0.98 + (depth_percentile / 500.0)))
            slippage_bps = max(1.0, 4.0 + (spread_percentile / 18.0) - (depth_percentile / 80.0))
            latency_seconds = 1.0
        if execution_profile.startswith("market_urgent"):
            slippage_bps += 1.5
        if execution_profile.startswith("limit_passive"):
            fill_ratio = max(0.25, fill_ratio - 0.12)
            latency_seconds += 6.0
        fill_ratios.append(fill_ratio)
        slippages.append(slippage_bps)
        latencies.append(latency_seconds)
    return {
        "fill_ratio": round(sum(fill_ratios) / max(1, len(fill_ratios)), 4),
        "slippage_bps": round(sum(slippages) / max(1, len(slippages)), 4),
        "latency_seconds": round(sum(latencies) / max(1, len(latencies)), 4),
    }


def _reference_price_from_snapshot(snapshot: Dict[str, Any]) -> float | None:
    order_book = snapshot.get("order_book") or {}
    for candidate in (
        snapshot.get("current_price"),
        snapshot.get("mark_price"),
        order_book.get("mid_price"),
        snapshot.get("index_price"),
    ):
        try:
            if candidate is not None:
                return float(candidate)
        except (TypeError, ValueError):
            continue
    return None


def _extract_trigger_context(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    active_flags = [
        flag
        for flag, enabled in (
            ("volume_surge", bool(snapshot.get("volume_surge_trigger_active"))),
            ("fast_move", bool(snapshot.get("fast_move_trigger_active"))),
            ("range_break", bool(snapshot.get("range_break_trigger_active"))),
            ("opportunity", bool(snapshot.get("is_opportunity_trigger"))),
            ("risk", bool(snapshot.get("is_risk_trigger"))),
        )
        if enabled
    ]
    return {
        "active_flags": active_flags,
        "signature": "+".join(active_flags) if active_flags else "none",
    }


def _extract_execution_context(snapshot: Dict[str, Any]) -> Dict[str, str]:
    spread_percentile = _coerce_float(snapshot.get("spread_percentile_1h"))
    depth_percentile = _coerce_float(snapshot.get("depth_percentile_1h"))
    invalidation_distance = _coerce_float(snapshot.get("distance_to_invalid_level"))
    if spread_percentile is None:
        spread_bucket = "unknown_spread"
    elif spread_percentile <= 40:
        spread_bucket = "tight_spread"
    elif spread_percentile <= 70:
        spread_bucket = "acceptable_spread"
    else:
        spread_bucket = "wide_spread"
    if depth_percentile is None:
        depth_bucket = "unknown_depth"
    elif depth_percentile >= 60:
        depth_bucket = "deep_book"
    elif depth_percentile >= 25:
        depth_bucket = "tradable_depth"
    else:
        depth_bucket = "thin_book"
    if invalidation_distance is None:
        invalidation_bucket = "unknown_invalidation"
    elif invalidation_distance <= 0.8:
        invalidation_bucket = "tight_invalidation"
    elif invalidation_distance <= 1.5:
        invalidation_bucket = "manageable_invalidation"
    else:
        invalidation_bucket = "wide_invalidation"
    return {
        "spread_bucket": spread_bucket,
        "depth_bucket": depth_bucket,
        "invalidation_bucket": invalidation_bucket,
        "signature": f"{spread_bucket}|{depth_bucket}|{invalidation_bucket}",
    }


def _extract_trend_context(snapshot: Dict[str, Any]) -> Dict[str, str]:
    trend_1h = str(snapshot.get("trend_1h") or "").strip().upper() or "UNKNOWN"
    timeframe_15m = (snapshot.get("timeframes") or {}).get("15m") or {}
    trend_15m = str(timeframe_15m.get("trend") or "").strip().upper() or "UNKNOWN"
    return {
        "trend_1h": trend_1h,
        "trend_15m": trend_15m,
        "signature": f"{trend_1h}|{trend_15m}",
    }


def _compute_fill_price(
    *,
    reference_price: float,
    side: str,
    spread_bps: float,
    extra_slippage_bps: float,
) -> float:
    if reference_price <= 0:
        return 0.0
    half_spread_bps = max(0.0, spread_bps) * 0.5
    adjustment_bps = half_spread_bps + max(0.0, extra_slippage_bps)
    multiplier = 1.0 + (adjustment_bps / 10000.0)
    if side == "BUY":
        return reference_price * multiplier
    return reference_price * max(0.0, 1.0 - (adjustment_bps / 10000.0))


def _estimate_extra_slippage_bps(
    *,
    depth_percentile: float | None,
    spread_percentile: float | None,
    notional: float,
) -> float:
    depth_penalty = 0.0 if depth_percentile is None else max(0.0, (55.0 - depth_percentile) / 30.0)
    spread_penalty = 0.0 if spread_percentile is None else max(0.0, (spread_percentile - 40.0) / 50.0)
    size_penalty = min(1.5, max(0.0, notional / 250.0))
    return round(0.8 + depth_penalty + spread_penalty + size_penalty, 4)


def _iter_window_prices(window_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    for row in window_rows:
        price = None
        for candidate in (row.get("current_price"), row.get("mark_price"), row.get("mid_price")):
            coerced = _coerce_float(candidate)
            if coerced is not None and coerced > 0:
                price = coerced
                break
        if price is None:
            continue
        spread_bps = _coerce_float(row.get("spread_bps")) or 0.0
        snapshot = row.get("snapshot") or {}
        samples.append(
            {
                "timestamp": row.get("timestamp"),
                "price": price,
                "spread_bps": spread_bps,
                "snapshot": snapshot,
            }
        )
    return samples


def _infer_baseline_side(snapshot: Dict[str, Any]) -> str:
    long_score = 0.0
    short_score = 0.0
    trend_1h = str(snapshot.get("trend_1h") or "").upper()
    if "UP" in trend_1h:
        long_score += 1.0
    if "DOWN" in trend_1h:
        short_score += 1.0
    if bool(snapshot.get("range_break_trigger_active")):
        if "UP" in trend_1h:
            long_score += 0.8
        elif "DOWN" in trend_1h:
            short_score += 0.8
    timeframes = snapshot.get("timeframes") or {}
    for timeframe in ("15m", "1h"):
        payload = timeframes.get(timeframe) or {}
        trend = str(payload.get("trend") or "").upper()
        change = payload.get("change_3bar_pct")
        rsi = payload.get("rsi_14")
        if "UP" in trend:
            long_score += 0.4
        if "DOWN" in trend:
            short_score += 0.4
        try:
            change_value = float(change)
            if change_value > 0:
                long_score += 0.35
            elif change_value < 0:
                short_score += 0.35
        except (TypeError, ValueError):
            pass
        try:
            rsi_value = float(rsi)
            if rsi_value >= 55:
                long_score += 0.25
            elif rsi_value <= 45:
                short_score += 0.25
        except (TypeError, ValueError):
            pass
        if bool(payload.get("breakout_up")):
            long_score += 0.5
        if bool(payload.get("breakout_down")):
            short_score += 0.5
    if long_score <= 0 and short_score <= 0:
        return ""
    if abs(long_score - short_score) < 0.25:
        return ""
    return "BUY" if long_score > short_score else "SELL"


def build_hold_baseline_intent(audit: Dict[str, Any]) -> Dict[str, Any]:
    intent = audit.get("intent") or {}
    if str(intent.get("decision") or "").upper() != "HOLD":
        return {}
    if intent.get("orders"):
        return {}
    account_state = audit.get("account_state") or {}
    if account_state.get("positions") or account_state.get("open_orders") or int(account_state.get("open_orders_count") or 0) > 0:
        return {}
    market_state = audit.get("market_state") or audit.get("market") or {}
    market_snapshots = market_state.get("market_snapshots") or {}
    if not market_snapshots:
        return {}
    symbol, snapshot = next(iter(market_snapshots.items()))
    account_state = audit.get("account_state") or {}
    trigger_active = any(
        bool(snapshot.get(flag))
        for flag in (
            "is_opportunity_trigger",
            "volume_surge_trigger_active",
            "fast_move_trigger_active",
            "range_break_trigger_active",
        )
    )
    if not trigger_active:
        return {}
    spread_percentile = _coerce_float(snapshot.get("spread_percentile_1h")) or 50.0
    depth_percentile = _coerce_float(snapshot.get("depth_percentile_1h")) or 50.0
    invalidation_distance = _coerce_float(snapshot.get("distance_to_invalid_level"))
    volume_ratio = _coerce_float(snapshot.get("volume_ratio_15m")) or 0.0
    spread_ok = spread_percentile <= 70.0
    depth_ok = depth_percentile >= 25.0
    invalidation_ok = invalidation_distance is None or invalidation_distance <= 1.5
    volume_ok = volume_ratio >= 1.1
    if not (spread_ok and depth_ok and invalidation_ok and volume_ok):
        return {}
    side = _infer_baseline_side(snapshot)
    if side not in ("BUY", "SELL"):
        return {}
    entry_price = _reference_price_from_snapshot(snapshot) or float(snapshot.get("current_price") or 0.0)
    if entry_price <= 0:
        return {}
    available_balance = float(account_state.get("available_balance") or account_state.get("total_balance") or 0.0)
    notional_target = max(10.0, min(available_balance * 0.1, available_balance * 0.2))
    quantity = round(max(0.001, notional_target / entry_price), 6)
    atr_1h = _coerce_float(snapshot.get("atr_1h")) or 0.0
    stop_buffer = max(0.0035, ((atr_1h * max(0.6, invalidation_distance or 0.8)) / entry_price) if atr_1h > 0 else 0.005)
    take_profit_buffer = max(stop_buffer * 1.6, 0.006)
    extra_slippage_bps = _estimate_extra_slippage_bps(
        depth_percentile=depth_percentile,
        spread_percentile=spread_percentile,
        notional=notional_target,
    )
    assumed_entry_price = round(
        _compute_fill_price(
            reference_price=entry_price,
            side=side,
            spread_bps=_coerce_float((snapshot.get("order_book") or {}).get("spread_bps")) or 0.0,
            extra_slippage_bps=extra_slippage_bps,
        ),
        8,
    )
    if side == "BUY":
        stop_loss = round(assumed_entry_price * (1.0 - stop_buffer), 6)
        take_profit = round(assumed_entry_price * (1.0 + take_profit_buffer), 6)
    else:
        stop_loss = round(assumed_entry_price * (1.0 + stop_buffer), 6)
        take_profit = round(assumed_entry_price * (1.0 - take_profit_buffer), 6)
    return {
        "decision": side,
        "orders": [
            {
                "symbol": symbol,
                "action": side,
                "order_type": "MARKET",
                "quantity": quantity,
                "execution_profile": "market_urgent",
                "reduce_only": False,
                "take_profit": take_profit,
                "stop_loss": stop_loss,
                "assumed_entry_price": assumed_entry_price,
            }
        ],
        "confidence": 0.35,
        "rationale": [
            "Baseline counterfactual entry: trigger was active with acceptable spread/depth and manageable invalidation distance."
        ],
        "baseline_policy": {
            "trigger_active": trigger_active,
            "spread_percentile_1h": round(spread_percentile, 4),
            "depth_percentile_1h": round(depth_percentile, 4),
            "distance_to_invalid_level": round(invalidation_distance, 4) if invalidation_distance is not None else None,
            "volume_ratio_15m": round(volume_ratio, 4),
            "entry_reference_price": round(entry_price, 8),
            "assumed_entry_price": assumed_entry_price,
            "entry_slippage_bps": extra_slippage_bps,
            "taker_fee_bps": BASELINE_TAKER_FEE_BPS,
            "quantity": quantity,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
        },
    }


def build_simple_rule_hold_baseline_intent(audit: Dict[str, Any]) -> Dict[str, Any]:
    baseline_intent = build_hold_baseline_intent(audit)
    if not baseline_intent:
        return {}
    market_state = audit.get("market_state") or audit.get("market") or {}
    market_snapshots = market_state.get("market_snapshots") or {}
    symbol = str(((baseline_intent.get("orders") or [{}])[0]).get("symbol") or "")
    snapshot = market_snapshots.get(symbol) or {}
    trigger_context = _extract_trigger_context(snapshot)
    execution_context = _extract_execution_context(snapshot)
    trend_context = _extract_trend_context(snapshot)
    if "range_break" not in trigger_context["active_flags"] and "volume_surge" not in trigger_context["active_flags"]:
        return {}
    if execution_context["spread_bucket"] == "wide_spread" or execution_context["depth_bucket"] == "thin_book":
        return {}
    if trend_context["trend_1h"] not in {"UP", "DOWN"}:
        return {}
    intent_copy = copy.deepcopy(baseline_intent)
    intent_copy["confidence"] = 0.45
    intent_copy["rationale"] = [
        "Simple-rule counterfactual entry: strong trigger, aligned 1h trend, and tradable execution context."
    ]
    intent_copy["baseline_policy"]["policy_name"] = "simple_rule_entry"
    return intent_copy


def classify_hold_decision(audit: Dict[str, Any], baseline_intent: Dict[str, Any]) -> str:
    intent = audit.get("intent") or {}
    if str(intent.get("decision") or "").upper() != "HOLD":
        return ""
    market_state = audit.get("market_state") or audit.get("market") or {}
    market_snapshots = market_state.get("market_snapshots") or {}
    if not market_snapshots:
        return "unclear_hold"
    _, snapshot = next(iter(market_snapshots.items()))
    trigger_context = _extract_trigger_context(snapshot)
    execution_context = _extract_execution_context(snapshot)
    if baseline_intent:
        return "probe_candidate_hold"
    trigger_active = bool(trigger_context["active_flags"])
    if not trigger_active:
        return "no_trigger_hold"
    if bool(snapshot.get("is_risk_trigger")):
        return "risk_filtered_hold"
    if (
        execution_context["spread_bucket"] == "wide_spread"
        or execution_context["depth_bucket"] == "thin_book"
    ):
        return "execution_filtered_hold"
    return "structure_filtered_hold"


def _evaluate_counterfactual_windows(
    *,
    path_rows: List[Dict[str, Any]],
    side: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    move_threshold_pct: float,
    stop_threshold_pct: float,
    fee_bps: float,
) -> Dict[str, Any]:
    windows: Dict[str, Any] = {}
    missed_breakout_count = 0
    opportunity_costs: List[float] = []
    favorable_moves: List[float] = []
    for window_label, window_minutes in HOLD_OPPORTUNITY_WINDOWS:
        cutoff = path_rows[0]["_audit_timestamp"] + timedelta(minutes=window_minutes)
        window_rows = [row for row in path_rows if row["_parsed_timestamp"] <= cutoff]
        samples = _iter_window_prices(window_rows)
        if not samples:
            windows[window_label] = {"sample_count": 0}
            continue
        best_move_pct = 0.0
        worst_move_pct = 0.0
        exit_reason = "window_close"
        exit_fill_price = samples[-1]["price"]
        stop_hit = False
        take_profit_hit = False
        for sample in samples:
            sample_price = sample["price"]
            if side == "BUY":
                move_pct = ((sample_price - entry_price) / entry_price) * 100.0
            else:
                move_pct = ((entry_price - sample_price) / entry_price) * 100.0
            best_move_pct = max(best_move_pct, move_pct)
            worst_move_pct = min(worst_move_pct, move_pct)
            if side == "BUY" and sample_price <= stop_loss:
                stop_hit = True
                exit_reason = "stop_loss"
                exit_fill_price = _compute_fill_price(
                    reference_price=sample_price,
                    side="SELL",
                    spread_bps=sample["spread_bps"],
                    extra_slippage_bps=0.8,
                )
                break
            if side == "SELL" and sample_price >= stop_loss:
                stop_hit = True
                exit_reason = "stop_loss"
                exit_fill_price = _compute_fill_price(
                    reference_price=sample_price,
                    side="BUY",
                    spread_bps=sample["spread_bps"],
                    extra_slippage_bps=0.8,
                )
                break
            if side == "BUY" and sample_price >= take_profit:
                take_profit_hit = True
                exit_reason = "take_profit"
                exit_fill_price = _compute_fill_price(
                    reference_price=sample_price,
                    side="SELL",
                    spread_bps=sample["spread_bps"],
                    extra_slippage_bps=0.6,
                )
                break
            if side == "SELL" and sample_price <= take_profit:
                take_profit_hit = True
                exit_reason = "take_profit"
                exit_fill_price = _compute_fill_price(
                    reference_price=sample_price,
                    side="BUY",
                    spread_bps=sample["spread_bps"],
                    extra_slippage_bps=0.6,
                )
                break
        if not stop_hit and not take_profit_hit:
            exit_fill_price = _compute_fill_price(
                reference_price=samples[-1]["price"],
                side="SELL" if side == "BUY" else "BUY",
                spread_bps=samples[-1]["spread_bps"],
                extra_slippage_bps=0.8,
            )
        if side == "BUY":
            gross_realized_pct = ((exit_fill_price - entry_price) / entry_price) * 100.0
        else:
            gross_realized_pct = ((entry_price - exit_fill_price) / entry_price) * 100.0
        net_realized_pct = gross_realized_pct - ((fee_bps * 2.0) / 100.0)
        breakout_hit = (
            net_realized_pct >= move_threshold_pct
            and best_move_pct >= move_threshold_pct
            and worst_move_pct >= (-stop_threshold_pct * 1.15)
        )
        if breakout_hit:
            missed_breakout_count += 1
            favorable_moves.append(best_move_pct)
            opportunity_costs.append(max(0.0, net_realized_pct))
        windows[window_label] = {
            "sample_count": len(samples),
            "best_move_pct": round(best_move_pct, 4),
            "worst_move_pct": round(worst_move_pct, 4),
            "realized_move_pct": round(net_realized_pct, 4),
            "gross_realized_move_pct": round(gross_realized_pct, 4),
            "move_threshold_pct": move_threshold_pct,
            "stop_threshold_pct": stop_threshold_pct,
            "breakout_hit": breakout_hit,
            "stop_hit": stop_hit,
            "take_profit_hit": take_profit_hit,
            "exit_reason": exit_reason,
            "opportunity_cost_pct": round(max(0.0, net_realized_pct), 4),
        }
    return {
        "windows": windows,
        "missed_breakout_count": missed_breakout_count,
        "missed_move_median_pct": round(sorted(favorable_moves)[len(favorable_moves) // 2], 4) if favorable_moves else 0.0,
        "hold_opportunity_cost": round(sum(opportunity_costs), 4),
        "baseline_beats_hold": missed_breakout_count > 0,
    }


def _evaluate_reference_baseline(
    *,
    store: StateStore,
    audit_timestamp: datetime,
    symbol: str,
    snapshot: Dict[str, Any],
    baseline_intent: Dict[str, Any],
) -> Dict[str, Any]:
    order = (baseline_intent.get("orders") or [{}])[0]
    side = str(order.get("action") or baseline_intent.get("decision") or "").upper()
    entry_price = _coerce_float(order.get("assumed_entry_price")) or _reference_price_from_snapshot(snapshot)
    stop_loss = _coerce_float(order.get("stop_loss")) or 0.0
    take_profit = _coerce_float(order.get("take_profit")) or 0.0
    if side not in {"BUY", "SELL"} or not entry_price or stop_loss <= 0 or take_profit <= 0:
        return {}
    atr_1h = _coerce_float(snapshot.get("atr_1h")) or 0.0
    invalidation_distance = _coerce_float(snapshot.get("distance_to_invalid_level"))
    invalidation_pct = 0.0
    if invalidation_distance is not None and atr_1h > 0:
        invalidation_pct = max(0.0, (invalidation_distance * atr_1h / entry_price) * 100.0)
    move_threshold_pct = max(0.25, round(max(invalidation_pct, (atr_1h / entry_price) * 100.0 if atr_1h > 0 else 0.0) * 0.7, 4))
    stop_threshold_pct = max(0.2, round(max(invalidation_pct, move_threshold_pct * 0.65), 4))
    path_rows = store.get_market_path_between(
        symbol=symbol,
        start_timestamp=audit_timestamp,
        end_timestamp=audit_timestamp + timedelta(hours=6),
        limit=200,
    )
    normalized_rows: List[Dict[str, Any]] = []
    for row in path_rows:
        timestamp_text = str(row.get("timestamp") or "")
        try:
            parsed_timestamp = datetime.strptime(timestamp_text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        normalized_rows.append({**row, "_parsed_timestamp": parsed_timestamp, "_audit_timestamp": audit_timestamp})
    if not normalized_rows:
        return {}
    evaluation = _evaluate_counterfactual_windows(
        path_rows=normalized_rows,
        side=side,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        move_threshold_pct=move_threshold_pct,
        stop_threshold_pct=stop_threshold_pct,
        fee_bps=float((baseline_intent.get("baseline_policy") or {}).get("taker_fee_bps") or BASELINE_TAKER_FEE_BPS),
    )
    evaluation["entry_reference_price"] = round(entry_price, 8)
    evaluation["move_threshold_pct"] = move_threshold_pct
    evaluation["stop_threshold_pct"] = stop_threshold_pct
    return evaluation


def evaluate_hold_opportunity_counterfactual(store: StateStore, audit: Dict[str, Any]) -> Dict[str, Any]:
    baseline_intent = build_hold_baseline_intent(audit)
    simple_rule_intent = build_simple_rule_hold_baseline_intent(audit)
    hold_quality = classify_hold_decision(audit, baseline_intent)
    if not baseline_intent and not simple_rule_intent:
        return {"hold_quality": hold_quality} if hold_quality else {}
    market_state = audit.get("market_state") or audit.get("market") or {}
    market_snapshots = market_state.get("market_snapshots") or {}
    order = (baseline_intent.get("orders") or [{}])[0] if baseline_intent else (simple_rule_intent.get("orders") or [{}])[0]
    symbol = str(order.get("symbol") or "")
    snapshot = market_snapshots.get(symbol) or {}
    if not symbol:
        return {}
    audit_timestamp = datetime.strptime(str(audit.get("timestamp")), "%Y-%m-%d %H:%M:%S")
    primary_evaluation = (
        _evaluate_reference_baseline(
            store=store,
            audit_timestamp=audit_timestamp,
            symbol=symbol,
            snapshot=snapshot,
            baseline_intent=baseline_intent,
        )
        if baseline_intent
        else {}
    )
    simple_rule_evaluation = (
        _evaluate_reference_baseline(
            store=store,
            audit_timestamp=audit_timestamp,
            symbol=symbol,
            snapshot=snapshot,
            baseline_intent=simple_rule_intent,
        )
        if simple_rule_intent
        else {}
    )
    if simple_rule_evaluation.get("baseline_beats_hold"):
        hold_quality = "missed_opportunity_hold"
    elif primary_evaluation.get("baseline_beats_hold"):
        hold_quality = "probe_only_opportunity_hold"
    side = str(order.get("action") or (baseline_intent or simple_rule_intent).get("decision") or "").upper()

    return {
        "baseline_intent": baseline_intent,
        "simple_rule_baseline_intent": simple_rule_intent,
        "hold_quality": hold_quality,
        "symbol": symbol,
        "entry_side": side,
        "entry_reference_price": primary_evaluation.get("entry_reference_price"),
        "move_threshold_pct": primary_evaluation.get("move_threshold_pct"),
        "stop_threshold_pct": primary_evaluation.get("stop_threshold_pct"),
        "windows": primary_evaluation.get("windows") or {},
        "missed_breakout_count": int(primary_evaluation.get("missed_breakout_count") or 0),
        "missed_move_median_pct": float(primary_evaluation.get("missed_move_median_pct") or 0.0),
        "hold_opportunity_cost": float(primary_evaluation.get("hold_opportunity_cost") or 0.0),
        "baseline_beats_hold": bool(primary_evaluation.get("baseline_beats_hold")),
        "simple_rule_baseline_beats_hold": bool(simple_rule_evaluation.get("baseline_beats_hold")),
        "reference_policy_comparison": {
            "probe_entry": {
                "beats_hold": bool(primary_evaluation.get("baseline_beats_hold")),
                "hold_opportunity_cost": float(primary_evaluation.get("hold_opportunity_cost") or 0.0),
            },
            "simple_rule_entry": {
                "beats_hold": bool(simple_rule_evaluation.get("baseline_beats_hold")),
                "hold_opportunity_cost": float(simple_rule_evaluation.get("hold_opportunity_cost") or 0.0),
            },
        },
        "trigger_context": _extract_trigger_context(snapshot),
        "execution_context": _extract_execution_context(snapshot),
        "trend_context": _extract_trend_context(snapshot),
    }


def extract_evidence_signals(intent: Dict[str, Any]) -> List[str]:
    signals: Set[str] = set()
    for item in intent.get("rationale_items") or []:
        for evidence in (item or {}).get("evidence") or []:
            signal = evidence.get("signal") or "signal"
            timeframe = evidence.get("timeframe") or "context"
            symbol = evidence.get("symbol")
            if symbol:
                signals.add(f"{symbol}:{signal}@{timeframe}")
            else:
                signals.add(f"{signal}@{timeframe}")
    return sorted(signals)


def extract_evidence_by_source(intent: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in intent.get("rationale_items") or []:
        for evidence in (item or {}).get("evidence") or []:
            source = str(evidence.get("source") or "").strip()
            if not source:
                continue
            grouped.setdefault(source, []).append(
                {
                    "signal": evidence.get("signal"),
                    "symbol": evidence.get("symbol"),
                    "timeframe": evidence.get("timeframe"),
                    "observed_value": evidence.get("observed_value"),
                    "threshold": evidence.get("threshold"),
                    "operator": evidence.get("operator"),
                }
            )
    return grouped


def compare_intents(original: Dict[str, Any], replayed: Dict[str, Any]) -> Dict[str, Any]:
    original_sides = extract_order_sides(original)
    replayed_sides = extract_order_sides(replayed)
    original_order_types = extract_order_types(original)
    replayed_order_types = extract_order_types(replayed)
    original_execution_profiles = extract_execution_profiles(original)
    replayed_execution_profiles = extract_execution_profiles(replayed)
    original_size = extract_total_size(original)
    replayed_size = extract_total_size(replayed)
    original_keywords = extract_rationale_keywords(original.get("rationale") or [])
    replayed_keywords = extract_rationale_keywords(replayed.get("rationale") or [])
    original_evidence_signals = extract_evidence_signals(original)
    replayed_evidence_signals = extract_evidence_signals(replayed)
    original_evidence_by_source = extract_evidence_by_source(original)
    replayed_evidence_by_source = extract_evidence_by_source(replayed)
    evidence_source_diffs = []
    for source in sorted(set(original_evidence_by_source) | set(replayed_evidence_by_source)):
        original_rows = original_evidence_by_source.get(source, [])
        replayed_rows = replayed_evidence_by_source.get(source, [])
        if original_rows == replayed_rows:
            continue
        evidence_source_diffs.append(
            {
                "source": source,
                "original": original_rows,
                "replayed": replayed_rows,
            }
        )
    return {
        "decision_changed": original.get("decision") != replayed.get("decision"),
        "order_count_changed": len(original.get("orders", [])) != len(replayed.get("orders", [])),
        "side_changed": original_sides != replayed_sides,
        "order_type_changed": original_order_types != replayed_order_types,
        "execution_profile_changed": original_execution_profiles != replayed_execution_profiles,
        "size_delta": round(replayed_size - original_size, 8),
        "confidence_delta": round(
            float(replayed.get("confidence", 0.0)) - float(original.get("confidence", 0.0)),
            6,
        ),
        "original_decision": original.get("decision"),
        "replayed_decision": replayed.get("decision"),
        "original_sides": original_sides,
        "replayed_sides": replayed_sides,
        "original_order_types": original_order_types,
        "replayed_order_types": replayed_order_types,
        "original_execution_profiles": original_execution_profiles,
        "replayed_execution_profiles": replayed_execution_profiles,
        "original_total_size": original_size,
        "replayed_total_size": replayed_size,
        "rationale_keywords_added": sorted(set(replayed_keywords) - set(original_keywords)),
        "rationale_keywords_removed": sorted(set(original_keywords) - set(replayed_keywords)),
        "original_evidence_signal_count": len(original_evidence_signals),
        "replayed_evidence_signal_count": len(replayed_evidence_signals),
        "evidence_signal_count_delta": len(replayed_evidence_signals) - len(original_evidence_signals),
        "evidence_signals_added": sorted(set(replayed_evidence_signals) - set(original_evidence_signals)),
        "evidence_signals_removed": sorted(set(original_evidence_signals) - set(replayed_evidence_signals)),
        "evidence_source_diffs": evidence_source_diffs,
    }


def print_candidates(store: StateStore, limit: int):
    candidates = store.get_replay_candidates(limit=limit)
    for item in candidates:
        print(
            f"audit_id={item['id']} timestamp={item['timestamp']} "
            f"decision={item['intent'].get('decision', 'UNKNOWN')} risk_ok={item['risk_approved']}"
        )


def strip_retrieval_context(request_payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = copy.deepcopy(request_payload or {})
    payload["similar_past_cases"] = []
    return payload


def build_replay_engine() -> DecisionEngine:
    provider = normalize_provider_name(os.getenv("REPLAY_PROVIDER") or os.getenv("AI_PROVIDER"))
    api_key = resolve_api_key(
        provider,
        explicit_api_key=os.getenv("REPLAY_API_KEY"),
        shared_api_key=os.getenv("AI_API_KEY"),
    )
    base_url = resolve_base_url(
        provider,
        explicit_base_url=os.getenv("REPLAY_BASE_URL"),
        shared_base_url=os.getenv("AI_BASE_URL"),
    )
    replay_model = (
        os.getenv("REPLAY_MODEL", "").strip()
        or os.getenv("MEMORY_MODEL", "").strip()
        or os.getenv("AI_DEFAULT_MODEL", "").strip()
        or os.getenv("GEMINI_MEMORY_MODEL", "").strip()
        or os.getenv("GEMINI_DEFAULT_MODEL", "").strip()
    )
    return DecisionEngine(
        api_key=api_key,
        cache_store=None,
        provider=provider,
        base_url=base_url,
        default_model=replay_model or None,
        trigger_model=replay_model or None,
    )


def replay_request_payload(request_payload: Dict[str, Any], engine: DecisionEngine):
    request = DecisionRequest.model_validate(request_payload)
    result = engine.generate_decision(request)
    return result.intent.model_dump(), result.raw_response


def should_run_retrieval_counterfactual(audit: Dict[str, Any], request_payload: Dict[str, Any]) -> bool:
    similar_cases = list(request_payload.get("similar_past_cases") or [])
    if not similar_cases:
        return False
    intent = audit.get("intent") or {}
    decision = str(intent.get("decision") or "").upper()
    risk_ok = bool(audit.get("risk_approved"))
    return decision in ("BUY", "SELL", "REDUCE", "EXIT") or (not risk_ok and decision in ("BUY", "SELL"))


def replay_audit(store: StateStore, audit_id: int, engine: DecisionEngine, include_counterfactual: bool = True):
    audit = store.get_audit_log(audit_id)
    if audit is None:
        raise SystemExit(f"audit_id={audit_id} not found")

    request_payload = audit.get("decision_request") or {}
    if not request_payload:
        raise SystemExit(f"audit_id={audit_id} has no stored decision_request")

    replayed_intent, replayed_raw_response = replay_request_payload(request_payload, engine=engine)
    retrieval_counterfactual = {}
    if include_counterfactual and should_run_retrieval_counterfactual(audit, request_payload):
        without_retrieval_payload = strip_retrieval_context(request_payload)
        without_retrieval_intent, without_retrieval_raw_response = replay_request_payload(
            without_retrieval_payload,
            engine=engine,
        )
        retrieval_counterfactual = {
            "similar_past_case_count_with": len(request_payload.get("similar_past_cases") or []),
            "similar_past_case_count_without": len(without_retrieval_payload.get("similar_past_cases") or []),
            "with_retrieval_intent": replayed_intent,
            "without_retrieval_intent": without_retrieval_intent,
            "with_retrieval_raw_response": replayed_raw_response,
            "without_retrieval_raw_response": without_retrieval_raw_response,
            "with_vs_without_comparison": compare_intents(replayed_intent, without_retrieval_intent),
            "original_vs_without_comparison": compare_intents(audit.get("intent", {}), without_retrieval_intent),
        }
    hold_opportunity_counterfactual = evaluate_hold_opportunity_counterfactual(store, audit)
    output = build_replay_payload(
        audit,
        audit_id,
        replayed_intent,
        replayed_raw_response,
        retrieval_counterfactual=retrieval_counterfactual,
        hold_opportunity_counterfactual=hold_opportunity_counterfactual,
    )
    return output


def replay_and_optionally_save(
    store: StateStore,
    audit_id: int,
    engine: DecisionEngine,
    save: bool = False,
    include_counterfactual: bool = True,
) -> Dict[str, Any]:
    payload = replay_audit(
        store,
        audit_id=audit_id,
        engine=engine,
        include_counterfactual=include_counterfactual,
    )
    if save:
        store.save_replay_run(audit_id, payload)
    return payload


def resolve_incremental_replay_limit(
    *,
    candidate_count: int,
    base_limit: int,
    max_limit: int,
    coverage_target: float = 0.5,
) -> int:
    candidate_count = max(0, int(candidate_count or 0))
    if candidate_count <= 0:
        return 0
    base_limit = max(1, int(base_limit or 1))
    max_limit = max(base_limit, int(max_limit or base_limit))
    coverage_target = min(1.0, max(0.1, float(coverage_target or 0.5)))
    scaled_limit = int(math.ceil(candidate_count * coverage_target))
    return min(candidate_count, max_limit, max(base_limit, scaled_limit))


def run_incremental_replay_batch(
    store: StateStore,
    *,
    window_hours: int = 24,
    limit: int = 30,
    max_limit: int | None = None,
    coverage_target: float = 0.5,
    hold_quota: int = 0,
    candidate_pool_limit: int | None = None,
    save: bool = True,
) -> Dict[str, Any]:
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=max(1, int(window_hours or 24)))
    base_limit = max(1, int(limit or 30))
    max_limit = max(base_limit, int(max_limit or max(base_limit, base_limit * 2)))
    hold_quota = max(0, min(int(hold_quota or 0), max_limit))
    candidate_pool_limit = max(
        max_limit,
        int(candidate_pool_limit or max(120, max_limit * 4)),
    )
    candidate_pool = store.get_replay_batch_candidates(
        since=since,
        limit=candidate_pool_limit,
        hold_quota=0,
    )
    effective_limit = resolve_incremental_replay_limit(
        candidate_count=len(candidate_pool),
        base_limit=base_limit,
        max_limit=max_limit,
        coverage_target=coverage_target,
    )
    if effective_limit <= 0:
        return {"candidate_count": 0, "saved_count": 0, "replayed_audit_ids": []}
    candidates = store.get_replay_batch_candidates(
        since=since,
        limit=effective_limit,
        hold_quota=hold_quota,
    )
    engine = build_replay_engine()
    replayed_audit_ids: List[int] = []
    saved_count = 0
    for candidate in candidates:
        audit_id = int(candidate.get("id") or 0)
        if not audit_id:
            continue
        include_counterfactual = bool((candidate.get("intent") or {}).get("decision") != "HOLD")
        payload = replay_and_optionally_save(
            store,
            audit_id=audit_id,
            engine=engine,
            save=save,
            include_counterfactual=include_counterfactual,
        )
        replayed_audit_ids.append(audit_id)
        if save and payload:
            saved_count += 1
    summary = {
        "candidate_count": len(candidate_pool),
        "selected_count": len(candidates),
        "effective_limit": effective_limit,
        "base_limit": base_limit,
        "max_limit": max_limit,
        "hold_quota": hold_quota,
        "saved_count": saved_count,
        "replayed_audit_ids": replayed_audit_ids,
    }
    if hasattr(store, "upsert_learning_artifact"):
        store.upsert_learning_artifact(
            "replay_batch_heartbeat",
            {
                **summary,
                "window_hours": max(1, int(window_hours or 24)),
                "coverage_target": round(float(coverage_target), 4),
                "executed_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
            },
            artifact_type="heartbeat",
        )
    return summary


def build_replay_payload(
    audit: Dict[str, Any],
    audit_id: int,
    replayed_intent: Dict[str, Any],
    replayed_raw_response: str,
    retrieval_counterfactual: Dict[str, Any] | None = None,
    hold_opportunity_counterfactual: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    simulated = simulate_execution_metrics(audit, replayed_intent)
    return {
        "audit_id": audit_id,
        "timestamp": audit.get("timestamp"),
        "original_intent": audit.get("intent", {}),
        "replayed_intent": replayed_intent,
        "comparison": compare_intents(audit.get("intent", {}), replayed_intent),
        "simulated_fill_ratio": simulated["fill_ratio"],
        "simulated_slippage_bps": simulated["slippage_bps"],
        "simulated_latency_seconds": simulated["latency_seconds"],
        "original_raw_response": audit.get("ai_raw_response", ""),
        "replayed_raw_response": replayed_raw_response,
        "risk_reason": audit.get("risk_reason"),
        "retrieval_counterfactual": retrieval_counterfactual or {},
        "hold_opportunity_counterfactual": hold_opportunity_counterfactual or {},
    }


def main():
    parser = argparse.ArgumentParser(description="Replay a stored audit decision request.")
    parser.add_argument("--audit-id", type=int, help="Audit log ID to replay")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "data/gpt_trader.db"))
    parser.add_argument("--list", action="store_true", help="List recent replayable audits")
    parser.add_argument("--save", action="store_true", help="Persist replay comparison into replay_runs")
    parser.add_argument("--auto", action="store_true", help="Run incremental replay batch over recent audits")
    parser.add_argument("--window-hours", type=int, default=int(os.getenv("REPLAY_WINDOW_HOURS", "24") or 24))
    parser.add_argument("--limit", type=int, default=int(os.getenv("REPLAY_LIMIT", "30") or 30), help="Number of audits to list")
    parser.add_argument("--max-limit", type=int, default=int(os.getenv("REPLAY_MAX_LIMIT", "60") or 60))
    parser.add_argument("--coverage-target", type=float, default=float(os.getenv("REPLAY_COVERAGE_TARGET", "0.5") or 0.5))
    parser.add_argument("--hold-quota", type=int, default=int(os.getenv("REPLAY_HOLD_QUOTA", "8") or 8))
    parser.add_argument(
        "--candidate-pool-limit",
        type=int,
        default=int(os.getenv("REPLAY_CANDIDATE_POOL_LIMIT", "240") or 240),
    )
    args = parser.parse_args()

    store = StateStore(args.db_path)
    if args.list:
        print_candidates(store, limit=args.limit)
        return

    if args.auto:
        summary = run_incremental_replay_batch(
            store,
            window_hours=args.window_hours,
            limit=args.limit,
            max_limit=args.max_limit,
            coverage_target=args.coverage_target,
            hold_quota=args.hold_quota,
            candidate_pool_limit=args.candidate_pool_limit,
            save=args.save,
        )
        print(json.dumps(summary, indent=2))
        return

    if not args.audit_id:
        raise SystemExit("--audit-id is required unless --list or --auto is used")

    engine = build_replay_engine()
    payload = replay_and_optionally_save(
        store,
        audit_id=args.audit_id,
        engine=engine,
        save=args.save,
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
