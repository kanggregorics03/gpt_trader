from collections import defaultdict


class TradingMemoryContextMixin:
    def _compact_memory_record(self, row, max_ignore_when: int):
        metrics = row.get("metrics") or {}
        apply_when = row.get("apply_when") or metrics.get("apply_when") or {
            "regime": [row.get("regime")] if row.get("regime") else [],
            "volatility": [],
            "trend": [],
        }
        ignore_when = row.get("ignore_when") or metrics.get("ignore_when") or []
        evidence_ids = row.get("evidence_journal_ids") or metrics.get("evidence_journal_ids") or []
        compact_metrics = {
            "support": int(row.get("support") or metrics.get("support") or 0),
            "status": row.get("status") or metrics.get("status") or "rule",
            "category": row.get("category") or metrics.get("category") or "lesson",
            "regime": row.get("regime") or metrics.get("regime") or "general",
            "confidence_score": float(row.get("confidence_score") or metrics.get("confidence_score") or 0.0),
            "validity_score": float(row.get("validity_score") or metrics.get("validity_score") or 0.0),
            "freshness_score": float(row.get("freshness_score") or metrics.get("freshness_score") or 0.0),
            "lifecycle_status": row.get("lifecycle_status") or metrics.get("lifecycle_status") or "active",
            "apply_when": apply_when,
            "ignore_when": ignore_when[:max_ignore_when],
            "evidence_journal_ids": evidence_ids[:10],
            "trigger_context": str(row.get("trigger_context") or metrics.get("trigger_context") or ""),
            "execution_context": str(row.get("execution_context") or metrics.get("execution_context") or ""),
            "trend_context": str(row.get("trend_context") or metrics.get("trend_context") or ""),
            "hold_quality": str(row.get("hold_quality") or metrics.get("hold_quality") or ""),
        }
        return {
            "rule": row.get("rule") or "",
            "category": compact_metrics["category"],
            "status": compact_metrics["status"],
            "support": compact_metrics["support"],
            "regime": compact_metrics["regime"],
            "apply_when": compact_metrics["apply_when"],
            "ignore_when": compact_metrics["ignore_when"],
            "evidence_journal_ids": compact_metrics["evidence_journal_ids"],
            "trigger_context": compact_metrics["trigger_context"],
            "execution_context": compact_metrics["execution_context"],
            "trend_context": compact_metrics["trend_context"],
            "hold_quality": compact_metrics["hold_quality"],
            "metrics": compact_metrics,
        }

    def _build_memory_context_signatures(self, market_snapshots) -> dict[str, set[str]]:
        trigger_contexts: set[str] = set()
        execution_contexts: set[str] = set()
        trend_contexts: set[str] = set()
        for snapshot in (market_snapshots or {}).values():
            active_flags = []
            if bool(getattr(snapshot, "volume_surge_signal_active", False)):
                active_flags.append("volume_surge")
            if bool(getattr(snapshot, "fast_move_signal_active", False)):
                active_flags.append("fast_move")
            if bool(getattr(snapshot, "range_break_signal_active", False)):
                active_flags.append("range_break")
            if bool(getattr(snapshot, "is_risk_trigger", False)):
                active_flags.append("risk")
            trigger_contexts.add("+".join(active_flags) if active_flags else "none")

            spread_percentile = getattr(snapshot, "spread_percentile_1h", None)
            depth_percentile = getattr(snapshot, "depth_percentile_1h", None)
            invalidation_distance = getattr(snapshot, "distance_to_invalid_level", None)
            if spread_percentile is None:
                spread_bucket = "unknown_spread"
            elif float(spread_percentile) <= 40.0:
                spread_bucket = "tight_spread"
            elif float(spread_percentile) <= 70.0:
                spread_bucket = "acceptable_spread"
            else:
                spread_bucket = "wide_spread"
            if depth_percentile is None:
                depth_bucket = "unknown_depth"
            elif float(depth_percentile) >= 60.0:
                depth_bucket = "deep_book"
            elif float(depth_percentile) >= 25.0:
                depth_bucket = "tradable_depth"
            else:
                depth_bucket = "thin_book"
            if invalidation_distance is None:
                invalidation_bucket = "unknown_invalidation"
            elif float(invalidation_distance) <= 0.8:
                invalidation_bucket = "tight_invalidation"
            elif float(invalidation_distance) <= 1.5:
                invalidation_bucket = "manageable_invalidation"
            else:
                invalidation_bucket = "wide_invalidation"
            execution_contexts.add(f"{spread_bucket}|{depth_bucket}|{invalidation_bucket}")

            trend_1h = str(getattr(snapshot, "trend_1h", "") or "UNKNOWN").strip().upper() or "UNKNOWN"
            timeframe_15m = getattr(snapshot, "timeframes", {}).get("15m") if getattr(snapshot, "timeframes", None) else {}
            trend_15m = str(getattr(timeframe_15m, "trend", None) or (timeframe_15m.get("trend") if isinstance(timeframe_15m, dict) else "") or "UNKNOWN").strip().upper() or "UNKNOWN"
            trend_contexts.add(f"{trend_1h}|{trend_15m}")

        return {
            "trigger": trigger_contexts,
            "execution": execution_contexts,
            "trend": trend_contexts,
        }

    def _memory_context_gate_score(
        self,
        *,
        category: str,
        trigger_context: str,
        execution_context: str,
        trend_context: str,
        hold_quality: str,
        active_contexts: dict[str, set[str]],
    ) -> float:
        score = 0.0
        category_text = str(category or "").strip().lower()
        trigger_text = str(trigger_context or "").strip().lower()
        execution_text = str(execution_context or "").strip().lower()
        trend_text = str(trend_context or "").strip().lower()
        hold_quality_text = str(hold_quality or "").strip().lower()
        active_trigger = {item.lower() for item in active_contexts.get("trigger") or set()}
        active_execution = {item.lower() for item in active_contexts.get("execution") or set()}
        active_trend = {item.lower() for item in active_contexts.get("trend") or set()}

        if trigger_text:
            if trigger_text in active_trigger:
                score += 9.0
            elif trigger_text != "none":
                score -= 7.5 if category_text == "opportunity" else 2.0
        elif category_text == "opportunity":
            score -= 2.0

        if execution_text:
            if execution_text in active_execution:
                score += 7.0
            elif category_text == "opportunity":
                score -= 5.5
            else:
                score -= 1.5

        if trend_text:
            if trend_text in active_trend:
                score += 6.5
            elif category_text in {"opportunity", "risk_control"}:
                score -= 4.5
            else:
                score -= 1.0

        if hold_quality_text == "missed_opportunity_hold" and trigger_text and trigger_text not in active_trigger:
            score -= 6.0
        elif hold_quality_text == "good_hold" and trigger_text == "none" and "none" in active_trigger:
            score += 1.25
        return score

    def _select_long_term_memory_context(self, rows, market_snapshots, risk_mode: str, limit: int, max_ignore_when: int):
        if not rows:
            self.last_context_selection_summary["long_term_memory"] = {
                "available_total": 0,
                "selected_total": 0,
                "available_bucket_counts": {},
                "selected_bucket_counts": {},
                "excluded_bucket_counts": {},
                "selected_rules": [],
            }
            return []
        current_symbols = set(market_snapshots.keys())
        active_trends = set()
        active_volatility = set()
        for snapshot in market_snapshots.values():
            active_trends.add(str(getattr(snapshot.regime, "trend_alignment", "") or ""))
            active_volatility.add(str(getattr(snapshot.regime, "volatility_regime", "") or ""))
        active_contexts = self._build_memory_context_signatures(market_snapshots)

        scored = []
        available_bucket_counts: dict[str, int] = defaultdict(int)
        excluded_bucket_counts: dict[str, int] = defaultdict(int)
        for index, row in enumerate(rows):
            score = 0.0
            metrics = row.get("metrics") or {}
            status = str(row.get("status") or "rule")
            support = int(row.get("support") or 0)
            category = str(row.get("category") or "lesson")
            rule_text = str(row.get("rule") or "")
            apply_when = row.get("apply_when") or {}
            confidence_score = float(row.get("confidence_score") or metrics.get("confidence_score") or 0.0)
            validity_score = float(row.get("validity_score") or metrics.get("validity_score") or 0.0)
            freshness_score = float(row.get("freshness_score") or metrics.get("freshness_score") or 0.0)
            lifecycle_status = str(row.get("lifecycle_status") or metrics.get("lifecycle_status") or "active")
            trigger_context = str(row.get("trigger_context") or metrics.get("trigger_context") or "")
            execution_context = str(row.get("execution_context") or metrics.get("execution_context") or "")
            trend_context = str(row.get("trend_context") or metrics.get("trend_context") or "")
            hold_quality = str(row.get("hold_quality") or metrics.get("hold_quality") or "").strip().lower()
            retired = bool(row.get("retired") or metrics.get("retired") or lifecycle_status == "retired")
            bucket = self._classify_memory_bucket(row, metrics=metrics)
            available_bucket_counts[bucket] += 1
            if retired or lifecycle_status == "retired":
                if support < 4 or confidence_score < 0.65 or validity_score < 0.65:
                    excluded_bucket_counts[bucket] += 1
                    continue
                score -= 7.0
            apply_regimes = set(apply_when.get("regime") or [])
            apply_trends = set(apply_when.get("trend") or [])
            apply_volatility = set(apply_when.get("volatility") or [])

            score += 8.0 if status == "rule" else 3.0
            score += min(10.0, float(support))
            score += confidence_score * 10.0
            score += validity_score * 8.0
            score += freshness_score * 4.0
            if category == "risk_control":
                score += 4.0
            elif category in {"execution_quality", "execution", "microstructure"}:
                score += 2.5
            if hold_quality == "good_hold":
                score += 0.75
            elif hold_quality in {"missed_opportunity_hold", "probe_only_opportunity_hold"}:
                score += 2.5
            if str(risk_mode or "") in apply_regimes:
                score += 8.0
            if active_trends and active_trends.intersection(apply_trends):
                score += 6.0
            if active_volatility and active_volatility.intersection(apply_volatility):
                score += 5.0
            score += self._memory_context_gate_score(
                category=category,
                trigger_context=trigger_context,
                execution_context=execution_context,
                trend_context=trend_context,
                hold_quality=hold_quality,
                active_contexts=active_contexts,
            )
            if current_symbols and any(symbol in rule_text for symbol in current_symbols):
                score += 4.0
            score += max(0.0, 2.0 - (index * 0.08))
            enriched_row = dict(row)
            enriched_row["memory_bucket"] = bucket
            enriched_row["row_index"] = index
            enriched_row["selection_score"] = round(score, 4)
            scored.append((score, index, enriched_row))

        bucket_priority = [
            "risk_control",
            "execution_quality",
            "opportunity",
            "missed_opportunity_hold",
            "good_hold",
            "general",
            "emerging",
            "retired_counterpoint",
        ]
        selected_rows_meta: list[dict] = []
        selected_indices: set[int] = set()
        selected_bucket_counts: dict[str, int] = defaultdict(int)

        for bucket in bucket_priority:
            if len(selected_rows_meta) >= max(1, limit):
                break
            bucket_candidates = [
                item for item in scored
                if item[1] not in selected_indices and str(item[2].get("memory_bucket") or "") == bucket
            ]
            if not bucket_candidates:
                continue
            best = sorted(bucket_candidates, key=lambda item: (-item[0], item[1]))[0]
            selected_rows_meta.append(best[2])
            selected_indices.add(best[1])
            selected_bucket_counts[bucket] += 1

        while len(selected_rows_meta) < max(1, limit):
            remaining = [item for item in scored if item[1] not in selected_indices]
            if not remaining:
                break
            best = max(
                remaining,
                key=lambda item: (
                    item[0] - (selected_bucket_counts[str(item[2].get("memory_bucket") or "")] * 1.25),
                    -item[1],
                ),
            )
            selected_rows_meta.append(best[2])
            selected_indices.add(best[1])
            selected_bucket_counts[str(best[2].get("memory_bucket") or "")] += 1

        ordered_rows = [rows[index] for index in range(len(rows)) if index in selected_indices]
        self.last_context_selection_summary["long_term_memory"] = {
            "available_total": len(rows),
            "selected_total": len(ordered_rows),
            "available_bucket_counts": dict(sorted(available_bucket_counts.items())),
            "selected_bucket_counts": dict(sorted(selected_bucket_counts.items())),
            "excluded_bucket_counts": dict(sorted(excluded_bucket_counts.items())),
            "selected_rules": [str(row.get("rule") or "")[:120] for row in ordered_rows],
        }
        return [
            self._compact_memory_record(
                row,
                max_ignore_when=max_ignore_when,
            )
            for row in ordered_rows
        ]
