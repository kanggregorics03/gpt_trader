class TradingMarketVectorContextMixin:
    def _build_market_case_signature(self, market_snapshots, risk_mode: str) -> dict:
        symbols = set(market_snapshots.keys())
        trigger_reasons = set()
        risk_trigger_reasons = set()
        regime_tags = set()
        for symbol, snapshot in (market_snapshots or {}).items():
            if bool(getattr(snapshot, "fast_move_signal_active", False)):
                trigger_reasons.add(f"{symbol}:fast_price_move_15m")
            if bool(getattr(snapshot, "volume_surge_signal_active", False)):
                trigger_reasons.add(f"{symbol}:volume_surge_15m")
            if bool(getattr(snapshot, "range_break_signal_active", False)):
                trigger_reasons.add(f"{symbol}:range_break_1h")
            for reason in getattr(snapshot, "risk_trigger_reasons", []) or []:
                risk_trigger_reasons.add(f"{symbol}:{reason}")
            trend_alignment = str(getattr(snapshot.regime, "trend_alignment", "") or "").strip()
            volatility_regime = str(getattr(snapshot.regime, "volatility_regime", "") or "").strip()
            if trend_alignment:
                regime_tags.add(f"trend:{trend_alignment}")
            if volatility_regime:
                regime_tags.add(f"volatility:{volatility_regime}")
        return {
            "symbols": symbols,
            "trigger_reasons": trigger_reasons,
            "risk_trigger_reasons": risk_trigger_reasons,
            "regime_tags": regime_tags,
            "risk_mode": str(risk_mode or ""),
        }

    def _derive_regime_key(self, market_snapshots) -> str:
        trend_counts = {}
        volatility_counts = {}
        for snapshot in (market_snapshots or {}).values():
            trend_alignment = str(getattr(snapshot.regime, "trend_alignment", "") or "mixed").strip() or "mixed"
            volatility_regime = str(getattr(snapshot.regime, "volatility_regime", "") or "unknown").strip() or "unknown"
            trend_counts[trend_alignment] = trend_counts.get(trend_alignment, 0) + 1
            volatility_counts[volatility_regime] = volatility_counts.get(volatility_regime, 0) + 1
        if not trend_counts and not volatility_counts:
            return ""
        trend = max(trend_counts.items(), key=lambda item: (item[1], item[0]))[0] if trend_counts else "mixed"
        volatility = max(volatility_counts.items(), key=lambda item: (item[1], item[0]))[0] if volatility_counts else "unknown"
        return f"{trend}|{volatility}"

    def _serialize_market_snapshots(self, market_snapshots) -> dict[str, dict]:
        serialized = {}
        for symbol, snapshot in (market_snapshots or {}).items():
            if hasattr(snapshot, "model_dump"):
                serialized[str(symbol)] = snapshot.model_dump(mode="json")
            elif isinstance(snapshot, dict):
                serialized[str(symbol)] = dict(snapshot)
        return serialized

    def _build_current_market_vectors(self, market_snapshots, account_state=None, risk_state=None) -> dict[str, list[float]]:
        rag_engine = getattr(self, "rag_engine", None)
        if rag_engine is None or not hasattr(rag_engine, "build_market_vectors"):
            return {}
        serialized_snapshots = self._serialize_market_snapshots(market_snapshots)
        if not serialized_snapshots:
            return {}
        account_payload = (
            account_state.model_dump(mode="json")
            if hasattr(account_state, "model_dump")
            else (dict(account_state or {}) if isinstance(account_state, dict) else None)
        )
        risk_payload = self._serialize_risk_state(risk_state)
        try:
            return rag_engine.build_market_vectors(
                serialized_snapshots,
                account_state=account_payload,
                risk_state=risk_payload,
            )
        except Exception:
            return {}

    def _search_trade_case_vector_matches(
        self,
        current_feature_vectors: dict[str, list[float]],
        risk_mode: str,
        limit: int = 24,
    ) -> dict[str, dict]:
        if not current_feature_vectors:
            return {}
        fetch_candidates = getattr(self.state_store, "get_trade_case_vector_candidates", None)
        fetch_band_candidates = getattr(self.state_store, "get_trade_case_vector_band_candidates", None)
        if not callable(fetch_candidates) or getattr(self, "rag_engine", None) is None:
            return {}
        symbols = sorted({str(symbol) for symbol in current_feature_vectors.keys() if str(symbol).strip()})
        raw_candidates = []
        seen_trade_case_ids = set()
        if callable(fetch_band_candidates):
            for row in fetch_band_candidates(
                current_feature_vectors=current_feature_vectors,
                limit=max(limit * 12, 120),
                statuses=["closed", "open", "active", "opened", "reduced"],
            ) or []:
                trade_case_id = str(row.get("trade_case_id") or "").strip()
                if not trade_case_id or trade_case_id in seen_trade_case_ids:
                    continue
                seen_trade_case_ids.add(trade_case_id)
                raw_candidates.append(dict(row, retrieval_source="band_index"))
        if len(raw_candidates) < max(limit * 3, 24):
            for row in fetch_candidates(limit=max(limit * 10, 100), symbols=symbols) or []:
                trade_case_id = str(row.get("trade_case_id") or "").strip()
                if not trade_case_id or trade_case_id in seen_trade_case_ids:
                    continue
                seen_trade_case_ids.add(trade_case_id)
                raw_candidates.append(dict(row, retrieval_source="full_scan"))
        ranked = []
        for row in raw_candidates or []:
            trade_case_id = str(row.get("trade_case_id") or "").strip()
            feature_vector = row.get("feature_vector") or []
            symbol = str(row.get("symbol") or "").strip()
            if not trade_case_id or not feature_vector:
                continue
            comparison_vectors = []
            if symbol and current_feature_vectors.get(symbol):
                comparison_vectors.append((symbol, current_feature_vectors[symbol]))
            else:
                comparison_vectors.extend((candidate_symbol, vector) for candidate_symbol, vector in current_feature_vectors.items())
            best_similarity = 0.0
            matched_symbol = symbol or ""
            for candidate_symbol, current_vector in comparison_vectors:
                similarity = self.rag_engine._cosine_similarity(feature_vector, current_vector)
                if similarity > best_similarity:
                    best_similarity = similarity
                    matched_symbol = candidate_symbol
            if best_similarity <= 0:
                continue
            recency_bonus = 0.5 if row.get("closed_at") or row.get("opened_at") else 0.0
            pnl = row.get("realized_pnl")
            if pnl is None:
                pnl = row.get("estimated_pnl_pct")
            outcome_bonus = 0.0
            try:
                if pnl is not None and float(pnl) < 0:
                    outcome_bonus = 0.4
                elif pnl is not None and float(pnl) > 0:
                    outcome_bonus = 0.2
            except (TypeError, ValueError):
                outcome_bonus = 0.0
            slippage_penalty = 0.0
            try:
                slippage_penalty = min(0.3, max(0.0, float(row.get("slippage_bps") or 0.0) / 50.0))
            except (TypeError, ValueError):
                slippage_penalty = 0.0
            risk_mode_bonus = 0.2 if str(row.get("risk_mode") or "") == str(risk_mode or "") else 0.0
            band_match_bonus = min(0.8, max(0.0, float(row.get("matched_band_count") or 0.0)) * 0.15)
            final_score = best_similarity + recency_bonus + outcome_bonus + risk_mode_bonus + band_match_bonus - slippage_penalty
            ranked.append(
                (
                    final_score,
                    {
                        "trade_case_id": trade_case_id,
                        "symbol": symbol,
                        "matched_symbol": matched_symbol,
                        "similarity": round(best_similarity, 4),
                        "final_score": round(final_score, 4),
                        "thesis_failure_reason": row.get("thesis_failure_reason"),
                        "status": row.get("status"),
                        "slippage_bps": row.get("slippage_bps"),
                        "matched_band_count": int(row.get("matched_band_count") or 0),
                        "retrieval_source": str(row.get("retrieval_source") or "full_scan"),
                    },
                )
            )
        ranked.sort(key=lambda item: item[0], reverse=True)
        return {item["trade_case_id"]: item for _, item in ranked[: max(1, limit)]}
