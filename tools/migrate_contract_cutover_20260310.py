import argparse
import json
import sqlite3
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List


CUTOVER_DATE = "2026-03-10"
CUTOVER_DATETIME = "2026-03-10 00:00:00"
LEGACY_POSITION_CONFIG_KEYS = (
    "weakened_reduce_pct",
    "profit_reduce_trigger_pct",
    "profit_reduce_fraction",
)
LEGACY_SAFETY_CONFIG_KEYS = (
    "profit_protect_trigger_pct",
    "profit_protect_reduce_fraction",
)
LEGACY_POSITION_RULE_MARKERS = (
    "reduce the position",
    "reduce exposure",
    "stalled reduce",
    "scale out",
    "partial take profit",
    "partial profit",
    "profit reduce",
)
LEGACY_SAFETY_RULE_MARKERS = (
    "profit protect",
    "profit-protect",
    "scale out",
    "partial take profit",
    "partial profit",
    "reduce the position",
    "reduce exposure",
)
LEGACY_MEMORY_RULE_MARKERS = (
    "profit protect",
    "profit-protect",
    "scale out",
    "partial take profit",
    "partial profit",
    "reduce the position when",
    "reduce exposure when",
)


def _normalize_text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip()
        return text or default
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_normalize_text(item, default="") for item in value]
        parts = [part for part in parts if part]
        return ", ".join(parts) or default
    if isinstance(value, dict):
        try:
            return json.dumps(value, sort_keys=True, ensure_ascii=True)
        except TypeError:
            return str(value)
    return str(value)


def _clean_legacy_policy_config(
    config: Dict[str, Any] | None,
    *,
    domain: str,
) -> tuple[Dict[str, Any], List[str]]:
    clean_config = dict(config or {})
    keys_to_remove = (
        LEGACY_SAFETY_CONFIG_KEYS
        if domain == "safety_5m"
        else LEGACY_POSITION_CONFIG_KEYS
        if domain == "position_15m"
        else ()
    )
    removed_keys = [key for key in keys_to_remove if key in clean_config]
    for key in removed_keys:
        clean_config.pop(key, None)
    return clean_config, removed_keys


def _collect_legacy_policy_rule_markers(row: Dict[str, Any], *, domain: str) -> List[str]:
    markers = LEGACY_SAFETY_RULE_MARKERS if domain == "safety_5m" else LEGACY_POSITION_RULE_MARKERS
    policy_name = str((row or {}).get("policy_name") or "").strip().lower().replace("-", "_")
    rule_text = str((row or {}).get("rule_text") or "").strip().lower()
    matched: List[str] = []
    for marker in markers:
        if marker in rule_text and marker not in matched:
            matched.append(marker)
    token_markers = {
        "reduce": ("_reduce", "reduce_", "reduce"),
        "scale out": ("scale_out", "scaleout"),
        "profit protect": ("profit_protect",),
    }
    for label, tokens in token_markers.items():
        if label in matched:
            continue
        if any(token in policy_name for token in tokens):
            matched.append(label)
    return matched


def _memory_entry_uses_legacy_management_contract(row: Dict[str, Any]) -> List[str]:
    metrics = dict((row or {}).get("metrics") or {})
    blob = " ".join(
        part
        for part in [
            str((row or {}).get("rule") or "").strip().lower(),
            _normalize_text(metrics.get("candidate_lessons"), default="").lower(),
            _normalize_text(metrics.get("merged_rules"), default="").lower(),
        ]
        if part
    )
    return [marker for marker in LEGACY_MEMORY_RULE_MARKERS if marker in blob]


def _apply_contract_migration_metadata(
    metrics: Dict[str, Any] | None,
    *,
    migration_reason: str,
    removed_keys: List[str] | None = None,
    rule_markers: List[str] | None = None,
    now_text: str,
) -> Dict[str, Any]:
    updated = dict(metrics or {})
    updated["retired_legacy_config"] = True
    updated["retirement_reason"] = migration_reason
    updated["migration_reason"] = migration_reason
    updated["contract_cutover_date"] = CUTOVER_DATE
    updated["contract_migrated_at"] = now_text
    updated["contract_migrated_from"] = "pre_2026_03_10_reduce_contract"
    if removed_keys:
        updated["legacy_config_keys_removed"] = removed_keys
    if rule_markers:
        updated["legacy_rule_markers"] = rule_markers
    return updated


