import argparse
import copy
import json
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
    market_snapshots = ((audit.get("market") or {}).get("market_snapshots") or {})
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
    output = build_replay_payload(
        audit,
        audit_id,
        replayed_intent,
        replayed_raw_response,
        retrieval_counterfactual=retrieval_counterfactual,
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


def run_incremental_replay_batch(
    store: StateStore,
    *,
    window_hours: int = 24,
    limit: int = 30,
    save: bool = True,
) -> Dict[str, Any]:
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=max(1, int(window_hours or 24)))
    candidates = store.get_replay_batch_candidates(since=since, limit=max(1, int(limit or 30)))
    if not candidates:
        return {"candidate_count": 0, "saved_count": 0, "replayed_audit_ids": []}
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
    return {
        "candidate_count": len(candidates),
        "saved_count": saved_count,
        "replayed_audit_ids": replayed_audit_ids,
    }


def build_replay_payload(
    audit: Dict[str, Any],
    audit_id: int,
    replayed_intent: Dict[str, Any],
    replayed_raw_response: str,
    retrieval_counterfactual: Dict[str, Any] | None = None,
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
    }


def main():
    parser = argparse.ArgumentParser(description="Replay a stored audit decision request.")
    parser.add_argument("--audit-id", type=int, help="Audit log ID to replay")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "data/gpt_trader.db"))
    parser.add_argument("--list", action="store_true", help="List recent replayable audits")
    parser.add_argument("--save", action="store_true", help="Persist replay comparison into replay_runs")
    parser.add_argument("--auto", action="store_true", help="Run incremental replay batch over recent audits")
    parser.add_argument("--window-hours", type=int, default=int(os.getenv("REPLAY_WINDOW_HOURS", "24") or 24))
    parser.add_argument("--limit", type=int, default=10, help="Number of audits to list")
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
