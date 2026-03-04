import argparse
import json
import os
from typing import Any, Dict

from dotenv import load_dotenv

from ai.decision_engine import DecisionEngine
from core.models import DecisionRequest
from db.state_store import StateStore

load_dotenv()


def compare_intents(original: Dict[str, Any], replayed: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "decision_changed": original.get("decision") != replayed.get("decision"),
        "order_count_changed": len(original.get("orders", [])) != len(replayed.get("orders", [])),
        "confidence_delta": round(
            float(replayed.get("confidence", 0.0)) - float(original.get("confidence", 0.0)),
            6,
        ),
        "original_decision": original.get("decision"),
        "replayed_decision": replayed.get("decision"),
    }


def print_candidates(store: StateStore, limit: int):
    candidates = store.get_replay_candidates(limit=limit)
    for item in candidates:
        print(
            f"audit_id={item['id']} timestamp={item['timestamp']} "
            f"decision={item['intent'].get('decision', 'UNKNOWN')} risk_ok={item['risk_approved']}"
        )


def replay_audit(store: StateStore, audit_id: int, gemini_key: str):
    audit = store.get_audit_log(audit_id)
    if audit is None:
        raise SystemExit(f"audit_id={audit_id} not found")

    request_payload = audit.get("decision_request") or {}
    if not request_payload:
        raise SystemExit(f"audit_id={audit_id} has no stored decision_request")

    engine = DecisionEngine(gemini_key)
    request = DecisionRequest.model_validate(request_payload)
    result = engine.generate_decision(request)

    output = build_replay_payload(audit, audit_id, result.intent.model_dump(), result.raw_response)
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
