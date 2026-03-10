import hashlib
import json
import math
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from loguru import logger


class StateStoreDecodingMixin:
    def _decode_audit_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        decoded = dict(row)
        defaults = {
            "account_state": {},
            "market_state": {},
            "market_vectors": {},
            "intent": {},
            "receipts": [],
            "risk_profile": {},
            "decision_request": {},
            "execution_summary": {},
        }
        for key, default in defaults.items():
            decoded[key] = self._safe_json_loads(decoded.get(key), default=default)
        return decoded

    def _format_memory_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        metrics = self._safe_json_loads(item.get("metrics"), default={})
        apply_when = metrics.get("apply_when") or {
            "regime": [metrics.get("regime")] if metrics.get("regime") else [],
            "volatility": [],
            "trend": [],
        }
        ignore_when = metrics.get("ignore_when") or [
            "No explicit ignore condition stored; treat as low-priority guidance."
        ]
        item["metrics"] = metrics
        item["category"] = metrics.get("category", "lesson")
        item["status"] = metrics.get("status", "rule")
        item["support"] = int(metrics.get("support") or 0)
        item["regime"] = metrics.get("regime", "general")
        item["apply_when"] = apply_when
        item["ignore_when"] = ignore_when
        item["evidence_journal_ids"] = metrics.get("evidence_journal_ids") or []
        item["confidence_score"] = float(metrics.get("confidence_score") or 0.0)
        item["validity_score"] = float(metrics.get("validity_score") or item["confidence_score"] or 0.0)
        item["decay_score"] = float(metrics.get("decay_score") or 0.0)
        item["freshness_score"] = float(metrics.get("freshness_score") or 0.0)
        item["lifecycle_status"] = metrics.get("lifecycle_status") or ("retired" if metrics.get("retired") else "active")
        item["retired"] = bool(metrics.get("retired"))
        item["retirement_reason"] = metrics.get("retirement_reason")
        item["canonical_key"] = metrics.get("canonical_key")
        item["merge_count"] = int(metrics.get("merge_count") or 0)
        item["revive_count"] = int(metrics.get("revive_count") or 0)
        item["trigger_context"] = str(metrics.get("trigger_context") or "")
        item["execution_context"] = str(metrics.get("execution_context") or "")
        item["trend_context"] = str(metrics.get("trend_context") or "")
        item["hold_quality"] = str(metrics.get("hold_quality") or "")
        return item

    def _format_journal_summary(self, row: Dict[str, Any]) -> str:
        decision = row.get("decision") or "UNKNOWN"
        rationale_value = row.get("rationale") or []
        if isinstance(rationale_value, list):
            rationale_text = " | ".join(str(item) for item in rationale_value if item)
        else:
            rationale_text = str(rationale_value).strip()
        rationale_json = row.get("rationale_json") or row.get("rationale_items") or []
        risk_reason = (row.get("risk_reason") or "").strip()
        estimated_pnl_pct = row.get("estimated_pnl_pct")
        ledger_net_realized_pnl = row.get("ledger_net_realized_pnl")
        ledger_fee_cost = row.get("ledger_fee_cost")
        risk_mode = row.get("risk_mode")
        risk_approved = row.get("risk_approved")
        trigger_symbols = row.get("trigger_symbols") or []
        risk_trigger_symbols = row.get("risk_trigger_symbols") or []
        evidence_quality = row.get("evidence_quality") or {}
        entry_size_cap = row.get("entry_size_cap") or {}
        evidence_parts = []
        for item in rationale_json[:2]:
            evidence = (item or {}).get("evidence") or []
            if not evidence:
                continue
            first = evidence[0]
            signal = first.get("signal") or "signal"
            timeframe = first.get("timeframe") or "context"
            observed = first.get("observed_value")
            operator = first.get("operator") or "note"
            threshold = first.get("threshold")
            if threshold is None:
                evidence_parts.append(f"{signal}@{timeframe}={observed}")
            else:
                evidence_parts.append(f"{signal}@{timeframe}={observed} {operator} {threshold}")

        parts = [f"{decision}:"]
        if rationale_text:
            parts.append(rationale_text.replace("\n", " | "))
        if evidence_parts:
            parts.append(f"evidence={' ; '.join(evidence_parts)}")
        if risk_reason:
            parts.append(f"risk={risk_reason}")
        if risk_mode:
            parts.append(f"mode={risk_mode}")
        if risk_approved is not None:
            parts.append(f"approved={bool(risk_approved)}")
        if estimated_pnl_pct is not None:
            parts.append(f"pnl={float(estimated_pnl_pct):.2f}%")
        if ledger_net_realized_pnl is not None:
            parts.append(f"ledger_net={float(ledger_net_realized_pnl):.4f}")
        if ledger_fee_cost is not None:
            parts.append(f"fees={float(ledger_fee_cost):.4f}")
        if evidence_quality:
            parts.append(f"eq={float(evidence_quality.get('factor') or 0.0):.2f}")
        if entry_size_cap:
            parts.append(
                "cap="
                f"{float(entry_size_cap.get('base_cap_pct') or 0.0):.1f}"
                "->"
                f"{float(entry_size_cap.get('effective_cap_pct') or 0.0):.1f}"
            )
        if trigger_symbols:
            parts.append(f"trigger={','.join(trigger_symbols)}")
        if risk_trigger_symbols:
            parts.append(f"risk_trigger={','.join(risk_trigger_symbols)}")
        case_summary = str(row.get("case_summary") or "").strip()
        if case_summary:
            parts.append(f"cases={case_summary}")
        return " ".join(parts)

    def _decode_journal_context_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        item["rationale_json"] = self._safe_json_loads(item.get("rationale_json"), default=[])
        item["market_state"] = self._safe_json_loads(item.get("market_state"), default={})
        item["execution_summary"] = self._safe_json_loads(item.get("execution_summary"), default={})
        item["trade_case_ids"] = self._safe_json_loads(item.get("trade_case_ids"), default=[])
        item["case_statuses"] = self._safe_json_loads(item.get("case_statuses"), default={})
        rationale_text = item.get("rationale") or ""
        item["rationale"] = [line for line in str(rationale_text).split("\n") if line]
        item["rationale_items"] = item["rationale_json"]
        item["journal_id"] = int(item.get("id") or 0)
        item["trigger_symbols"] = (item["market_state"].get("trigger_symbols") or [])
        item["risk_trigger_symbols"] = (item["market_state"].get("risk_trigger_symbols") or [])
        item["evidence_quality"] = item["execution_summary"].get("evidence_quality") or {}
        item["entry_size_cap"] = item["execution_summary"].get("entry_size_cap") or {}
        item["summary"] = self._format_journal_summary(item)
        return item

    def _decode_symbol_journal_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        item["evidence_signals"] = self._safe_json_loads(item.get("evidence_signals"), default=[])
        item["payload"] = self._safe_json_loads(item.get("payload"), default={})
        return item

    def _decode_trade_case_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        item["evidence_signals"] = self._safe_json_loads(item.get("evidence_signals"), default=[])
        item["trigger_reasons"] = self._safe_json_loads(item.get("trigger_reasons"), default=[])
        item["risk_trigger_reasons"] = self._safe_json_loads(item.get("risk_trigger_reasons"), default=[])
        item["feature_vector"] = self._safe_json_loads(item.get("feature_vector"), default=[])
        item["payload"] = self._safe_json_loads(item.get("payload"), default={})
        return item

    def _decode_trade_case_vector_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        feature_vector = item.get("feature_vector")
        if isinstance(feature_vector, list):
            item["feature_vector"] = feature_vector
        else:
            item["feature_vector"] = self._safe_json_loads(feature_vector, default=[])
        payload = item.get("payload")
        if isinstance(payload, dict):
            item["payload"] = payload
        else:
            item["payload"] = self._safe_json_loads(payload, default={})
        item["matched_band_count"] = int(item.get("matched_band_count") or 0)
        return item

    def _vector_norm(self, vector: Any) -> float:
        values = vector if isinstance(vector, list) else []
        total = 0.0
        for value in values:
            try:
                total += float(value) * float(value)
            except (TypeError, ValueError):
                continue
        return total ** 0.5

    def _coerce_vector_values(self, vector: Any) -> List[float]:
        values = vector if isinstance(vector, list) else []
        normalized: List[float] = []
        for value in values:
            try:
                normalized.append(float(value))
            except (TypeError, ValueError):
                normalized.append(0.0)
        return normalized

    def _build_vector_band_hashes(
        self,
        feature_vector: Any,
        band_count: int = 4,
        dims_per_band: int = 4,
    ) -> List[tuple[int, str]]:
        values = self._coerce_vector_values(feature_vector)
        if not values:
            return []
        norm = self._vector_norm(values) or 1.0
        unit_values = [value / norm for value in values]
        required_dims = band_count * dims_per_band
        if len(unit_values) < required_dims:
            unit_values = unit_values + ([0.0] * (required_dims - len(unit_values)))
        else:
            unit_values = unit_values[:required_dims]
        hashes: List[tuple[int, str]] = []
        for band_index in range(band_count):
            chunk = unit_values[band_index::band_count][:dims_per_band]
            if len(chunk) < dims_per_band:
                chunk = chunk + ([0.0] * (dims_per_band - len(chunk)))
            quantized = []
            for value in chunk:
                bucket = max(-6, min(6, int(round(value / 0.2))))
                quantized.append(str(bucket))
            digest = hashlib.sha1("|".join(quantized).encode("utf-8")).hexdigest()[:16]
            hashes.append((band_index, digest))
        return hashes

    def _build_trade_case_vector_band_entries(
        self,
        trade_case_id: str,
        symbol: str,
        feature_vector: Any,
        closed_at: Any = None,
    ) -> List[tuple[Any, ...]]:
        if not trade_case_id or not symbol:
            return []
        band_hashes = self._build_vector_band_hashes(feature_vector)
        return [
            (
                trade_case_id,
                symbol,
                band_index,
                band_hash,
                closed_at,
            )
            for band_index, band_hash in band_hashes
        ]

    def _safe_json_loads(self, value: Any, default: Any):
        if value in (None, ""):
            return default
        try:
            return json.loads(value)
        except Exception:
            return default
