from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


class AlphaPolicyEngine:
    def __init__(self, policy: Optional[Dict[str, Any]] = None, state_store=None):
        self.policy = dict((policy or {}).get("alpha_policy") or {})
        self.state_store = state_store

    def get_active_policy(self, regime_key: Optional[str] = None) -> Dict[str, Any]:
        if self.state_store is None or not hasattr(self.state_store, "get_policy_registry_entry"):
            return {"config": dict(self.policy)} if self.policy else {}
        registry = {}
        scoped_fetch = getattr(self.state_store, "get_policy_registry_entry_for_regime", None)
        if callable(scoped_fetch):
            registry = scoped_fetch("alpha_1h", regime_key=regime_key)
        if not registry:
            registry = self.state_store.get_policy_registry_entry("champion:alpha_1h")
        if registry:
            return registry
        return {"config": dict(self.policy)} if self.policy else {}

    def filter_market_snapshots(
        self,
        market_snapshots: Dict[str, Any],
        policy_override: Optional[Dict[str, Any]] = None,
        regime_key: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], Dict[str, List[str]]]:
        policy = policy_override or (self.get_active_policy(regime_key=regime_key).get("config") or {})
        if not policy:
            return dict(market_snapshots or {}), {}
        allowed = {}
        blocked = {}
        for symbol, snapshot in (market_snapshots or {}).items():
            reasons = self._evaluate_snapshot(snapshot, policy)
            if reasons:
                blocked[str(symbol)] = reasons
            else:
                allowed[str(symbol)] = snapshot
        return allowed, blocked

    def would_block_symbol(self, snapshot: Any, policy_config: Dict[str, Any]) -> Tuple[bool, List[str]]:
        reasons = self._evaluate_snapshot(snapshot, policy_config or {})
        return bool(reasons), reasons

    def _evaluate_snapshot(self, snapshot: Any, policy_config: Dict[str, Any]) -> List[str]:
        if snapshot is None:
            return ["missing_snapshot"]
        reasons: List[str] = []
        min_volume = self._safe_float(policy_config.get("min_volume_ratio_15m"))
        max_spread_pct = self._safe_float(policy_config.get("max_spread_percentile_1h"))
        max_distance = self._safe_float(policy_config.get("max_distance_to_invalid_level"))

        volume_ratio = self._safe_float(getattr(snapshot, "volume_ratio_15m", None))
        spread_percentile = self._safe_float(getattr(snapshot, "spread_percentile_1h", None))
        distance_to_invalid = self._safe_float(getattr(snapshot, "distance_to_invalid_level", None))

        if min_volume is not None and volume_ratio is not None and volume_ratio < min_volume:
            reasons.append(f"volume_ratio_15m<{min_volume}")
        if max_spread_pct is not None and spread_percentile is not None and spread_percentile > max_spread_pct:
            reasons.append(f"spread_percentile_1h>{max_spread_pct}")
        if max_distance is not None and distance_to_invalid is not None and distance_to_invalid > max_distance:
            reasons.append(f"distance_to_invalid_level>{max_distance}")
        return reasons

    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None
