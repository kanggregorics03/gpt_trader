from dataclasses import dataclass, field
from math import isclose
from typing import Any, Dict, List, Optional

import yaml

from core.models import DecisionRequest, EvidenceItem, TradeIntent


@dataclass
class IntentValidationResult:
    is_valid: bool
    issues: List[str] = field(default_factory=list)


class StructuredIntentValidator:
    _ROOT_PREFIXES = {
        "account_state",
        "market_context",
        "market_snapshot",
        "market_snapshots",
        "risk_state",
        "recent_journal_context",
        "long_term_memory",
        "similar_past_cases",
        "risk_profile",
        "policy_summary",
    }

    def validate(self, request: DecisionRequest, intent: TradeIntent) -> IntentValidationResult:
        issues: List[str] = []
        payload = request.model_dump(mode="json")
        self._hydrate_policy_summary(payload)

        if not intent.rationale_items:
            issues.append("structured_rationale_missing")
            return IntentValidationResult(is_valid=False, issues=issues)

        for index, item in enumerate(intent.rationale_items):
            if not item.evidence:
                issues.append(f"rationale_items[{index}].evidence_missing")
                continue

            numeric_evidence_found = False
            for evidence_index, evidence in enumerate(item.evidence):
                issue_prefix = f"rationale_items[{index}].evidence[{evidence_index}]"
                resolved = self._resolve_source_value(payload, request, evidence)
                if resolved is None:
                    issues.append(f"{issue_prefix}.source_missing:{evidence.source}")
                    continue

                if self._is_numeric_or_boolean_scalar(resolved):
                    numeric_evidence_found = True
                actual_value = resolved
                if evidence.operator != "note" and evidence.threshold is None:
                    issues.append(f"{issue_prefix}.threshold_required")

                if evidence.operator == "note" and evidence.threshold is not None:
                    issues.append(f"{issue_prefix}.threshold_not_allowed_for_note")

                if not self._values_match(actual_value, evidence.observed_value):
                    issues.append(
                        f"{issue_prefix}.observed_value_mismatch:"
                        f"expected~={actual_value},got={evidence.observed_value}"
                    )
                elif not self._operator_matches(payload, actual_value, evidence):
                    issues.append(
                        f"{issue_prefix}.operator_condition_failed:"
                        f"actual={actual_value},operator={evidence.operator},threshold={evidence.threshold}"
                    )

            if not numeric_evidence_found:
                issues.append(f"rationale_items[{index}].numeric_evidence_missing")

        issues.extend(self._validate_action_consistency(intent))
        return IntentValidationResult(is_valid=not issues, issues=issues)

    def _hydrate_policy_summary(self, payload: Dict[str, Any]) -> None:
        raw_policy_summary = payload.get("policy_summary")
        if not isinstance(raw_policy_summary, str):
            return
        try:
            parsed = yaml.safe_load(raw_policy_summary)
        except yaml.YAMLError:
            return
        if isinstance(parsed, dict):
            payload["policy_summary"] = parsed

    def _resolve_source_value(
        self,
        payload: Dict[str, Any],
        request: DecisionRequest,
        evidence: EvidenceItem,
    ) -> Optional[float | str | bool]:
        candidate_paths = [evidence.source]
        candidate_paths.extend(self._market_context_alias_paths(request, evidence))
        candidate_paths.extend(self._request_alias_paths(evidence))
        if not self._has_root_prefix(evidence.source):
            if evidence.symbol and evidence.symbol in request.market_snapshots:
                candidate_paths.append(f"market_snapshots.{evidence.symbol}.{evidence.source}")
            if request.market_snapshot is not None and (
                evidence.symbol is None or evidence.symbol == request.market_snapshot.symbol
            ):
                candidate_paths.append(f"market_snapshot.{evidence.source}")

        for path in candidate_paths:
            value = self._get_path_value(payload, path)
            scalar_value = self._coerce_scalar_value(value)
            if scalar_value is not None:
                return scalar_value
        return None

    def _request_alias_paths(self, evidence: EvidenceItem) -> List[str]:
        if str(evidence.source or "") == "guarded_attention_active":
            return ["is_guarded_attention"]
        return []

    def _market_context_alias_paths(
        self,
        request: DecisionRequest,
        evidence: EvidenceItem,
    ) -> List[str]:
        source = str(evidence.source or "")
        if not source.startswith("market_context."):
            return []

        alias_suffix = source[len("market_context.") :]
        candidate_paths: List[str] = []
        symbol = str(evidence.symbol or "")

        if alias_suffix:
            candidate_paths.append(f"market_snapshots.{alias_suffix}")
            if request.market_snapshot is not None:
                snapshot_symbol = str(request.market_snapshot.symbol or "")
                if alias_suffix.startswith(f"{snapshot_symbol}."):
                    stripped = alias_suffix[len(snapshot_symbol) + 1 :]
                    if stripped:
                        candidate_paths.append(f"market_snapshot.{stripped}")
                else:
                    candidate_paths.append(f"market_snapshot.{alias_suffix}")

        if symbol and symbol in request.market_snapshots and not alias_suffix.startswith(f"{symbol}."):
            candidate_paths.append(f"market_snapshots.{symbol}.{alias_suffix}")
        return candidate_paths

    def _has_root_prefix(self, path: str) -> bool:
        head = path.split(".", 1)[0]
        return head in self._ROOT_PREFIXES

    def _get_path_value(self, payload: Dict[str, Any], path: str) -> Any:
        current: Any = payload
        for part in path.split("."):
            if isinstance(current, dict):
                if part not in current:
                    return None
                current = current[part]
                continue
            if isinstance(current, list):
                try:
                    current = current[int(part)]
                except (ValueError, IndexError):
                    return None
                continue
            return None
        return current

    def _coerce_scalar_value(self, value: Any) -> Optional[float | str | bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
        return None

    def _is_numeric_scalar(self, value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _is_numeric_or_boolean_scalar(self, value: Any) -> bool:
        return isinstance(value, bool) or self._is_numeric_scalar(value)

    def _values_match(self, actual_value: float | str | bool, observed_value: float | str | bool) -> bool:
        if self._is_numeric_scalar(actual_value) and self._is_numeric_scalar(observed_value):
            actual_float = float(actual_value)
            observed_float = float(observed_value)
            tolerance = max(0.01, abs(actual_float) * 0.02)
            return isclose(actual_float, observed_float, abs_tol=tolerance, rel_tol=0.02)
        if isinstance(actual_value, bool) or isinstance(observed_value, bool):
            return bool(actual_value) is bool(observed_value)
        if actual_value is None or observed_value is None:
            return actual_value is observed_value
        return str(actual_value).strip().lower() == str(observed_value).strip().lower()

    def _operator_matches(self, payload: Dict[str, Any], actual_value: float | str | bool, evidence: EvidenceItem) -> bool:
        threshold = evidence.threshold
        operator = evidence.operator
        if operator == "note":
            return True
        if threshold is None:
            return False
        if operator == "eq":
            return self._values_match(actual_value, threshold)
        if not self._is_numeric_scalar(actual_value) or not self._is_numeric_scalar(threshold):
            return False
        actual_float = float(actual_value)
        threshold_float = float(threshold)
        if operator == "gte":
            return actual_float >= threshold_float
        if operator == "gt":
            return actual_float > threshold_float
        if operator == "lte":
            return actual_float <= threshold_float
        if operator == "lt":
            return actual_float < threshold_float
        if operator == "cross_up":
            previous_value = self._resolve_previous_value(payload, evidence, actual_float)
            if previous_value is None:
                return actual_float > threshold_float
            return previous_value <= threshold_float and actual_float > threshold_float
        if operator == "cross_down":
            previous_value = self._resolve_previous_value(payload, evidence, actual_float)
            if previous_value is None:
                return actual_float < threshold_float
            return previous_value >= threshold_float and actual_float < threshold_float
        if operator == "delta_abs_gte":
            return abs(actual_float) >= abs(threshold_float)
        return False

    def _resolve_previous_value(
        self,
        payload: Dict[str, Any],
        evidence: EvidenceItem,
        actual_value: float,
    ) -> Optional[float]:
        source_parts = evidence.source.split(".")
        if not source_parts:
            return None
        metric_name = source_parts[-1]
        timeframe_payload = self._get_path_value(payload, ".".join(source_parts[:-1]))
        if not isinstance(timeframe_payload, dict):
            return None
        previous_values = timeframe_payload.get("previous_values")
        if isinstance(previous_values, dict):
            previous_value = previous_values.get(metric_name)
            if isinstance(previous_value, (int, float)) and not isinstance(previous_value, bool):
                return float(previous_value)
        if metric_name == "last_close":
            change_1bar_pct = timeframe_payload.get("change_1bar_pct")
            if not isinstance(change_1bar_pct, (int, float)) or change_1bar_pct == -100:
                return None
            return float(actual_value) / (1.0 + (float(change_1bar_pct) / 100.0))
        return None

    def _validate_action_consistency(self, intent: TradeIntent) -> List[str]:
        issues: List[str] = []
        if intent.decision not in ("BUY", "SELL"):
            return issues

        evidence_quality = self._estimate_evidence_quality(intent)
        action_keywords = {
            "BUY": ("buy", "long", "bullish"),
            "SELL": ("sell", "short", "bearish"),
        }
        implication_texts = [
            f"{item.summary} {item.trade_implication}".lower()
            for item in intent.rationale_items
        ]
        if implication_texts and not any(
            any(keyword in text for keyword in action_keywords[intent.decision])
            for text in implication_texts
        ):
            issues.append("trade_implication_action_mismatch")

        factor = float(evidence_quality.get("factor") or 1.0)
        if factor <= 0.7 and float(intent.confidence) >= 0.75:
            issues.append("confidence_exceeds_evidence_quality")
        if factor <= 0.6 and not [note for note in intent.risk_notes if str(note).strip()]:
            issues.append("risk_notes_missing_for_weak_evidence")
        return issues

    def _estimate_evidence_quality(self, intent: TradeIntent) -> Dict[str, float]:
        evidence_items = []
        unique_signals = set()
        failure_mode_count = 0
        for item in intent.rationale_items:
            if item.failure_mode:
                failure_mode_count += 1
            for evidence in item.evidence:
                evidence_items.append(evidence)
                unique_signals.add((evidence.symbol or "", evidence.signal, evidence.timeframe))

        factor = 1.0
        if intent.rationale_items:
            if len(unique_signals) < 2:
                factor = min(factor, 0.7)
            if len(evidence_items) < max(1, len(intent.orders)):
                factor = min(factor, 0.6)
            if failure_mode_count < len(intent.rationale_items):
                factor = min(factor, 0.85)
        return {
            "factor": factor,
            "unique_signal_count": float(len(unique_signals)),
            "evidence_count": float(len(evidence_items)),
        }
