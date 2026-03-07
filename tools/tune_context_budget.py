import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import yaml


DEFAULT_CONTEXT_BUDGET = {
    "journal_fetch_limit": 24,
    "journal_keep_limit": 6,
    "memory_keep_limit": 8,
    "similar_case_keep_limit": 5,
    "max_rationale_items_per_journal": 2,
    "max_evidence_per_rationale_item": 2,
    "max_ignore_when_per_memory": 2,
}


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int(round((p / 100.0) * (len(ordered) - 1)))
    idx = max(0, min(len(ordered) - 1, idx))
    return float(ordered[idx])


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def safe_json_loads(raw: Any, default: Any):
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def collect_usage_samples(db_path: str, lookback: int) -> Dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, decision_request, execution_summary
        FROM audit_logs
        WHERE decision_request IS NOT NULL
        ORDER BY id DESC
        LIMIT ?
        """,
        (lookback,),
    ).fetchall()
    conn.close()

    token_counts: List[float] = []
    journal_counts: List[int] = []
    memory_counts: List[int] = []
    similar_counts: List[int] = []
    semantic_failures = 0

    for row in rows:
        request = safe_json_loads(row["decision_request"], {})
        execution_summary = safe_json_loads(row["execution_summary"], {})
        ai_usage = execution_summary.get("ai_usage") or {}
        total_tokens = ai_usage.get("total_token_count")
        if isinstance(total_tokens, (int, float)):
            token_counts.append(float(total_tokens))
        if ai_usage.get("semantic_validation_error"):
            semantic_failures += 1

        journal_counts.append(len(request.get("recent_journal_context") or []))
        memory_counts.append(len(request.get("long_term_memory") or []))
        similar_counts.append(len(request.get("similar_past_cases") or []))

    sample_count = len(rows)
    semantic_failure_rate = (semantic_failures / sample_count) if sample_count > 0 else 0.0
    return {
        "sample_count": sample_count,
        "semantic_failure_rate": semantic_failure_rate,
        "tokens": {
            "count": len(token_counts),
            "avg": round(sum(token_counts) / len(token_counts), 2) if token_counts else 0.0,
            "p50": percentile(token_counts, 50),
            "p75": percentile(token_counts, 75),
            "p90": percentile(token_counts, 90),
            "max": max(token_counts) if token_counts else 0.0,
        },
        "context": {
            "journal_p75": int(percentile([float(x) for x in journal_counts], 75)) if journal_counts else 0,
            "memory_p75": int(percentile([float(x) for x in memory_counts], 75)) if memory_counts else 0,
            "similar_p75": int(percentile([float(x) for x in similar_counts], 75)) if similar_counts else 0,
            "journal_avg": round((sum(journal_counts) / len(journal_counts)), 2) if journal_counts else 0.0,
            "memory_avg": round((sum(memory_counts) / len(memory_counts)), 2) if memory_counts else 0.0,
            "similar_avg": round((sum(similar_counts) / len(similar_counts)), 2) if similar_counts else 0.0,
        },
    }


def load_policy(path: str) -> Dict[str, Any]:
    if not Path(path).exists():
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def recommend_budget(
    current_budget: Dict[str, Any],
    usage: Dict[str, Any],
    target_token_p75: int,
) -> Dict[str, int]:
    baseline = dict(DEFAULT_CONTEXT_BUDGET)
    baseline.update(current_budget or {})

    token_p75 = float((usage.get("tokens") or {}).get("p75") or 0.0)
    semantic_failure_rate = float(usage.get("semantic_failure_rate") or 0.0)
    context = usage.get("context") or {}

    if token_p75 <= 0:
        return {
            "journal_fetch_limit": int(baseline["journal_fetch_limit"]),
            "journal_keep_limit": int(baseline["journal_keep_limit"]),
            "memory_keep_limit": int(baseline["memory_keep_limit"]),
            "similar_case_keep_limit": int(baseline["similar_case_keep_limit"]),
            "max_rationale_items_per_journal": int(baseline["max_rationale_items_per_journal"]),
            "max_evidence_per_rationale_item": int(baseline["max_evidence_per_rationale_item"]),
            "max_ignore_when_per_memory": int(baseline["max_ignore_when_per_memory"]),
        }

    ratio = float(target_token_p75) / token_p75
    if semantic_failure_rate >= 0.1:
        ratio = max(ratio, 0.9)
    ratio = max(0.65, min(1.15, ratio))

    candidate_journal_keep = clamp(
        round((context.get("journal_p75") or baseline["journal_keep_limit"]) * ratio),
        4,
        10,
    )
    candidate_memory_keep = clamp(
        round((context.get("memory_p75") or baseline["memory_keep_limit"]) * ratio),
        5,
        12,
    )
    candidate_similar_keep = clamp(
        round((context.get("similar_p75") or baseline["similar_case_keep_limit"]) * ratio),
        3,
        8,
    )

    # Smooth step from current policy to avoid abrupt quality shifts.
    alpha = 0.5
    smoothed_journal_keep = clamp(
        round((1 - alpha) * int(baseline["journal_keep_limit"]) + alpha * candidate_journal_keep),
        4,
        10,
    )
    smoothed_memory_keep = clamp(
        round((1 - alpha) * int(baseline["memory_keep_limit"]) + alpha * candidate_memory_keep),
        5,
        12,
    )
    smoothed_similar_keep = clamp(
        round((1 - alpha) * int(baseline["similar_case_keep_limit"]) + alpha * candidate_similar_keep),
        3,
        8,
    )
    journal_fetch_limit = clamp(max(smoothed_journal_keep + 10, smoothed_journal_keep * 4), 12, 48)

    max_rationale_items_per_journal = 2
    max_evidence_per_rationale_item = 2
    if semantic_failure_rate >= 0.12 and token_p75 <= (target_token_p75 * 0.9):
        max_rationale_items_per_journal = 3
        max_evidence_per_rationale_item = 3

    return {
        "journal_fetch_limit": int(journal_fetch_limit),
        "journal_keep_limit": int(smoothed_journal_keep),
        "memory_keep_limit": int(smoothed_memory_keep),
        "similar_case_keep_limit": int(smoothed_similar_keep),
        "max_rationale_items_per_journal": int(max_rationale_items_per_journal),
        "max_evidence_per_rationale_item": int(max_evidence_per_rationale_item),
        "max_ignore_when_per_memory": int(baseline.get("max_ignore_when_per_memory", 2)),
    }


def maybe_write_policy(policy_path: str, recommended: Dict[str, int]) -> None:
    policy = load_policy(policy_path)
    policy["context_budget"] = recommended
    with open(policy_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(policy, handle, sort_keys=False, allow_unicode=True)


def main():
    parser = argparse.ArgumentParser(description="Recommend context_budget values from recent audit logs.")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "data/gpt_trader.db"))
    parser.add_argument("--policy-path", default="policy.yaml")
    parser.add_argument("--lookback", type=int, default=400, help="Number of recent audits to analyze.")
    parser.add_argument("--target-token-p75", type=int, default=2800, help="Target p75 total token count.")
    parser.add_argument("--write-policy", action="store_true", help="Write recommended context_budget to policy.yaml.")
    parser.add_argument("--json", action="store_true", help="Print report as JSON.")
    args = parser.parse_args()

    db_path = str(args.db_path)
    policy = load_policy(args.policy_path)
    current_budget = (policy or {}).get("context_budget") or {}

    if not Path(db_path).exists():
        print(f"DB not found: {db_path}")
        print("Using current policy context_budget as recommendation.")
        recommended = recommend_budget(
            current_budget=current_budget,
            usage={"tokens": {"p75": 0}, "semantic_failure_rate": 0.0, "context": {}},
            target_token_p75=args.target_token_p75,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "db_found": False,
                        "recommended_context_budget": recommended,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            print("recommended_context_budget:")
            print(yaml.safe_dump(recommended, sort_keys=False, allow_unicode=True))
        if args.write_policy:
            maybe_write_policy(args.policy_path, recommended)
            print(f"Updated {args.policy_path}")
        return

    usage = collect_usage_samples(db_path=db_path, lookback=args.lookback)
    recommended = recommend_budget(
        current_budget=current_budget,
        usage=usage,
        target_token_p75=args.target_token_p75,
    )

    report = {
        "db_found": True,
        "db_path": db_path,
        "lookback": args.lookback,
        "target_token_p75": args.target_token_p75,
        "usage_summary": usage,
        "current_context_budget": current_budget or DEFAULT_CONTEXT_BUDGET,
        "recommended_context_budget": recommended,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print("usage_summary:")
        print(yaml.safe_dump(usage, sort_keys=False, allow_unicode=True))
        print("recommended_context_budget:")
        print(yaml.safe_dump(recommended, sort_keys=False, allow_unicode=True))

    if args.write_policy:
        maybe_write_policy(args.policy_path, recommended)
        print(f"Updated {args.policy_path}")


if __name__ == "__main__":
    main()
