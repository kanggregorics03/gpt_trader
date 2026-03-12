class TradingJournalScoringMixin:
    def _apply_recent_journal_reranker_adjustment(self, row) -> tuple[float, list[str]]:
        profile = self._get_learning_artifact_payload("journal_reranker_profile")
        if not profile:
            return 0.0, []
        adjustment = 0.0
        features: list[str] = []
        bucket = self._classify_recent_journal_bucket(row)
        decision = str(row.get("decision") or "HOLD").upper()
        bucket_weights = profile.get("bucket_weights") or {}
        decision_weights = profile.get("decision_weights") or {}
        if bucket in bucket_weights:
            value = float(bucket_weights.get(bucket) or 0.0)
            adjustment += value
            if value:
                features.append(f"learned_bucket:{bucket}")
        if decision in decision_weights:
            value = float(decision_weights.get(decision) or 0.0)
            adjustment += value
            if value:
                features.append(f"learned_decision:{decision}")
        if bool(row.get("loss_signal")):
            value = float(profile.get("loss_signal_weight") or 0.0)
            adjustment += value
            if value:
                features.append("learned_loss_signal")
        if row.get("estimated_pnl_pct") is not None:
            value = float(profile.get("pnl_presence_weight") or 0.0)
            adjustment += value
            if value:
                features.append("learned_pnl_presence")
        return round(adjustment, 4), features

    def _score_journal_case_similarity(
        self,
        row,
        current_signature: dict,
        trade_case_lookup: dict[str, dict],
        vector_match_lookup: dict[str, dict],
        row_index: int,
    ) -> tuple[float, list[str]]:
        score = 0.0
        matched_features: list[str] = []
        decision = str(row.get("decision") or "HOLD").upper()
        if decision in ("REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"):
            score += 8.0
            matched_features.append(f"decision:{decision}")
        elif decision in ("BUY", "SELL"):
            score += 6.0
            matched_features.append(f"decision:{decision}")
        elif decision == "HOLD":
            bucket = self._classify_recent_journal_bucket(row)
            if bucket == "good_hold":
                score += 1.75
                matched_features.append("good_hold")
            elif bucket == "missed_opportunity_hold":
                score += 4.25
                matched_features.append("missed_opportunity_hold")
        if bool(row.get("loss_signal")):
            score += 8.0
            matched_features.append("loss_signal")
        if str(row.get("risk_mode") or "") == current_signature["risk_mode"]:
            score += 8.0
            matched_features.append("risk_mode")
        if row.get("estimated_pnl_pct") is not None:
            score += 3.0
            matched_features.append("has_pnl")
        trigger_symbols = set(row.get("trigger_symbols") or [])
        risk_trigger_symbols = set(row.get("risk_trigger_symbols") or [])
        evidence_symbols = self._extract_symbols_from_rationale_items(row.get("rationale_items") or [])
        symbol_overlap = current_signature["symbols"].intersection(trigger_symbols | risk_trigger_symbols | evidence_symbols)
        if symbol_overlap:
            score += 10.0
            matched_features.append("symbol_overlap")
        if str(row.get("risk_reason") or "") not in ("", "Approved", "Safe action."):
            score += 2.0
            matched_features.append("non_default_risk_reason")

        case_statuses = row.get("case_statuses") or {}
        if any(status in ("opened", "closed", "reduced") for status in case_statuses.values()):
            score += 4.0
            matched_features.append("case_status")

        for trade_case_id in row.get("trade_case_ids") or []:
            trade_case = trade_case_lookup.get(trade_case_id) or {}
            if not trade_case:
                continue
            if str(trade_case.get("risk_mode") or "") == current_signature["risk_mode"]:
                score += 4.0
                matched_features.append("case_risk_mode")
            if trade_case.get("symbol") in current_signature["symbols"]:
                score += 8.0
                matched_features.append("case_symbol")
            trigger_overlap = current_signature["trigger_reasons"].intersection(
                {f"{trade_case.get('symbol')}:{reason}" for reason in (trade_case.get("trigger_reasons") or [])}
            )
            if trigger_overlap:
                score += 8.0
                matched_features.append("trigger_overlap")
            risk_trigger_overlap = current_signature["risk_trigger_reasons"].intersection(
                {f"{trade_case.get('symbol')}:{reason}" for reason in (trade_case.get("risk_trigger_reasons") or [])}
            )
            if risk_trigger_overlap:
                score += 6.0
                matched_features.append("risk_trigger_overlap")
            case_payload = trade_case.get("payload") or {}
            case_regime_tags = set(case_payload.get("regime_tags") or [])
            if case_regime_tags and current_signature["regime_tags"].intersection(case_regime_tags):
                score += 5.0
                matched_features.append("regime_overlap")
            evidence_overlap = set(trade_case.get("evidence_signals") or []).intersection(
                set((case_payload.get("entry_evidence_signals") or []))
            )
            if evidence_overlap:
                score += 2.0
                matched_features.append("case_evidence")
            status = str(trade_case.get("status") or "")
            realized_pnl = trade_case.get("realized_pnl")
            estimated_pnl_pct = trade_case.get("estimated_pnl_pct")
            if status == "closed":
                score += 4.0
                matched_features.append("closed_case")
                if (realized_pnl is not None and float(realized_pnl) < 0) or (
                    estimated_pnl_pct is not None and float(estimated_pnl_pct) < 0
                ):
                    score += 4.0
                    matched_features.append("closed_loss")
                elif (realized_pnl is not None and float(realized_pnl) > 0) or (
                    estimated_pnl_pct is not None and float(estimated_pnl_pct) > 0
                ):
                    score += 2.0
                    matched_features.append("closed_win")
            vector_match = vector_match_lookup.get(trade_case_id) or {}
            best_similarity = float(vector_match.get("similarity") or 0.0)
            if best_similarity > 0:
                score += min(18.0, best_similarity * 18.0)
                matched_features.append(f"vector_similarity:{best_similarity:.2f}")
                if vector_match.get("matched_symbol"):
                    matched_features.append(f"vector_symbol:{vector_match.get('matched_symbol')}")
                if vector_match.get("retrieval_source"):
                    matched_features.append(f"vector_source:{vector_match.get('retrieval_source')}")
                if vector_match.get("thesis_failure_reason"):
                    matched_features.append("vector_failure_reason")

        learned_adjustment, learned_features = self._apply_recent_journal_reranker_adjustment(row)
        if learned_adjustment:
            score += learned_adjustment
            matched_features.extend(learned_features)
        score += max(0.0, 2.5 - (row_index * 0.08))
        deduped_features = list(dict.fromkeys(matched_features))
        return score, deduped_features
