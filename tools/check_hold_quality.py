import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def _load_json(value):
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def summarize_hold_quality(db_path: str, limit: int) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS count FROM audit_logs")
    audit_count = int(cur.fetchone()["count"])
    cur.execute("SELECT COUNT(*) AS count FROM journal")
    journal_count = int(cur.fetchone()["count"])
    cur.execute("SELECT COUNT(*) AS count FROM replay_runs")
    replay_count = int(cur.fetchone()["count"])

    replay_quality = Counter()
    cur.execute("SELECT payload FROM replay_runs ORDER BY id DESC")
    for row in cur.fetchall():
        payload = _load_json(row["payload"])
        hold_quality = str(((payload.get("hold_opportunity_counterfactual") or {}).get("hold_quality") or "")).strip()
        replay_quality[hold_quality or "<none>"] += 1

    journal_quality = Counter()
    recent_rows = []
    cur.execute(
        """
        SELECT id, audit_log_id, decision, timestamp, rationale_json
        FROM journal
        ORDER BY id DESC
        """
    )
    for row in cur.fetchall():
        rationale_items = _load_json(row["rationale_json"])
        if not isinstance(rationale_items, list):
            rationale_items = []
        tagged_quality = ""
        tagged_source = ""
        tagged_detail = ""
        for item in rationale_items:
            if not isinstance(item, dict):
                continue
            hold_quality = str(item.get("hold_quality") or "").strip()
            if hold_quality:
                tagged_quality = hold_quality
                tagged_source = str(item.get("source") or "").strip()
                tagged_detail = str(item.get("hold_quality_detail") or "").strip()
                break
        journal_quality[tagged_quality or "<none>"] += 1
        if str(row["decision"] or "").upper() == "HOLD" and len(recent_rows) < limit:
            recent_rows.append(
                {
                    "journal_id": int(row["id"]),
                    "audit_log_id": int(row["audit_log_id"] or 0),
                    "timestamp": row["timestamp"],
                    "hold_quality": tagged_quality or None,
                    "hold_quality_detail": tagged_detail or None,
                    "source": tagged_source or None,
                }
            )

    print(f"db_path={db_path}")
    print(f"audit_logs={audit_count}")
    print(f"journal={journal_count}")
    print(f"replay_runs={replay_count}")
    print("replay_hold_quality_counts=" + json.dumps(dict(sorted(replay_quality.items())), ensure_ascii=False))
    print("journal_hold_quality_counts=" + json.dumps(dict(sorted(journal_quality.items())), ensure_ascii=False))
    print("recent_hold_rows=" + json.dumps(recent_rows, ensure_ascii=False))

    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize HOLD-quality coverage from replay and journal data.")
    parser.add_argument("--db-path", default="data/gpt_trader.db")
    parser.add_argument("--limit", type=int, default=8, help="How many recent HOLD journal rows to print")
    args = parser.parse_args()

    db_path = str(Path(args.db_path))
    summarize_hold_quality(db_path=db_path, limit=max(1, int(args.limit or 8)))


if __name__ == "__main__":
    main()
