import math
import time
from typing import Any, Dict, List

from loguru import logger

from db.state_store import StateStore


class RagEngine:
    # Base market features (legacy 13D) + context features (risk/account/position 13D)
    _FEATURE_NORMALIZERS: List[float] = [
        500.0,    # atr_15m
        1000.0,   # atr_1h
        1.0,      # trend_1h
        1.0,      # recent_volume_surge
        5.0,      # tf_15m change_3bar_pct
        5.0,      # tf_1h change_3bar_pct
        8.0,      # tf_4h change_3bar_pct
        100.0,    # tf_15m rsi_14
        100.0,    # tf_1h rsi_14
        1.0,      # order_book imbalance_top5
        0.005,    # funding_rate
        20.0,     # price_change_24h_pct
        1.0,      # trend_alignment
        10.0,     # risk daily_pnl_pct
        5.0,      # risk consecutive_losses
        1.0,      # risk cooldown_active
        120.0,    # risk cooldown_remaining_minutes
        1.0,      # risk drawdown_active
        1.0,      # account margin_ratio
        1.0,      # account available_balance_ratio
        3.0,      # account portfolio_exposure_ratio
        10.0,     # account portfolio_position_count
        1.0,      # symbol position side
        2.0,      # symbol notional exposure ratio
        20.0,     # symbol unrealized_pnl_pct
        10.0,     # symbol open_orders_count
    ]
    _FEATURE_WEIGHTS: List[float] = [
        1.2, 1.2, 1.2, 0.8, 1.0, 1.0, 1.0, 0.9, 0.9, 1.0, 1.1, 1.0, 1.1,
        1.5, 1.3, 1.2, 1.2, 1.5, 1.2, 1.1, 1.2, 1.1, 1.3, 1.4, 1.2, 1.0,
    ]

    def __init__(self, db_path: str = "data/gpt_trader.db", candidate_cache_ttl_ms: int = 5000):
        self.db_path = db_path
        self.candidate_cache_ttl_ms = candidate_cache_ttl_ms
        self._candidate_cache: Dict[str, Any] = {"fetched_at_ms": 0, "rows": []}

    def _safe_float(self, value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _normalize_and_weight(self, vector: List[float], target_length: int) -> List[float]:
        weighted: List[float] = []
        for idx in range(target_length):
            raw_value = self._safe_float(vector[idx], 0.0) if idx < len(vector) else 0.0
            scale = self._FEATURE_NORMALIZERS[idx] if idx < len(self._FEATURE_NORMALIZERS) else 1.0
            if scale == 0:
                scale = 1.0
            normalized = raw_value / scale
            # Prevent outliers from dominating cosine direction.
            normalized = max(-3.0, min(3.0, normalized))
            weight = self._FEATURE_WEIGHTS[idx] if idx < len(self._FEATURE_WEIGHTS) else 1.0
            weighted.append(normalized * weight)
        return weighted

    def _cosine_similarity(self, v1: List[float], v2: List[float]) -> float:
        target_length = max(len(v1), len(v2))
        if target_length <= 0:
            return 0.0

        vv1 = self._normalize_and_weight(v1, target_length)
        vv2 = self._normalize_and_weight(v2, target_length)

        dot_product = sum(a * b for a, b in zip(vv1, vv2))
        magnitude_v1 = math.sqrt(sum(a * a for a in vv1))
        magnitude_v2 = math.sqrt(sum(a * a for a in vv2))
        if magnitude_v1 == 0 or magnitude_v2 == 0:
            return 0.0
        return dot_product / magnitude_v1 / magnitude_v2

    def _extract_context_vector(
        self,
        symbol: str,
        account_state: Dict[str, Any] | None = None,
        risk_state: Dict[str, Any] | None = None,
    ) -> List[float]:
        risk = risk_state if isinstance(risk_state, dict) else {}
        account = account_state if isinstance(account_state, dict) else {}

        total_balance = self._safe_float(account.get("total_balance"), 0.0)
        available_balance = self._safe_float(account.get("available_balance"), 0.0)
        available_balance_ratio = (available_balance / total_balance) if total_balance > 0 else 0.0
        margin_ratio = self._safe_float(account.get("margin_ratio"), 0.0)

        raw_positions = account.get("positions")
        positions = raw_positions if isinstance(raw_positions, list) else []
        raw_open_orders = account.get("open_orders")
        open_orders = raw_open_orders if isinstance(raw_open_orders, list) else []
        symbol_upper = str(symbol).upper()

        portfolio_notional = 0.0
        symbol_position: Dict[str, Any] = {}
        for pos in positions:
            if not isinstance(pos, dict):
                continue
            notional = abs(self._safe_float(pos.get("notional_value"), 0.0))
            if notional == 0.0:
                size = abs(self._safe_float(pos.get("size"), 0.0))
                entry_price = self._safe_float(pos.get("entry_price"), 0.0)
                notional = size * entry_price
            portfolio_notional += notional
            if str(pos.get("symbol", "")).upper() == symbol_upper:
                symbol_position = pos

        portfolio_exposure_ratio = (portfolio_notional / total_balance) if total_balance > 0 else 0.0
        portfolio_position_count = float(len(positions))

        symbol_side = str(symbol_position.get("side", "")).upper()
        symbol_side_value = 1.0 if symbol_side == "LONG" else (-1.0 if symbol_side == "SHORT" else 0.0)
        symbol_notional = abs(self._safe_float(symbol_position.get("notional_value"), 0.0))
        if symbol_notional == 0.0 and symbol_position:
            symbol_notional = (
                abs(self._safe_float(symbol_position.get("size"), 0.0))
                * self._safe_float(symbol_position.get("entry_price"), 0.0)
            )
        symbol_notional_ratio = (symbol_notional / total_balance) if total_balance > 0 else 0.0
        symbol_unrealized_pnl_pct = self._safe_float(symbol_position.get("unrealized_pnl_pct"), 0.0)
        symbol_open_orders_count = float(
            sum(
                1
                for order in open_orders
                if isinstance(order, dict) and str(order.get("symbol", "")).upper() == symbol_upper
            )
        )

        return [
            self._safe_float(risk.get("daily_pnl_pct"), 0.0),
            self._safe_float(risk.get("consecutive_losses"), 0.0),
            1.0 if bool(risk.get("cooldown_active")) else 0.0,
            self._safe_float(risk.get("cooldown_remaining_minutes"), 0.0),
            1.0 if bool(risk.get("drawdown_active")) else 0.0,
            margin_ratio,
            available_balance_ratio,
            portfolio_exposure_ratio,
            portfolio_position_count,
            symbol_side_value,
            symbol_notional_ratio,
            symbol_unrealized_pnl_pct,
            symbol_open_orders_count,
        ]

    def _extract_vector(
        self,
        market_state: Dict[str, Any],
        symbol: str | None = None,
        account_state: Dict[str, Any] | None = None,
        risk_state: Dict[str, Any] | None = None,
    ) -> List[float]:
        try:
            resolved_symbol = symbol or str(market_state.get("symbol") or "UNKNOWN")
            atr_15m = float(market_state.get("atr_15m", 0.0))
            atr_1h = float(market_state.get("atr_1h", 0.0))
            trend = market_state.get("trend_1h", "FLAT")
            trend_val = 1.0 if trend == "UP" else (-1.0 if trend == "DOWN" else 0.0)
            surge = 1.0 if market_state.get("recent_volume_surge") else 0.0

            order_book = market_state.get("order_book") or {}
            derivatives = market_state.get("derivatives") or {}
            regime = market_state.get("regime") or {}
            timeframes = market_state.get("timeframes") or {}
            tf_15m = timeframes.get("15m") or {}
            tf_1h = timeframes.get("1h") or {}
            tf_4h = timeframes.get("4h") or {}

            trend_alignment = regime.get("trend_alignment", "mixed")
            trend_alignment_val = (
                1.0 if trend_alignment == "bullish" else (-1.0 if trend_alignment == "bearish" else 0.0)
            )

            liquidity = float(order_book.get("imbalance_top5", 0.5))
            funding_rate = float(derivatives.get("funding_rate") or 0.0)
            price_change_24h = float(market_state.get("price_change_24h_pct") or 0.0)

            base_vector = [
                atr_15m,
                atr_1h,
                trend_val,
                surge,
                float(tf_15m.get("change_3bar_pct") or 0.0),
                float(tf_1h.get("change_3bar_pct") or 0.0),
                float(tf_4h.get("change_3bar_pct") or 0.0),
                float(tf_15m.get("rsi_14") or 50.0),
                float(tf_1h.get("rsi_14") or 50.0),
                liquidity,
                funding_rate,
                price_change_24h,
                trend_alignment_val,
            ]
            return base_vector + self._extract_context_vector(
                resolved_symbol,
                account_state=account_state,
                risk_state=risk_state,
            )
        except Exception:
            return [0.0] * len(self._FEATURE_NORMALIZERS)

    def build_market_vectors(
        self,
        market_snapshots: Dict[str, Dict[str, Any]],
        account_state: Dict[str, Any] | None = None,
        risk_state: Dict[str, Any] | None = None,
    ) -> Dict[str, List[float]]:
        vectors = {}
        for symbol, snapshot in market_snapshots.items():
            vector = self._extract_vector(
                snapshot,
                symbol=str(symbol),
                account_state=account_state,
                risk_state=risk_state,
            )
            if any(value != 0.0 for value in vector):
                vectors[symbol] = vector
        return vectors

    def _extract_market_vectors_from_audit(
        self,
        audit_market_state: Dict[str, Any],
        account_state: Dict[str, Any] | None = None,
        risk_state: Dict[str, Any] | None = None,
    ) -> Dict[str, List[float]]:
        if not isinstance(audit_market_state, dict):
            return {}
        rag_context = audit_market_state.get("rag_context")
        if isinstance(rag_context, dict):
            if account_state is None and isinstance(rag_context.get("account_state"), dict):
                account_state = rag_context.get("account_state")
            if risk_state is None and isinstance(rag_context.get("risk_state"), dict):
                risk_state = rag_context.get("risk_state")
        market_snapshots = audit_market_state.get("market_snapshots")
        if isinstance(market_snapshots, dict) and market_snapshots:
            return self.build_market_vectors(
                market_snapshots,
                account_state=account_state,
                risk_state=risk_state,
            )

        symbol = audit_market_state.get("symbol")
        vector = self._extract_vector(
            audit_market_state,
            symbol=str(symbol) if symbol else None,
            account_state=account_state,
            risk_state=risk_state,
        )
        if symbol and any(value != 0.0 for value in vector):
            return {str(symbol): vector}
        return {}

    def _get_recent_candidates(self, limit: int = 500) -> List[Dict[str, Any]]:
        now_ms = int(time.time() * 1000)
        cached_rows = self._candidate_cache.get("rows") or []
        cached_at_ms = int(self._candidate_cache.get("fetched_at_ms") or 0)
        if cached_rows and (now_ms - cached_at_ms) <= self.candidate_cache_ttl_ms:
            return cached_rows

        store = StateStore(self.db_path)
        try:
            rows = store.get_recent_rag_candidates(limit=limit)
        finally:
            store.close()

        self._candidate_cache = {
            "fetched_at_ms": now_ms,
            "rows": rows,
        }
        return rows

    def _format_case(self, similarity: float, row: Dict[str, Any]) -> str:
        intent = row.get("intent") or {}
        decision = intent.get("decision", "UNKNOWN")
        rationale = intent.get("rationale") or []
        rationale_str = rationale[0] if rationale else "No specific rationale"
        return (
            f"Past Similarity {similarity:.2f}: We decided {decision}. "
            f"Rationale: {rationale_str}. Risk Approved: {row.get('risk_approved')}"
        )

    def get_similar_cases_batch(
        self,
        current_markets: Dict[str, Dict[str, Any]],
        limit_per_symbol: int = 3,
        candidate_limit: int = 500,
        account_state: Dict[str, Any] | None = None,
        risk_state: Dict[str, Any] | None = None,
    ) -> Dict[str, List[str]]:
        current_vectors = self.build_market_vectors(
            current_markets,
            account_state=account_state,
            risk_state=risk_state,
        )
        if not current_vectors:
            return {}

        candidates = self._get_recent_candidates(limit=candidate_limit)
        scored_by_symbol: Dict[str, List[tuple[float, str]]] = {
            symbol: [] for symbol in current_vectors
        }

        for row in candidates:
            past_vectors = row.get("market_vectors") or {}
            if not past_vectors:
                past_risk_state = None
                market_state = row.get("market_state") or {}
                rag_context = market_state.get("rag_context") if isinstance(market_state, dict) else {}
                if isinstance(rag_context, dict):
                    maybe_risk = rag_context.get("risk_state")
                    if isinstance(maybe_risk, dict):
                        past_risk_state = maybe_risk
                past_vectors = self._extract_market_vectors_from_audit(
                    market_state,
                    account_state=row.get("account_state") if isinstance(row.get("account_state"), dict) else None,
                    risk_state=past_risk_state,
                )
            if not past_vectors:
                continue

            for symbol, current_vector in current_vectors.items():
                direct_match = past_vectors.get(symbol)
                if direct_match is not None:
                    similarity = self._cosine_similarity(current_vector, direct_match)
                else:
                    similarity = max(
                        (self._cosine_similarity(current_vector, past_vector) for past_vector in past_vectors.values()),
                        default=0.0,
                    )
                if similarity <= 0:
                    continue
                scored_by_symbol[symbol].append((similarity, self._format_case(similarity, row)))

        results: Dict[str, List[str]] = {}
        for symbol, scored_cases in scored_by_symbol.items():
            scored_cases.sort(key=lambda item: item[0], reverse=True)
            results[symbol] = [case for _, case in scored_cases[:limit_per_symbol]]
        return results

    def get_similar_cases(self, current_market: Dict[str, Any], limit: int = 3) -> List[str]:
        symbol = str(current_market.get("symbol") or "UNKNOWN")
        try:
            return self.get_similar_cases_batch({symbol: current_market}, limit_per_symbol=limit).get(symbol, [])
        except Exception as e:
            logger.error(f"RAG Engine failed to get similar cases: {e}")
            return []
