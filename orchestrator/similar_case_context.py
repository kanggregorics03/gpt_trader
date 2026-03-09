from collections import defaultdict


class TradingSimilarCaseContextMixin:
    def _score_similar_past_case_candidate(self, trade_case, match, is_trigger_mode: bool) -> float:
        similarity = float((match or {}).get("similarity") or 0.0)
        final_score = float((match or {}).get("final_score") or 0.0)
        retrieval_source = str((match or {}).get("retrieval_source") or "full_scan").strip().lower() or "full_scan"
        status = str((trade_case or {}).get("status") or "").strip().lower()
        failure_reason = str((trade_case or {}).get("thesis_failure_reason") or "none").strip().lower()
        pnl = (trade_case or {}).get("realized_pnl")
        if pnl is None:
            pnl = (trade_case or {}).get("estimated_pnl_pct")
        try:
            pnl_value = float(pnl)
        except (TypeError, ValueError):
            pnl_value = 0.0

        score = (final_score * 10.0) + (similarity * 8.0)
        if status == "closed":
            score += 2.0
        if pnl_value < 0:
            score += 3.5 if is_trigger_mode else 5.0
        elif pnl_value > 0:
            score += 1.0 if is_trigger_mode else 0.25
        if failure_reason and failure_reason != "none":
            score += 2.5 if is_trigger_mode else 4.0
        if retrieval_source == "band_index":
            score += 0.75
        profile = self._get_learning_artifact_payload("similar_case_reranker_profile")
        if profile:
            score += similarity * float(profile.get("similarity_weight") or 0.0)
            score += final_score * float(profile.get("final_score_weight") or 0.0)
            score += float((match or {}).get("matched_band_count") or 0.0) * float(profile.get("matched_band_weight") or 0.0)
            score += float((profile.get("source_weights") or {}).get(retrieval_source, 0.0) or 0.0)
            if failure_reason and failure_reason != "none":
                score += float(profile.get("failure_reason_weight") or 0.0)
            if pnl_value < 0:
                score += float(profile.get("negative_outcome_weight") or 0.0)
        return score

    def _build_similar_past_cases(
        self,
        market_snapshots,
        account_state,
        risk_state,
        is_trigger_mode: bool,
        limit: int = 5,
    ):
        self.last_similar_case_matches = []
        selection_mode = "trigger" if is_trigger_mode else "background_support"
        target_limit = max(1, int(limit or 1))
        current_feature_vectors = self._build_current_market_vectors(
            market_snapshots,
            account_state=account_state,
            risk_state=risk_state,
        )
        vector_matches = self._search_trade_case_vector_matches(
            current_feature_vectors=current_feature_vectors,
            risk_mode="trigger" if is_trigger_mode else "",
            limit=max(target_limit * 4, 12),
        )
        cases = []
        seen = set()
        selected_sources: dict[str, int] = defaultdict(int)
        ranked_matches = []
        for trade_case_id, match in vector_matches.items():
            trade_case = self.state_store.get_trade_case(trade_case_id)
            if not trade_case:
                continue
            ranked_matches.append(
                (
                    self._score_similar_past_case_candidate(
                        trade_case,
                        match,
                        is_trigger_mode=is_trigger_mode,
                    ),
                    trade_case_id,
                    match,
                    trade_case,
                )
            )
        for _, trade_case_id, match, trade_case in sorted(ranked_matches, key=lambda item: item[0], reverse=True):
            symbol = str(trade_case.get("symbol") or match.get("matched_symbol") or "UNKNOWN")
            if symbol in market_snapshots and not market_snapshots[symbol].is_opportunity_trigger and any(
                snapshot.is_opportunity_trigger for snapshot in market_snapshots.values()
            ):
                continue
            pnl = trade_case.get("realized_pnl")
            if pnl is None:
                pnl = trade_case.get("estimated_pnl_pct")
            failure_reason = trade_case.get("thesis_failure_reason") or "none"
            case_text = (
                f"{symbol}: Similarity {float(match.get('similarity') or 0.0):.2f}; "
                f"status={trade_case.get('status')}; pnl={pnl}; "
                f"slippage_bps={trade_case.get('slippage_bps')}; "
                f"failure_reason={failure_reason}; "
                f"source={match.get('retrieval_source') or 'full_scan'}"
            )
            if case_text in seen:
                continue
            seen.add(case_text)
            self.last_similar_case_matches.append(
                {
                    "context_type": "similar_past_cases",
                    "rank_index": len(cases) + 1,
                    "trade_case_id": trade_case_id,
                    "case_symbol": symbol,
                    "matched_symbol": match.get("matched_symbol") or symbol,
                    "similarity": match.get("similarity"),
                    "final_score": match.get("final_score"),
                    "retrieval_source": match.get("retrieval_source") or "full_scan",
                    "matched_band_count": match.get("matched_band_count") or 0,
                    "payload": {
                        "status": trade_case.get("status"),
                        "thesis_failure_reason": failure_reason,
                        "slippage_bps": trade_case.get("slippage_bps"),
                    },
                }
            )
            selected_sources[str(match.get("retrieval_source") or "full_scan")] += 1
            cases.append(case_text)
            if len(cases) >= target_limit:
                self.last_context_selection_summary["similar_past_cases"] = {
                    "trigger_mode": bool(is_trigger_mode),
                    "selection_mode": selection_mode,
                    "available_total": len(vector_matches),
                    "selected_total": len(cases),
                    "selected_sources": dict(sorted(selected_sources.items())),
                }
                return cases[:target_limit]

        if cases:
            self.last_context_selection_summary["similar_past_cases"] = {
                "trigger_mode": bool(is_trigger_mode),
                "selection_mode": selection_mode,
                "available_total": len(vector_matches),
                "selected_total": len(cases),
                "selected_sources": dict(sorted(selected_sources.items())),
            }
            return cases[:target_limit]

        account_payload = (
            account_state.model_dump(mode="json")
            if hasattr(account_state, "model_dump")
            else dict(account_state or {})
        )
        risk_payload = self._serialize_risk_state(risk_state)
        current_markets = {
            symbol: snapshot.model_dump(mode="json")
            for symbol, snapshot in market_snapshots.items()
        }
        batch_cases = self.rag_engine.get_similar_cases_batch(
            current_markets,
            limit_per_symbol=3,
            account_state=account_payload,
            risk_state=risk_payload,
        )

        for symbol, raw_cases in batch_cases.items():
            if not market_snapshots[symbol].is_opportunity_trigger and any(
                snapshot.is_opportunity_trigger for snapshot in market_snapshots.values()
            ):
                continue
            for case in raw_cases:
                tagged_case = f"{symbol}: {case}"
                if tagged_case in seen:
                    continue
                seen.add(tagged_case)
                self.last_similar_case_matches.append(
                    {
                        "context_type": "similar_past_cases",
                        "rank_index": len(cases) + 1,
                        "trade_case_id": None,
                        "case_symbol": symbol,
                        "matched_symbol": symbol,
                        "similarity": None,
                        "final_score": None,
                        "retrieval_source": "rag_fallback",
                        "matched_band_count": 0,
                        "payload": {"raw_case": case},
                    }
                )
                selected_sources["rag_fallback"] += 1
                cases.append(tagged_case)
                if len(cases) >= target_limit:
                    self.last_context_selection_summary["similar_past_cases"] = {
                        "trigger_mode": bool(is_trigger_mode),
                        "selection_mode": selection_mode,
                        "available_total": max(len(vector_matches), len(cases)),
                        "selected_total": len(cases),
                        "selected_sources": dict(sorted(selected_sources.items())),
                    }
                    return cases[:target_limit]
        self.last_context_selection_summary["similar_past_cases"] = {
            "trigger_mode": bool(is_trigger_mode),
            "selection_mode": selection_mode,
            "available_total": max(len(vector_matches), len(cases)),
            "selected_total": len(cases),
            "selected_sources": dict(sorted(selected_sources.items())),
        }
        return cases[:target_limit]
