import math
import time
from typing import Any, Dict, List

from loguru import logger

from db.state_store import StateStore


class RagEngine:
    def __init__(self, db_path: str = "data/gpt_trader.db", candidate_cache_ttl_ms: int = 5000):
        self.db_path = db_path
        self.candidate_cache_ttl_ms = candidate_cache_ttl_ms
        self._candidate_cache: Dict[str, Any] = {"fetched_at_ms": 0, "rows": []}

    def _cosine_similarity(self, v1: List[float], v2: List[float]) -> float:
        dot_product = sum(a * b for a, b in zip(v1, v2))
        magnitude_v1 = math.sqrt(sum(a * a for a in v1))
        magnitude_v2 = math.sqrt(sum(a * a for a in v2))
        if magnitude_v1 == 0 or magnitude_v2 == 0:
            return 0.0
        return dot_product / magnitude_v1 / magnitude_v2

    def _extract_vector(self, market_state: Dict[str, Any]) -> List[float]:
        try:
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

            return [
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
        except Exception:
            return [0.0] * 13

    def build_market_vectors(self, market_snapshots: Dict[str, Dict[str, Any]]) -> Dict[str, List[float]]:
        vectors = {}
        for symbol, snapshot in market_snapshots.items():
            vector = self._extract_vector(snapshot)
            if any(value != 0.0 for value in vector):
                vectors[symbol] = vector
        return vectors

    def _extract_market_vectors_from_audit(self, audit_market_state: Dict[str, Any]) -> Dict[str, List[float]]:
        if not isinstance(audit_market_state, dict):
            return {}
        market_snapshots = audit_market_state.get("market_snapshots")
        if isinstance(market_snapshots, dict) and market_snapshots:
            return self.build_market_vectors(market_snapshots)

        symbol = audit_market_state.get("symbol")
        vector = self._extract_vector(audit_market_state)
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
    ) -> Dict[str, List[str]]:
        current_vectors = self.build_market_vectors(current_markets)
        if not current_vectors:
            return {}

        candidates = self._get_recent_candidates(limit=candidate_limit)
        scored_by_symbol: Dict[str, List[tuple[float, str]]] = {
            symbol: [] for symbol in current_vectors
        }

        for row in candidates:
            past_vectors = row.get("market_vectors") or {}
            if not past_vectors:
                past_vectors = self._extract_market_vectors_from_audit(row.get("market_state") or {})
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
