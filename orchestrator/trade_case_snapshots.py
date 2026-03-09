import time
from datetime import datetime


class TradingTradeCaseSnapshotMixin:
    def _build_trade_case_snapshot_entries(
        self,
        audit_log_id: int | None,
        observed_at: str,
        account_state,
        market_snapshots,
        symbol_journal_entries: list[dict],
    ) -> list[dict]:
        fetch_active_cases = getattr(self.state_store, "get_all_active_trade_cases", None)
        active_cases = fetch_active_cases() if callable(fetch_active_cases) else []
        active_by_symbol = {
            str((row or {}).get("symbol") or ""): (row or {})
            for row in (active_cases or [])
            if (row or {}).get("symbol") and (row or {}).get("trade_case_id")
        }
        entries_by_symbol = {
            str((row or {}).get("symbol") or ""): (row or {})
            for row in (symbol_journal_entries or [])
            if (row or {}).get("symbol") and (row or {}).get("trade_case_id")
        }
        positions_by_symbol = self._get_positions_by_symbol(account_state)
        snapshot_entries = []
        for symbol in sorted(set(active_by_symbol.keys()) | set(entries_by_symbol.keys())):
            case_entry = entries_by_symbol.get(symbol) or {}
            active_case = active_by_symbol.get(symbol) or {}
            trade_case_id = case_entry.get("trade_case_id") or active_case.get("trade_case_id")
            if not trade_case_id:
                continue
            snapshot = market_snapshots.get(symbol) if isinstance(market_snapshots, dict) else None
            position = positions_by_symbol.get(symbol)
            order_book = getattr(snapshot, "order_book", None) if snapshot is not None else None
            unrealized_pnl_pct = case_entry.get("observed_pnl_pct")
            if unrealized_pnl_pct is None and position is not None:
                unrealized_pnl_pct = getattr(position, "unrealized_pnl_pct", None)
            snapshot_entries.append(
                {
                    "trade_case_id": trade_case_id,
                    "timestamp": observed_at,
                    "audit_log_id": audit_log_id,
                    "symbol": symbol,
                    "current_price": getattr(snapshot, "current_price", None) if snapshot is not None else None,
                    "mark_price": getattr(snapshot, "mark_price", None) if snapshot is not None else None,
                    "mid_price": getattr(order_book, "mid_price", None) if order_book is not None else None,
                    "spread_bps": getattr(order_book, "spread_bps", None) if order_book is not None else None,
                    "volume_ratio_15m": getattr(snapshot, "volume_ratio_15m", None) if snapshot is not None else None,
                    "unrealized_pnl_pct": unrealized_pnl_pct,
                    "payload": {
                        "case_status": case_entry.get("case_status") or "active",
                        "holding_minutes": case_entry.get("holding_minutes"),
                        "opportunity_trigger_reasons": (
                            getattr(snapshot, "opportunity_trigger_reasons", []) if snapshot is not None else []
                        ),
                        "risk_trigger_reasons": (
                            getattr(snapshot, "risk_trigger_reasons", []) if snapshot is not None else []
                        ),
                    },
                }
            )
        return snapshot_entries

    def _read_live_account_state_for_case_sampling(self):
        private_feed = getattr(self.account_reader, "private_feed", None)
        if private_feed is not None:
            try:
                ws_state = private_feed.get_account_state(self.symbols)
                if ws_state is not None:
                    return ws_state
            except Exception:
                pass
        try:
            return self.account_reader.get_account_state(self.symbols, force_refresh=False)
        except Exception:
            return None

    def _capture_active_trade_case_snapshots(self, now_epoch: float | None = None) -> int:
        fetch_active_cases = getattr(self.state_store, "get_all_active_trade_cases", None)
        if not callable(fetch_active_cases):
            return 0
        active_cases = fetch_active_cases()
        if not active_cases:
            return 0
        account_state = self._read_live_account_state_for_case_sampling()
        if account_state is None:
            return 0
        positions_by_symbol = self._get_positions_by_symbol(account_state)
        observed_at = datetime.fromtimestamp(now_epoch or time.time()).strftime("%Y-%m-%d %H:%M:%S")
        snapshot_entries = []
        for active_case in active_cases:
            symbol = str(active_case.get("symbol") or "").strip()
            trade_case_id = str(active_case.get("trade_case_id") or "").strip()
            if not symbol or not trade_case_id:
                continue
            ticker = None
            order_book = None
            if getattr(self, "public_feed", None) is not None:
                try:
                    ticker = self.public_feed.get_ticker(symbol)
                except Exception:
                    ticker = None
                try:
                    order_book = self.public_feed.get_order_book(symbol, limit=10)
                except Exception:
                    order_book = None
            position = positions_by_symbol.get(symbol)
            best_bid = None
            best_ask = None
            if isinstance(order_book, dict):
                bids = order_book.get("bids") or []
                asks = order_book.get("asks") or []
                try:
                    best_bid = float(bids[0][0]) if bids else None
                except (TypeError, ValueError, IndexError):
                    best_bid = None
                try:
                    best_ask = float(asks[0][0]) if asks else None
                except (TypeError, ValueError, IndexError):
                    best_ask = None
            mid_price = ((best_bid + best_ask) / 2.0) if best_bid is not None and best_ask is not None else None
            spread_bps = (
                ((best_ask - best_bid) / mid_price) * 10000.0
                if mid_price not in (None, 0.0) and best_bid is not None and best_ask is not None
                else None
            )
            current_price = None
            mark_price = None
            if isinstance(ticker, dict):
                for candidate in (ticker.get("last"), ticker.get("close"), ticker.get("markPrice"), ticker.get("mark")):
                    try:
                        value = float(candidate)
                    except (TypeError, ValueError):
                        value = 0.0
                    if value > 0:
                        current_price = value
                        break
                for candidate in (ticker.get("markPrice"), ticker.get("mark"), getattr(position, "mark_price", None), current_price):
                    try:
                        value = float(candidate)
                    except (TypeError, ValueError):
                        value = 0.0
                    if value > 0:
                        mark_price = value
                        break
            if mark_price is None and position is not None:
                mark_price = getattr(position, "mark_price", None)
            if current_price is None and mark_price is not None:
                current_price = mark_price
            snapshot_entries.append(
                {
                    "trade_case_id": trade_case_id,
                    "timestamp": observed_at,
                    "audit_log_id": active_case.get("last_audit_log_id"),
                    "symbol": symbol,
                    "current_price": current_price,
                    "mark_price": mark_price,
                    "mid_price": mid_price,
                    "spread_bps": round(spread_bps, 6) if spread_bps is not None else None,
                    "volume_ratio_15m": None,
                    "unrealized_pnl_pct": getattr(position, "unrealized_pnl_pct", None) if position is not None else None,
                    "payload": {
                        "sampling_source": "poll_sampler",
                        "entry_decision": active_case.get("entry_decision"),
                        "last_decision": active_case.get("last_decision"),
                    },
                }
            )
        self.state_store.save_trade_case_snapshots(snapshot_entries)
        return len(snapshot_entries)

    def _sample_trade_case_paths_if_due(self, now_epoch: float) -> int:
        interval_seconds = float(self.adaptive_loop_config.get("trade_case_snapshot_poll_seconds", 5) or 5)
        if interval_seconds <= 0:
            return 0
        if self.last_trade_case_snapshot_at and (now_epoch - self.last_trade_case_snapshot_at) < interval_seconds:
            return 0
        captured = self._capture_active_trade_case_snapshots(now_epoch=now_epoch)
        if captured > 0:
            self.last_trade_case_snapshot_at = now_epoch
        return captured
