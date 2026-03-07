import time
from dataclasses import dataclass, field
from typing import Any, Dict

from core.models import AccountState, MarketSnapshot


@dataclass
class SnapshotBundle:
    account_state: AccountState
    risk_state: Dict[str, Any]
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
        self.symbols = list(symbols or [])
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

    def _build_risk_state(self, account_state: AccountState) -> Dict[str, Any]:
        risk_limits = self.risk_engine.policy.get("risk_limits", {})
        cooldown_minutes = risk_limits.get("cooldown_minutes", 0)
        drawdown_activate_pct = risk_limits.get("drawdown_activate_pct")
        if drawdown_activate_pct is None:
            daily_max_loss_pct = float(risk_limits.get("daily_max_loss_pct", 0.0) or 0.0)
            drawdown_activate_pct = round(daily_max_loss_pct * 0.5, 4) if daily_max_loss_pct > 0 else None
        return self.state_store.get_risk_state(
            current_balance=account_state.total_balance,
            cooldown_minutes=cooldown_minutes,
            drawdown_activate_pct=drawdown_activate_pct,
        )

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

        is_trigger_mode = any(
            snapshot.is_opportunity_trigger for snapshot in market_snapshots.values()
        )
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
