import argparse
import copy
import json
import os
import re
from typing import Any, Dict, List, Set

from dotenv import load_dotenv

from ai.decision_engine import DecisionEngine
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
        "size_delta": round(replayed_size - original_size, 8),
        "confidence_delta": round(
            float(replayed.get("confidence", 0.0)) - float(original.get("confidence", 0.0)),
            6,
        ),
        "original_decision": original.get("decision"),
        "replayed_decision": replayed.get("decision"),
        "original_sides": original_sides,
        "replayed_sides": replayed_sides,
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


def replay_request_payload(request_payload: Dict[str, Any], gemini_key: str):
    engine = DecisionEngine(gemini_key)
    request = DecisionRequest.model_validate(request_payload)
    result = engine.generate_decision(request)
    return result.intent.model_dump(), result.raw_response


def replay_audit(store: StateStore, audit_id: int, gemini_key: str):
    audit = store.get_audit_log(audit_id)
    if audit is None:
        raise SystemExit(f"audit_id={audit_id} not found")

    request_payload = audit.get("decision_request") or {}
    if not request_payload:
        raise SystemExit(f"audit_id={audit_id} has no stored decision_request")

    replayed_intent, replayed_raw_response = replay_request_payload(request_payload, gemini_key=gemini_key)
    without_retrieval_payload = strip_retrieval_context(request_payload)
    without_retrieval_intent, without_retrieval_raw_response = replay_request_payload(
        without_retrieval_payload,
        gemini_key=gemini_key,
    )
    output = build_replay_payload(
        audit,
        audit_id,
        replayed_intent,
        replayed_raw_response,
        retrieval_counterfactual={
            "similar_past_case_count_with": len(request_payload.get("similar_past_cases") or []),
            "similar_past_case_count_without": len(without_retrieval_payload.get("similar_past_cases") or []),
            "with_retrieval_intent": replayed_intent,
            "without_retrieval_intent": without_retrieval_intent,
            "with_retrieval_raw_response": replayed_raw_response,
            "without_retrieval_raw_response": without_retrieval_raw_response,
            "with_vs_without_comparison": compare_intents(replayed_intent, without_retrieval_intent),
            "original_vs_without_comparison": compare_intents(audit.get("intent", {}), without_retrieval_intent),
        },
    )
    print(json.dumps(output, indent=2))
    return output


def replay_and_optionally_save(
    store: StateStore,
    audit_id: int,
    gemini_key: str,
    save: bool = False,
) -> Dict[str, Any]:
    payload = replay_audit(store, audit_id=audit_id, gemini_key=gemini_key)
    if save:
        store.save_replay_run(audit_id, payload)
    return payload


def build_replay_payload(
    audit: Dict[str, Any],
    audit_id: int,
    replayed_intent: Dict[str, Any],
    replayed_raw_response: str,
    retrieval_counterfactual: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "audit_id": audit_id,
        "timestamp": audit.get("timestamp"),
        "original_intent": audit.get("intent", {}),
        "replayed_intent": replayed_intent,
        "comparison": compare_intents(audit.get("intent", {}), replayed_intent),
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
    parser.add_argument("--limit", type=int, default=10, help="Number of audits to list")
    args = parser.parse_args()

    store = StateStore(args.db_path)
    if args.list:
        print_candidates(store, limit=args.limit)
        return

    if not args.audit_id:
        raise SystemExit("--audit-id is required unless --list is used")

    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        raise SystemExit("GEMINI_API_KEY is required for replay")

    replay_and_optionally_save(
        store,
        audit_id=args.audit_id,
        gemini_key=gemini_key,
        save=args.save,
    )


if __name__ == "__main__":
    main()
