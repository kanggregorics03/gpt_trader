import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


class StateStoreRiskCalculationMixin:
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

        recent_losses = self._get_recent_consecutive_losses(day_start=day_start)
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

    def _get_recent_consecutive_losses(self, day_start: Optional[datetime] = None) -> int:
        query = """
            SELECT loss_signal
            FROM journal
            WHERE estimated_pnl_pct IS NOT NULL
        """
        params: list[Any] = []
        if day_start is not None:
            query += "\n            AND timestamp >= ?"
            params.append(day_start.strftime("%Y-%m-%d %H:%M:%S"))
        query += """
            ORDER BY id DESC
            LIMIT 20
            """
        cursor = self.conn.execute(
            query,
            tuple(params),
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
            int(
                config.get(
                    "max_minutes",
                    max(base_cooldown, config["base_minutes"]),
                )
                or max(base_cooldown, config["base_minutes"])
            ),
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
