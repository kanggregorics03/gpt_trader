import hashlib
from datetime import UTC, datetime


class TradingTradeCaseLifecycleMixin:
    def _make_trade_case_id(self, symbol: str, audit_log_id: int, observed_at: str) -> str:
        case_seed = f"{symbol}|{audit_log_id}|{observed_at}"
        digest = hashlib.sha1(case_seed.encode("utf-8")).hexdigest()[:10]
        compact_ts = (
            str(observed_at or "")
            .replace("-", "")
            .replace(":", "")
            .replace(" ", "T")
        )
        return f"{symbol}-{compact_ts}-{digest}"

    def _calculate_holding_minutes(self, opened_at: str | None, closed_at: str | None) -> float | None:
        if not opened_at or not closed_at:
            return None
        try:
            opened_dt = datetime.strptime(opened_at, "%Y-%m-%d %H:%M:%S")
            closed_dt = datetime.strptime(closed_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        elapsed_seconds = max(0.0, (closed_dt - opened_dt).total_seconds())
        return round(elapsed_seconds / 60.0, 2)

    def _get_positions_by_symbol(self, account_state) -> dict[str, object]:
        if account_state is None:
            return {}
        return {
            position.symbol: position
            for position in getattr(account_state, "positions", []) or []
            if getattr(position, "symbol", None)
        }

    def _get_symbol_observed_pnl_pct(self, symbol: str, before_state, after_state) -> float | None:
        positions_after = self._get_positions_by_symbol(after_state)
        positions_before = self._get_positions_by_symbol(before_state)
        after_position = positions_after.get(symbol)
        before_position = positions_before.get(symbol)
        for candidate in (after_position, before_position):
            if candidate is None:
                continue
            try:
                return float(getattr(candidate, "unrealized_pnl_pct", None))
            except (TypeError, ValueError):
                continue
        return None

    def _get_symbol_entry_price(self, symbol: str, before_state, after_state) -> float | None:
        positions_after = self._get_positions_by_symbol(after_state)
        positions_before = self._get_positions_by_symbol(before_state)
        for candidate in (positions_after.get(symbol), positions_before.get(symbol)):
            if candidate is None:
                continue
            try:
                entry_price = float(getattr(candidate, "entry_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if entry_price > 0:
                return entry_price
        return None

    def _get_symbol_entry_side(self, symbol: str, before_state, after_state, symbol_orders: list) -> str | None:
        positions_after = self._get_positions_by_symbol(after_state)
        positions_before = self._get_positions_by_symbol(before_state)
        for candidate in (positions_after.get(symbol), positions_before.get(symbol)):
            side = str(getattr(candidate, "side", "") or "").upper()
            if side in ("LONG", "SHORT"):
                return side
        for order in symbol_orders:
            action = str(getattr(order, "action", "") or "").upper()
            if action == "BUY":
                return "LONG"
            if action == "SELL":
                return "SHORT"
        return None

    def _normalize_case_timestamp(self, value) -> str | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 1_000_000_000_000:
                timestamp /= 1000.0
            try:
                return datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
            except (OverflowError, OSError, ValueError):
                return None
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return self._normalize_case_timestamp(int(text))
        candidate = text.replace("T", " ").replace("Z", "")
        if "." in candidate:
            candidate = candidate.split(".", 1)[0]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(candidate, fmt)
                return parsed.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC).replace(tzinfo=None)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def _get_symbol_fill_boundary_timestamp(
        self,
        audit_log_id: int | None,
        symbol: str,
        boundary: str,
    ) -> str | None:
        if not audit_log_id or not symbol or not hasattr(self.state_store, "get_execution_fills"):
            return None
        fills = self.state_store.get_execution_fills(audit_log_id)
        normalized = []
        for fill in fills or []:
            if str(fill.get("symbol") or "") != symbol:
                continue
            fill_timestamp = self._normalize_case_timestamp(fill.get("fill_timestamp"))
            if fill_timestamp:
                normalized.append(fill_timestamp)
        if not normalized:
            return None
        return min(normalized) if boundary == "first" else max(normalized)

    def _resolve_trade_case_snapshot_window(
        self,
        symbol: str,
        trade_case: dict | None,
        entry_audit_log_id: int | None = None,
        exit_audit_log_id: int | None = None,
        fallback_opened_at: str | None = None,
        fallback_closed_at: str | None = None,
    ) -> tuple[str | None, str | None]:
        trade_case = dict(trade_case or {})
        start_timestamp = self._get_symbol_fill_boundary_timestamp(
            entry_audit_log_id or trade_case.get("entry_audit_log_id"),
            symbol,
            boundary="first",
        )
        end_timestamp = self._get_symbol_fill_boundary_timestamp(
            exit_audit_log_id or trade_case.get("exit_audit_log_id"),
            symbol,
            boundary="last",
        )
        start_timestamp = start_timestamp or self._normalize_case_timestamp(
            fallback_opened_at or trade_case.get("opened_at")
        )
        end_timestamp = end_timestamp or self._normalize_case_timestamp(
            fallback_closed_at or trade_case.get("closed_at")
        )
        if start_timestamp and end_timestamp and start_timestamp > end_timestamp:
            end_timestamp = None
        return start_timestamp, end_timestamp

    def _recompute_trade_case_excursions(
        self,
        trade_case_id: str | None,
        symbol: str | None = None,
        trade_case: dict | None = None,
        entry_audit_log_id: int | None = None,
        exit_audit_log_id: int | None = None,
        fallback_opened_at: str | None = None,
        fallback_closed_at: str | None = None,
        current_observed_pnl_pct: float | None = None,
    ) -> tuple[float | None, float | None]:
        if not trade_case_id or not hasattr(self.state_store, "get_trade_case_snapshots"):
            return current_observed_pnl_pct, current_observed_pnl_pct
        snapshots = []
        start_timestamp = None
        end_timestamp = None
        if symbol:
            start_timestamp, end_timestamp = self._resolve_trade_case_snapshot_window(
                symbol=symbol,
                trade_case=trade_case,
                entry_audit_log_id=entry_audit_log_id,
                exit_audit_log_id=exit_audit_log_id,
                fallback_opened_at=fallback_opened_at,
                fallback_closed_at=fallback_closed_at,
            )
        window_applied = bool(start_timestamp or end_timestamp)
        if hasattr(self.state_store, "get_trade_case_snapshots_between") and window_applied:
            snapshots = self.state_store.get_trade_case_snapshots_between(
                trade_case_id,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
        if not snapshots and not window_applied:
            snapshots = self.state_store.get_trade_case_snapshots(trade_case_id)
        pnl_path = []
        for snapshot in snapshots:
            try:
                if snapshot.get("unrealized_pnl_pct") is not None:
                    pnl_path.append(float(snapshot["unrealized_pnl_pct"]))
            except (TypeError, ValueError):
                continue
        if current_observed_pnl_pct is not None:
            pnl_path.append(float(current_observed_pnl_pct))
        if not pnl_path:
            return None, None
        return max(pnl_path), min(pnl_path)

    def _build_trade_case_feature_vector(
        self,
        symbol: str,
        market_snapshots,
        account_state,
        risk_state,
    ) -> list[float]:
        if symbol not in (market_snapshots or {}):
            return []
        vectors = self._build_current_market_vectors(
            {symbol: market_snapshots.get(symbol)},
            account_state=account_state,
            risk_state=risk_state,
        )
        return list(vectors.get(symbol) or [])
