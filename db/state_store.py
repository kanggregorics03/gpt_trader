import json
import sqlite3
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

    def _init_db(self):
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    account_state JSON,
                    market_state JSON,
                    intent JSON,
                    risk_approved BOOLEAN,
                    risk_reason TEXT,
                    receipts JSON,
                    policy_snapshot TEXT,
                    decision_request JSON,
                    ai_raw_response TEXT,
                    execution_summary JSON
                );
                
                CREATE TABLE IF NOT EXISTS journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    decision TEXT,
                    rationale TEXT,
                    confidence REAL,
                    risk_reason TEXT,
                    estimated_pnl_pct REAL,
                    loss_signal BOOLEAN DEFAULT 0
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

                CREATE TABLE IF NOT EXISTS replay_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    audit_log_id INTEGER NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    original_decision TEXT,
                    replayed_decision TEXT,
                    decision_changed BOOLEAN DEFAULT 0,
                    order_count_changed BOOLEAN DEFAULT 0,
                    confidence_delta REAL DEFAULT 0,
                    payload JSON
                );
            """)
        self._ensure_column("audit_logs", "policy_snapshot", "TEXT")
        self._ensure_column("audit_logs", "decision_request", "JSON")
        self._ensure_column("audit_logs", "ai_raw_response", "TEXT")
        self._ensure_column("audit_logs", "execution_summary", "JSON")
        self._ensure_column("journal", "risk_reason", "TEXT")
        self._ensure_column("journal", "estimated_pnl_pct", "REAL")
        self._ensure_column("journal", "loss_signal", "BOOLEAN DEFAULT 0")

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
        decision_request: Optional[dict] = None,
        ai_raw_response: Optional[str] = None,
        execution_summary: Optional[dict] = None,
    ) -> Optional[int]:
        try:
            with self.conn:
                cursor = self.conn.execute(
                    """
                    INSERT INTO audit_logs (
                        account_state,
                        market_state,
                        intent,
                        risk_approved,
                        risk_reason,
                        receipts,
                        policy_snapshot,
                        decision_request,
                        ai_raw_response,
                        execution_summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        json.dumps(account),
                        json.dumps(market),
                        json.dumps(intent),
                        risk_ok,
                        reason,
                        json.dumps(receipts),
                        policy_snapshot,
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
        risk_reason: str = "",
        estimated_pnl_pct: Optional[float] = None,
        loss_signal: bool = False,
    ):
        try:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO journal (
                        decision,
                        rationale,
                        confidence,
                        risk_reason,
                        estimated_pnl_pct,
                        loss_signal
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        decision,
                        "\\n".join(rationale),
                        confidence,
                        risk_reason,
                        estimated_pnl_pct,
                        int(loss_signal),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to save journal log: {e}")

    def get_recent_journals(self, limit: int = 10) -> List[str]:
        try:
            cursor = self.conn.execute("SELECT decision, rationale FROM journal ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            return [f"{row['decision']}: {row['rationale']}" for row in rows]
        except Exception as e:
            logger.error(f"Failed to fetch journals: {e}")
            return []

    def get_long_term_memory(self) -> List[str]:
        try:
            cursor = self.conn.execute("SELECT rule FROM long_term_memory ORDER BY id DESC LIMIT 50")
            rows = cursor.fetchall()
            rules = [row['rule'] for row in rows]
            # Default empty lesson if none exists
            if not rules:
                return ["No long term lessons recorded yet. Be conservative."]
            return rules
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
                id,
                timestamp,
                decision,
                rationale,
                confidence,
                risk_reason,
                estimated_pnl_pct,
                loss_signal
            FROM journal
            WHERE timestamp >= ?
            ORDER BY id ASC
        """
        params: List[Any] = [since.strftime("%Y-%m-%d %H:%M:%S")]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def save_long_term_memory_entries(self, entries: List[Dict[str, Any]]):
        if not entries:
            return
        try:
            with self.conn:
                self.conn.executemany(
                    """
                    INSERT INTO long_term_memory (rule, version, metrics)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (
                            entry["rule"],
                            int(entry.get("version", 1)),
                            json.dumps(entry.get("metrics", {})),
                        )
                        for entry in entries
                    ],
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
                        confidence_delta,
                        payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        audit_log_id,
                        comparison.get("original_decision"),
                        comparison.get("replayed_decision"),
                        int(bool(comparison.get("decision_changed"))),
                        int(bool(comparison.get("order_count_changed"))),
                        float(comparison.get("confidence_delta") or 0.0),
                        json.dumps(payload),
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to save replay run: {e}")

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
            item = dict(row)
            try:
                item["metrics"] = json.loads(item["metrics"]) if item["metrics"] else {}
            except Exception:
                item["metrics"] = {}
            rows.append(item)
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

        snapshot = {
            "total_audits_24h": int(stats.get("total_audits") or 0),
            "rejected_24h": int(stats.get("rejected_audits") or 0),
            "loop_errors_24h": int(stats.get("loop_errors") or 0),
            "timeouts_unresolved_24h": int((unresolved_timeouts or {})["count"] or 0),
            "fee_cost_24h": fill_stats["fee_cost"],
            "fill_count_24h": fill_stats["fill_count"],
            "realized_pnl_24h": fill_stats["realized_pnl"],
            "ledger_net_pnl_24h": self.get_position_ledger_stats(hours=24, now=now)["net_realized_pnl"],
            "latest_audit": dict(latest_audit_row) if latest_audit_row else None,
            "latest_memory": self._decode_metrics_row(latest_memory_row),
            "recent_logs": self.get_recent_audits(limit=20),
            "recent_memory": self.get_recent_long_term_memory(limit=5),
            "recent_fills": self.get_recent_fills(limit=10),
            "recent_ledger": self.get_recent_position_ledger(limit=10),
            "recent_replays": self.get_recent_replay_runs(limit=10),
        }
        return snapshot

    def get_recent_audits(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT id, timestamp, intent, risk_approved, risk_reason, execution_summary
            FROM audit_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

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
        return decoded

    def get_replay_candidates(self, limit: int = 20) -> List[Dict[str, Any]]:
        cursor = self.conn.execute(
            """
            SELECT id, timestamp, intent, risk_approved, risk_reason
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
                confidence_delta,
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
                confidence_delta,
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
            item["payload"] = self._safe_json_loads(item.get("payload"), default={})
            rows.append(item)
        return rows

    def _decode_metrics_row(self, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        item = dict(row)
        try:
            item["metrics"] = json.loads(item["metrics"]) if item.get("metrics") else {}
        except Exception:
            item["metrics"] = {}
        return item

    def get_risk_state(
        self,
        current_balance: float,
        cooldown_minutes: int,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        now = now or self._utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        daily_start_balance = self._get_daily_start_balance(day_start) or current_balance
        daily_pnl_pct = 0.0
        if daily_start_balance > 0:
            daily_pnl_pct = ((current_balance - daily_start_balance) / daily_start_balance) * 100

        recent_losses = self._get_recent_consecutive_losses()
        cooldown_until = self._get_last_loss_timestamp()
        cooldown_active = False
        cooldown_remaining_minutes = 0
        if cooldown_until is not None:
            cooldown_until = cooldown_until + timedelta(minutes=cooldown_minutes)
            if cooldown_until > now:
                cooldown_active = True
                cooldown_remaining_minutes = max(
                    1,
                    int((cooldown_until - now).total_seconds() // 60),
                )

        return {
            "daily_start_balance": daily_start_balance,
            "daily_pnl_pct": daily_pnl_pct,
            "consecutive_losses": recent_losses,
            "cooldown_active": cooldown_active,
            "cooldown_remaining_minutes": cooldown_remaining_minutes,
        }

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

    def _get_last_loss_timestamp(self) -> Optional[datetime]:
        cursor = self.conn.execute(
            """
            SELECT timestamp
            FROM journal
            WHERE loss_signal = 1
            ORDER BY id DESC
            LIMIT 1
            """
        )
        row = cursor.fetchone()
        if not row or not row["timestamp"]:
            return None
        try:
            return datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def _utcnow(self) -> datetime:
        return datetime.now(UTC).replace(tzinfo=None)

    def _decode_audit_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        decoded = dict(row)
        defaults = {
            "account_state": {},
            "market_state": {},
            "intent": {},
            "receipts": [],
            "decision_request": {},
            "execution_summary": {},
        }
        for key, default in defaults.items():
            decoded[key] = self._safe_json_loads(decoded.get(key), default=default)
        return decoded

    def _safe_json_loads(self, value: Any, default: Any):
        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except Exception:
            return default