def _safe_json_loads(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _fetch_policy_registry(conn: sqlite3.Connection, *, domain: str) -> List[Dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT *
        FROM policy_registry
        WHERE domain = ?
        ORDER BY updated_at DESC, policy_key DESC
        """,
        (domain,),
    )
    rows: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
        item = dict(row)
        item["config"] = _safe_json_loads(item.get("config"), default={})
        item["metrics"] = _safe_json_loads(item.get("metrics"), default={})
        rows.append(item)
    return rows


def _upsert_policy_registry(conn: sqlite3.Connection, record: Dict[str, Any]) -> None:
    with conn:
        conn.execute(
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
                record.get("policy_key"),
                record.get("domain"),
                record.get("policy_type"),
                record.get("regime_key"),
                record.get("active_candidate_policy_id"),
                record.get("version", 1),
                json.dumps(record.get("config") or {}),
                json.dumps(record.get("metrics") or {}),
            ),
        )


def _migrate_policy_registry(conn: sqlite3.Connection, *, domain: str, migration_reason: str) -> int:
    migrated_count = 0
    now_text = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    for entry in _fetch_policy_registry(conn, domain=domain):
        config, removed_keys = _clean_legacy_policy_config(entry.get("config") or {}, domain=domain)
        if not removed_keys:
            continue
        metrics = _apply_contract_migration_metadata(
            entry.get("metrics") or {},
            migration_reason=migration_reason,
            removed_keys=removed_keys,
            now_text=now_text,
        )
        _upsert_policy_registry(
            conn,
            {
                "policy_key": entry.get("policy_key"),
                "domain": entry.get("domain"),
                "policy_type": entry.get("policy_type"),
                "regime_key": entry.get("regime_key"),
                "active_candidate_policy_id": entry.get("active_candidate_policy_id"),
                "version": int(entry.get("version") or 0) + 1,
                "config": config,
                "metrics": metrics,
            }
        )
        migrated_count += 1
    return migrated_count


def _fetch_candidate_policy_versions(conn: sqlite3.Connection, *, domain: str) -> List[Dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT *
        FROM candidate_policy_versions
        WHERE domain = ?
        ORDER BY created_at DESC, id DESC
        """,
        (domain,),
    )
    rows: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
        item = dict(row)
        item["config"] = _safe_json_loads(item.get("config"), default={})
        item["metrics"] = _safe_json_loads(item.get("metrics"), default={})
        rows.append(item)
    return rows


def _update_candidate_policy_version(
    conn: sqlite3.Connection,
    candidate_policy_id: int,
    *,
    status: str,
    config: Dict[str, Any],
    metrics: Dict[str, Any],
    retired_at: str,
) -> bool:
    try:
        with conn:
            conn.execute(
                """
                UPDATE candidate_policy_versions
                SET status = ?, config = ?, metrics = ?, retired_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    json.dumps(config),
                    json.dumps(metrics),
                    retired_at,
                    int(candidate_policy_id),
                ),
            )
        return True
    except sqlite3.Error:
        return False


def _retire_legacy_candidate_policy_versions(conn: sqlite3.Connection) -> int:
    retired_count = 0
    now_text = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    for domain in ("position_15m", "safety_5m"):
        for candidate in _fetch_candidate_policy_versions(conn, domain=domain):
            if str(candidate.get("status") or "").strip().lower() == "retired":
                continue
            clean_config, removed_keys = _clean_legacy_policy_config(candidate.get("config") or {}, domain=domain)
            rule_markers = _collect_legacy_policy_rule_markers(candidate, domain=domain)
            if not removed_keys and not rule_markers:
                continue
            metrics = _apply_contract_migration_metadata(
                candidate.get("metrics") or {},
                migration_reason="contract_cutover_20260310",
                removed_keys=removed_keys,
                rule_markers=rule_markers,
                now_text=now_text,
            )
            updated = _update_candidate_policy_version(
                conn,
                int(candidate.get("id") or 0),
                status="retired",
                config=clean_config,
                metrics=metrics,
                retired_at=now_text,
            )
            if updated:
                retired_count += 1
    return retired_count


def _fetch_long_term_memory_pool(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    cursor = conn.execute(
        """
        SELECT id, timestamp, rule, version, metrics
        FROM long_term_memory
        """
    )
    rows: List[Dict[str, Any]] = []
    for row in cursor.fetchall():
        item = dict(row)
        item["metrics"] = _safe_json_loads(item.get("metrics"), default={})
        rows.append(item)
    return rows


def _save_long_term_memory_entries(conn: sqlite3.Connection, entries: List[Dict[str, Any]]) -> None:
    if not entries:
        return
    with conn:
        for entry in entries:
            conn.execute(
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


def _retire_legacy_long_term_memory_entries(conn: sqlite3.Connection) -> int:
    rows = _fetch_long_term_memory_pool(conn)
    now = datetime.now(UTC)
    updates: List[Dict[str, Any]] = []
    for row in rows:
        metrics = dict(row.get("metrics") or {})
        if bool(metrics.get("retired")):
            continue
        rule_markers = _memory_entry_uses_legacy_management_contract(row)
        if not rule_markers:
            continue
        metrics["lifecycle_status"] = "retired"
        metrics["retired"] = True
        metrics["retirement_reason"] = "contract_cutover_20260310"
        metrics["migration_reason"] = "contract_cutover_20260310"
        metrics["legacy_rule_markers"] = rule_markers
        metrics["contract_cutover_date"] = CUTOVER_DATE
        metrics["contract_migrated_from"] = "pre_2026_03_10_reduce_contract"
        metrics["last_validated_at"] = now.isoformat()
        updates.append(
            {
                "id": row.get("id"),
                "rule": row.get("rule"),
                "version": max(int(row.get("version") or 1) + 1, 2),
                "metrics": metrics,
            }
        )
    if updates:
        _save_long_term_memory_entries(conn, updates)
    return len(updates)


def _backup_db(db_path: str) -> str | None:
    source_path = Path(db_path)
    if not source_path.exists() or not source_path.is_file():
        return None
    backup_path = source_path.with_name(f"{source_path.stem}.pre_contract_20260310{source_path.suffix}.bak")
    shutil.copy2(source_path, backup_path)
    return str(backup_path)


def _upsert_learning_artifact(conn: sqlite3.Connection, artifact_key: str, payload: Dict[str, Any]) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO learning_artifacts (
                artifact_key,
                artifact_type,
                payload,
                updated_at
            ) VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(artifact_key) DO UPDATE SET
                artifact_type = excluded.artifact_type,
                payload = excluded.payload,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                artifact_key,
                "migration",
                json.dumps(payload or {}),
            ),
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Back up and migrate learning surfaces to the 2026-03-10 management contract.",
    )
    parser.add_argument(
        "--db-path",
        default="data/gpt_trader.db",
        help="SQLite database path.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip creating a DB backup before migration.",
    )
    args = parser.parse_args()

    backup_path = None if args.no_backup else _backup_db(args.db_path)
    conn = sqlite3.connect(args.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        summary = {
            "cutover_date": CUTOVER_DATE,
            "cutover_datetime": CUTOVER_DATETIME,
            "backup_path": backup_path,
            "position_registry_migrated_count": _migrate_policy_registry(
                conn,
                domain="position_15m",
                migration_reason="reduce_removed",
            ),
            "safety_registry_migrated_count": _migrate_policy_registry(
                conn,
                domain="safety_5m",
                migration_reason="profit_protect_removed",
            ),
            "candidate_policy_retired_count": _retire_legacy_candidate_policy_versions(conn),
            "long_term_memory_retired_count": _retire_legacy_long_term_memory_entries(conn),
        }
        _upsert_learning_artifact(conn, "policy_contract_cutover_20260310", summary)
        print(json.dumps(summary, indent=2, ensure_ascii=True, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
