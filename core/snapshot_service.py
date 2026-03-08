import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Dict

from core.models import AccountState, MarketSnapshot, RiskStateSnapshot, coerce_risk_state_snapshot


@dataclass
class SnapshotBundle:
    account_state: AccountState
    risk_state: RiskStateSnapshot
    policy_summary: str
    market_snapshots: Dict[str, MarketSnapshot]
    market_prices: Dict[str, float]
    is_trigger_mode: bool
    risk_mode: str
    risk_profile: Dict[str, Any]
    materialized_at_epoch: float
    refreshed_timeframes: tuple[str, ...] = field(default_factory=tuple)


class SnapshotService:
    DEFAULT_TIMEFRAME_LIMITS = {"5m": 80, "15m": 80, "1h": 80, "4h": 80}
    TIMEFRAME_MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
    STRONG_TRIGGER_REASONS = {"volume_surge_15m", "range_break_1h"}

    def __init__(
        self,
        *,
        state_store,
        account_reader,
        market_collector,
        risk_engine,
        symbols,
        materialization_minutes: int = 5,
    ):
        self.state_store = state_store
        self.account_reader = account_reader
        self.market_collector = market_collector
        self.risk_engine = risk_engine
        configured_symbols = [str(symbol).strip().upper() for symbol in (symbols or []) if str(symbol).strip()]
        primary_symbol = None
        if hasattr(risk_engine, "get_trading_symbol"):
            primary_symbol = str(risk_engine.get_trading_symbol() or "").strip().upper()
        if primary_symbol:
            self.symbols = [primary_symbol]
        elif configured_symbols:
            self.symbols = [configured_symbols[0]]
        else:
            self.symbols = ["BTCUSDT"]
        self.materialization_minutes = max(1, int(materialization_minutes or 5))
        self._market_snapshot_cache: Dict[str, MarketSnapshot] = {}
        self._last_timeframe_buckets: Dict[str, int] = {}
        self._last_materialization_bucket: int | None = None
        self._materialized_bundle: SnapshotBundle | None = None

    def _bucket(self, now_epoch: float, minutes: int) -> int:
        interval_seconds = max(1, int(minutes)) * 60
        return int(now_epoch // interval_seconds)

    def next_materialization_epoch(self, now_epoch: float | None = None) -> float:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        interval_seconds = self.materialization_minutes * 60
        return (self._bucket(now_epoch, self.materialization_minutes) + 1) * interval_seconds

    def _resolve_timeframes_to_refresh(self, now_epoch: float, force: bool) -> Dict[str, int]:
        if force or not self._market_snapshot_cache:
            return dict(self.DEFAULT_TIMEFRAME_LIMITS)

        refresh = {"5m": self.DEFAULT_TIMEFRAME_LIMITS["5m"]}
        for timeframe, minutes in self.TIMEFRAME_MINUTES.items():
            if timeframe == "5m":
                continue
            current_bucket = self._bucket(now_epoch, minutes)
            if self._last_timeframe_buckets.get(timeframe) != current_bucket:
                refresh[timeframe] = self.DEFAULT_TIMEFRAME_LIMITS[timeframe]
        return refresh

    def _build_risk_state(self, account_state: AccountState) -> RiskStateSnapshot:
        risk_limits = self.risk_engine.policy.get("risk_limits", {})
        cooldown_minutes = risk_limits.get("cooldown_minutes", 0)
        cooldown_config = risk_limits.get("dynamic_cooldown", {})
        drawdown_activate_pct = risk_limits.get("drawdown_activate_pct")
        if drawdown_activate_pct is None:
            daily_max_loss_pct = float(risk_limits.get("daily_max_loss_pct", 0.0) or 0.0)
            drawdown_activate_pct = round(daily_max_loss_pct * 0.5, 4) if daily_max_loss_pct > 0 else None
        return coerce_risk_state_snapshot(
            self.state_store.get_risk_state(
                current_balance=account_state.total_balance,
                cooldown_minutes=cooldown_minutes,
                cooldown_config=cooldown_config,
                drawdown_activate_pct=drawdown_activate_pct,
            )
        )

    def _should_enable_trigger_mode(self, market_snapshots: Dict[str, MarketSnapshot]) -> bool:
        for snapshot in (market_snapshots or {}).values():
            reasons = list(snapshot.opportunity_trigger_reasons or [])
            if len(reasons) < 2:
                continue
            if any(reason in self.STRONG_TRIGGER_REASONS for reason in reasons):
                return True
        return False

    def _build_market_snapshot_history_entries(
        self,
        market_snapshots: Dict[str, MarketSnapshot],
        *,
        now_epoch: float,
    ) -> list[Dict[str, Any]]:
        timestamp = datetime.fromtimestamp(now_epoch, UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        entries: list[Dict[str, Any]] = []
        for symbol, snapshot in (market_snapshots or {}).items():
            order_book = getattr(snapshot, "order_book", None)
            entries.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "current_price": getattr(snapshot, "current_price", None),
                    "mark_price": getattr(snapshot, "mark_price", None),
                    "mid_price": getattr(order_book, "mid_price", None) if order_book is not None else None,
                    "spread_bps": getattr(order_book, "spread_bps", None) if order_book is not None else None,
                    "spread_percentile_1h": getattr(snapshot, "spread_percentile_1h", None),
                    "depth_percentile_1h": getattr(snapshot, "depth_percentile_1h", None),
                    "depth_notional_top5": getattr(snapshot, "depth_notional_top5", None),
                    "spread_vs_1h_avg": getattr(snapshot, "spread_vs_1h_avg", None),
                    "volume_ratio_15m": getattr(snapshot, "volume_ratio_15m", None),
                    "distance_to_invalid_level": getattr(snapshot, "distance_to_invalid_level", None),
                    "trigger_active": bool(getattr(snapshot, "is_opportunity_trigger", False)),
                    "risk_trigger_active": bool(getattr(snapshot, "is_risk_trigger", False)),
                    "volume_surge_trigger_active": bool(getattr(snapshot, "volume_surge_trigger_active", False)),
                    "fast_move_trigger_active": bool(getattr(snapshot, "fast_move_trigger_active", False)),
                    "range_break_trigger_active": bool(getattr(snapshot, "range_break_trigger_active", False)),
                    "trend_1h": getattr(snapshot, "trend_1h", None),
                    "payload": snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else {},
                }
            )
        return entries

    def materialize(self, now_epoch: float | None = None, force: bool = False) -> SnapshotBundle:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        current_bucket = self._bucket(now_epoch, self.materialization_minutes)
        if (
            not force
            and self._materialized_bundle is not None
            and self._last_materialization_bucket == current_bucket
        ):
            return self._materialized_bundle

        refreshed_timeframes = self._resolve_timeframes_to_refresh(now_epoch, force=force)
        account_state = self.account_reader.get_account_state(self.symbols)
        risk_state = self._build_risk_state(account_state)

        market_snapshots = {}
        market_prices = {}
        for symbol in self.symbols:
            trigger_state = self.state_store.get_symbol_trigger_state(symbol)
            snapshot = self.market_collector.get_market_snapshot(
                symbol,
                trigger_state=trigger_state,
                timeframes_to_fetch=refreshed_timeframes,
                existing_snapshot=self._market_snapshot_cache.get(symbol),
            )
            self.state_store.save_symbol_trigger_state(
                symbol,
                self.market_collector.build_next_trigger_state(snapshot),
            )
            self._market_snapshot_cache[symbol] = snapshot
            market_snapshots[symbol] = snapshot
            market_prices[symbol] = snapshot.current_price

        for timeframe in refreshed_timeframes:
            self._last_timeframe_buckets[timeframe] = self._bucket(
                now_epoch,
                self.TIMEFRAME_MINUTES[timeframe],
            )
        if hasattr(self.state_store, "save_market_snapshot_history"):
            self.state_store.save_market_snapshot_history(
                self._build_market_snapshot_history_entries(
                    market_snapshots,
                    now_epoch=now_epoch,
                )
            )

        is_trigger_mode = self._should_enable_trigger_mode(market_snapshots)
        risk_mode = self.risk_engine.get_active_risk_mode(
            risk_state=risk_state,
            is_trigger_mode=is_trigger_mode,
        )
        risk_profile = self.risk_engine.get_active_risk_profile(
            risk_state=risk_state,
            is_trigger_mode=is_trigger_mode,
        )
        bundle = SnapshotBundle(
            account_state=account_state,
            risk_state=risk_state,
            policy_summary=self.risk_engine.get_summary(),
            market_snapshots=market_snapshots,
            market_prices=market_prices,
            is_trigger_mode=is_trigger_mode,
            risk_mode=risk_mode,
            risk_profile=risk_profile,
            materialized_at_epoch=now_epoch,
            refreshed_timeframes=tuple(sorted(refreshed_timeframes.keys())),
        )
        self._materialized_bundle = bundle
        self._last_materialization_bucket = current_bucket
        return bundle

    def build_cycle_snapshot(self) -> SnapshotBundle:
        return self.materialize(force=False)
