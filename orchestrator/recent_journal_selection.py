from collections import defaultdict


class TradingRecentJournalSelectionMixin:
    def _select_recent_journal_context(
        self,
        rows,
        market_snapshots,
        risk_mode: str,
        limit: int,
        max_rationale_items: int,
        max_evidence_per_item: int,
        trade_cases=None,
        account_state=None,
        risk_state=None,
    ):
        if not rows:
            self.last_context_selection_summary["recent_journal_context"] = {
                "available_total": 0,
                "selected_total": 0,
                "available_bucket_counts": {},
                "selected_bucket_counts": {},
                "excluded_bucket_counts": {},
                "selected_journal_ids": [],
            }
            return []
        current_signature = self._build_market_case_signature(market_snapshots, risk_mode=risk_mode)
        current_feature_vectors = self._build_current_market_vectors(
            market_snapshots,
            account_state=account_state,
            risk_state=risk_state,
        )
        vector_match_lookup = self._search_trade_case_vector_matches(
            current_feature_vectors=current_feature_vectors,
            risk_mode=risk_mode,
            limit=max(limit * 6, 18),
        )
        trade_case_lookup = {
            str((case or {}).get("trade_case_id") or ""): (case or {})
            for case in (trade_cases or [])
            if (case or {}).get("trade_case_id")
        }
        for trade_case_id in vector_match_lookup.keys():
            if trade_case_id not in trade_case_lookup:
                trade_case = self.state_store.get_trade_case(trade_case_id)
                if trade_case:
                    trade_case_lookup[trade_case_id] = trade_case
        scored = []
        available_bucket_counts: dict[str, int] = defaultdict(int)
        excluded_bucket_counts: dict[str, int] = defaultdict(int)
        for index, row in enumerate(rows):
            score, matched_features = self._score_journal_case_similarity(
                row=row,
                current_signature=current_signature,
                trade_case_lookup=trade_case_lookup,
                vector_match_lookup=vector_match_lookup,
                row_index=index,
            )
            enriched_row = dict(row)
            enriched_row["similarity_score"] = round(score, 4)
            enriched_row["matched_features"] = matched_features
            bucket = self._classify_recent_journal_bucket(enriched_row)
            enriched_row["context_bucket"] = bucket
            enriched_row["row_index"] = index
            available_bucket_counts[bucket] += 1
            if bucket == "system_fallback":
                excluded_bucket_counts[bucket] += 1
            scored.append((score, index, enriched_row))

        usable = [item for item in scored if str(item[2].get("context_bucket") or "") != "system_fallback"]
        bucket_priority = [
            "risk_outcome",
            "good_hold",
            "hold",
            "entry_action",
            "missed_opportunity_hold",
            "other_action",
        ]
        selected_rows_meta: list[dict] = []
        selected_indices: set[int] = set()
        selected_bucket_counts: dict[str, int] = defaultdict(int)
        diversity_counts: dict[str, int] = defaultdict(int)

        if max(1, limit) == 1 and usable:
            best = sorted(usable, key=lambda item: (-item[0], item[1]))[0]
            selected_rows_meta.append(best[2])
            selected_indices.add(best[1])
            selected_bucket_counts[str(best[2].get("context_bucket") or "")] += 1
            diversity_counts[self._journal_decision_diversity_key(best[2])] += 1

        for bucket in bucket_priority:
            if len(selected_rows_meta) >= max(1, limit):
                break
            bucket_candidates = [
                item for item in usable
                if item[1] not in selected_indices and str(item[2].get("context_bucket") or "") == bucket
            ]
            if not bucket_candidates:
                continue
            best = sorted(bucket_candidates, key=lambda item: (-item[0], item[1]))[0]
            selected_rows_meta.append(best[2])
            selected_indices.add(best[1])
            selected_bucket_counts[bucket] += 1
            diversity_counts[self._journal_decision_diversity_key(best[2])] += 1

        while len(selected_rows_meta) < max(1, limit):
            remaining = [item for item in usable if item[1] not in selected_indices]
            if not remaining:
                break
            best = max(
                remaining,
                key=lambda item: (
                    item[0]
                    - (selected_bucket_counts[str(item[2].get("context_bucket") or "")] * 1.5)
                    - (diversity_counts[self._journal_decision_diversity_key(item[2])] * 2.5),
                    -item[1],
                ),
            )
            selected_rows_meta.append(best[2])
            selected_indices.add(best[1])
            selected_bucket_counts[str(best[2].get("context_bucket") or "")] += 1
            diversity_counts[self._journal_decision_diversity_key(best[2])] += 1

        selected_rows = {int(row["row_index"]): row for row in selected_rows_meta}
        ordered_rows = [selected_rows[index] for index in range(len(rows)) if index in selected_indices]
        self.last_context_selection_summary["recent_journal_context"] = {
            "available_total": len(rows),
            "selected_total": len(ordered_rows),
            "available_bucket_counts": dict(sorted(available_bucket_counts.items())),
            "selected_bucket_counts": dict(sorted(selected_bucket_counts.items())),
            "excluded_bucket_counts": dict(sorted(excluded_bucket_counts.items())),
            "selected_journal_ids": [
                row.get("journal_id") or row.get("id")
                for row in ordered_rows
                if row.get("journal_id") or row.get("id")
            ],
        }
        return [
            self._compact_recent_journal_row(
                row,
                max_rationale_items=max_rationale_items,
                max_evidence_per_item=max_evidence_per_item,
            )
            for row in ordered_rows
        ]
