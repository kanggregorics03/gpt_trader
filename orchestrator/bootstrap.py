
import hashlib
import threading
import time
from datetime import UTC, datetime
from typing import Any

from ai.decision_engine import DecisionResult
from ai.llm_provider import resolve_api_key, resolve_decision_models
from core.models import (
    TradeIntent,
)
from utils.logger import log

from orchestrator.bootstrap_config import RuntimeConfig, StartupConfigurationError
from orchestrator.bootstrap_dependencies import BootstrapDependencyBuilder


class TradingBootstrapMixin:
    def __init__(self):
        log.info("Initializing Trading Orchestrator...")
        dependencies = BootstrapDependencyBuilder(self).build()
        self.runtime_config = dependencies.runtime_config
        self.api_key = self.runtime_config.api_key
        self.api_secret = self.runtime_config.api_secret
        self.trading_mode = dependencies.trading_mode
        self.ai_provider = self.runtime_config.ai_provider
        self.ai_api_key = self.runtime_config.ai_api_key
        self.ai_base_url = self.runtime_config.ai_base_url
        self.ai_default_model = self.runtime_config.ai_default_model
        self.ai_trigger_model = self.runtime_config.ai_trigger_model
        self.db_path = self.runtime_config.db_path
        self.state_store = dependencies.state_store
        self.risk_engine = dependencies.risk_engine
        self.rag_engine = dependencies.rag_engine
        self.symbols = dependencies.symbols
        self.symbol = dependencies.symbol
        self.private_exchange = dependencies.private_exchange
        self.public_exchange = dependencies.public_exchange
        self.public_feed = dependencies.public_feed
        self.private_account_feed = dependencies.private_account_feed
        self.account_reader = dependencies.account_reader
        self.market_collector = dependencies.market_collector
        self.execution_engine = dependencies.execution_engine
        self.execution_policy_engine = dependencies.execution_policy_engine
        self.alpha_policy_engine = dependencies.alpha_policy_engine
        self.position_policy_engine = dependencies.position_policy_engine
        self.safety_policy_engine = dependencies.safety_policy_engine
        self.ai_engine = dependencies.ai_engine
        self.alert_manager = dependencies.alert_manager
        self.snapshot_service = dependencies.snapshot_service
        self._initialize_runtime_state()

    def _validate_ai_configuration(self, runtime_config: RuntimeConfig | None = None):
        if runtime_config is not None:
            config = runtime_config
        elif hasattr(self, "runtime_config"):
            config = self.runtime_config
        else:
            config = RuntimeConfig(
                api_key=getattr(self, "api_key", None),
                api_secret=getattr(self, "api_secret", None),
                trading_mode_env=None,
                trading_testnet_env=None,
                ai_provider=getattr(self, "ai_provider", None),
                ai_api_key=getattr(self, "ai_api_key", None),
                ai_base_url=getattr(self, "ai_base_url", None),
                ai_default_model=getattr(self, "ai_default_model", None),
                ai_trigger_model=getattr(self, "ai_trigger_model", None),
                db_path=getattr(self, "db_path", "data/gpt_trader.db"),
            )
        resolve_api_key(
            config.ai_provider,
            explicit_api_key=config.ai_api_key,
        )
        resolve_decision_models(
            config.ai_provider,
            default_model=config.ai_default_model,
            trigger_model=config.ai_trigger_model,
        )

    def _build_private_account_ws_config(self, risk_engine=None):
        engine = risk_engine or self.risk_engine
        configured = engine.policy.get("private_account_ws", {})
        config = dict(self.DEFAULT_PRIVATE_ACCOUNT_WS)
        config.update(configured or {})
        config["reconcile_ttl_ms"] = max(
            5000,
            int(config.get("reconcile_ttl_ms", self.DEFAULT_PRIVATE_ACCOUNT_WS["reconcile_ttl_ms"]) or 0),
        )
        config["ws_stale_after_seconds"] = max(
            5,
            int(
                config.get(
                    "ws_stale_after_seconds",
                    self.DEFAULT_PRIVATE_ACCOUNT_WS["ws_stale_after_seconds"],
                )
                or 0
            ),
        )
        config["ws_recover_stability_seconds"] = max(
            1,
            int(
                config.get(
                    "ws_recover_stability_seconds",
                    self.DEFAULT_PRIVATE_ACCOUNT_WS["ws_recover_stability_seconds"],
                )
                or 0
            ),
        )
        config["execution_cache_size"] = max(
            20,
            int(
                config.get(
                    "execution_cache_size",
                    self.DEFAULT_PRIVATE_ACCOUNT_WS["execution_cache_size"],
                )
                or 0
            ),
        )
        config["forced_reconcile_on_recover"] = bool(config.get("forced_reconcile_on_recover", True))
        return config

    def _build_public_market_ws_config(self, risk_engine=None):
        engine = risk_engine or self.risk_engine
        configured = engine.policy.get("public_market_ws", {})
        config = dict(self.DEFAULT_PUBLIC_MARKET_WS)
        config.update(configured or {})
        config["orderbook_depth"] = max(
            1,
            int(config.get("orderbook_depth", self.DEFAULT_PUBLIC_MARKET_WS["orderbook_depth"]) or 0),
        )
        config["stream_ticker"] = bool(config.get("stream_ticker", True))
        config["stream_orderbook"] = bool(config.get("stream_orderbook", True))
        raw_timeframes = config.get("stream_timeframes", self.DEFAULT_PUBLIC_MARKET_WS["stream_timeframes"])
        config["timeframes"] = list(raw_timeframes or [])
        config.pop("stream_timeframes", None)
        config["max_kline_rows"] = max(
            20,
            int(config.get("max_kline_rows", self.DEFAULT_PUBLIC_MARKET_WS["max_kline_rows"]) or 0),
        )
        return config

    def _build_market_data_cache_config(self, risk_engine=None):
        engine = risk_engine or self.risk_engine
        configured = engine.policy.get("market_data_cache", {})
        config = dict(self.DEFAULT_MARKET_DATA_CACHE)
        config.update(configured or {})
        key_mapping = {
            "ticker_ttl_ms": "ticker",
            "order_book_ttl_ms": "order_book",
            "funding_ttl_ms": "funding_rate",
            "open_interest_ttl_ms": "open_interest",
        }
        return {
            collector_key: max(250, int(config.get(policy_key, default_value) or default_value))
            for policy_key, default_value in self.DEFAULT_MARKET_DATA_CACHE.items()
            for collector_key in [key_mapping[policy_key]]
        }

    def _estimate_realized_pnl_pct(self, intent, account_state):
        if intent.decision not in ("REDUCE", "EXIT"):
            return None

        target_symbols = {order.symbol for order in intent.orders}
        if not target_symbols:
            target_symbols = {position.symbol for position in account_state.positions}

        matched_positions = [
            (
                position.account_unrealized_pnl_pct
                if getattr(position, "account_unrealized_pnl_pct", None) is not None
                else position.unrealized_pnl_pct
            )
            for position in account_state.positions
            if position.symbol in target_symbols
        ]
        if not matched_positions:
            return None
        return sum(matched_positions) / len(matched_positions)

    def _calculate_balance_delta_pct(self, before_balance: float, after_balance: float):
        if before_balance <= 0:
            return None
        return ((after_balance - before_balance) / before_balance) * 100.0

    def _enrich_intent_metadata(self, intent, input_hash: str, loop_mode: str, risk_mode: str, market_snapshots):
        rationale_seed = ""
        if intent.rationale_items:
            rationale_seed = str(intent.rationale_items[0].summary or "").strip()
        if not rationale_seed and intent.rationale:
            rationale_seed = str(intent.rationale[0] or "").strip()
        invalidation_reason = str(getattr(intent, "invalidation_reason", None) or "").strip()
        if not invalidation_reason:
            for item in intent.rationale_items or []:
                failure_mode = str(getattr(item, "failure_mode", "") or "").strip()
                if failure_mode:
                    invalidation_reason = failure_mode
                    break
        thesis_summary = str(getattr(intent, "thesis_summary", None) or rationale_seed or "").strip() or None
        thesis_basis = "|".join(
            [
                str(input_hash or "").strip(),
                str(intent.decision or "").strip(),
                str(thesis_summary or ""),
                str(invalidation_reason or ""),
            ]
        )
        thesis_id = str(getattr(intent, "thesis_id", None) or "").strip()
        if not thesis_id and thesis_basis.replace("|", "").strip():
            thesis_id = hashlib.sha1(thesis_basis.encode("utf-8")).hexdigest()[:12]
        management_template_id = str(getattr(intent, "management_template_id", None) or "").strip()
        if not management_template_id:
            management_template_id = self._derive_management_template_id(
                intent=intent,
                loop_mode=loop_mode,
                risk_mode=risk_mode,
                market_snapshots=market_snapshots,
            )
        exit_reason = str(getattr(intent, "exit_reason", None) or "").strip()
        if not exit_reason:
            exit_reason = self._derive_intent_exit_reason(intent)
        return intent.model_copy(
            update={
                "thesis_id": thesis_id or None,
                "thesis_summary": thesis_summary,
                "invalidation_reason": invalidation_reason or None,
                "management_template_id": management_template_id or None,
                "exit_reason": exit_reason or None,
            }
        )

    def _derive_management_template_id(self, intent, loop_mode: str, risk_mode: str, market_snapshots):
        if intent.decision in ("REDUCE", "EXIT"):
            return f"{loop_mode}_risk_reduction"
        if intent.decision == "CANCEL":
            return f"{loop_mode}_working_order_cleanup"
        if intent.decision == "REPLACE_ENTRY":
            return f"{loop_mode}_working_order_reprice"
        strong_trigger = any(
            bool(getattr(snapshot, "is_opportunity_trigger", False))
            for snapshot in (market_snapshots or {}).values()
        )
        if loop_mode == "1h":
            return "entry_trigger_followthrough" if strong_trigger else "entry_swing_base"
        if loop_mode == "15m":
            return "position_manage_adaptive"
        if loop_mode == "5m":
            return "execution_safety_fast"
        return f"{loop_mode}_{risk_mode}"

    def _derive_intent_exit_reason(self, intent):
        if intent.decision == "EXIT":
            risk_text = " ".join(str(note or "") for note in (intent.risk_notes or [])).lower()
            rationale_text = " ".join(str(row or "") for row in (intent.rationale or [])).lower()
            merged = f"{risk_text} {rationale_text}"
            if "cooldown" in merged or "kill switch" in merged:
                return "risk_control"
            if "spread" in merged or "liquidity" in merged:
                return "liquidity_exit"
            if "invalid" in merged or "thesis" in merged or "failure" in merged:
                return "thesis_break"
            return "full_exit"
        if intent.decision == "REDUCE":
            return "scale_out"
        if intent.decision == "CANCEL":
            return "order_cleanup"
        if intent.decision == "REPLACE_ENTRY":
            return "order_reprice"
        return ""

    def _build_policy_decision_result(self, decision_payload: dict, input_hash: str, model_name: str):
        metadata = dict(decision_payload.get("metadata") or {})
        return DecisionResult(
            intent=TradeIntent(
                decision=decision_payload.get("decision") or "HOLD",
                orders=decision_payload.get("orders") or [],
                rationale=decision_payload.get("rationale") or [f"Deterministic {model_name}."],
                rationale_items=[],
                risk_notes=decision_payload.get("risk_notes") or [],
                confidence=float(decision_payload.get("confidence") or 0.8),
                exit_reason=decision_payload.get("exit_reason"),
            ),
            prompt="",
            raw_response="",
            model_name=model_name,
            input_hash=input_hash,
            cache_hit=False,
            usage_metadata={"skipped": True, "policy_source": metadata.get("policy_source") or model_name, **metadata},
        )

    def _resolve_bundle_policy_config(self, bundle_summary: dict, domain: str) -> dict:
        policy_key_field = {
            "alpha_1h": "alpha_policy_key",
            "position_15m": "position_policy_key",
            "safety_5m": "safety_policy_key",
            "execution_5m": "execution_policy_key",
        }.get(domain, "")
        policy_key = str((bundle_summary or {}).get(policy_key_field) or "").strip()
        if not policy_key or not hasattr(self.state_store, "get_policy_registry_entry"):
            return {}
        entry = self.state_store.get_policy_registry_entry(policy_key)
        return entry.get("config") or {}

    def _prepare_decision_market_snapshots(
        self,
        account_state,
        market_snapshots,
        loop_mode: str,
        regime_key: str = "",
        alpha_policy_override: dict | None = None,
    ):
        decision_market_snapshots = dict(market_snapshots or {})
        alpha_summary = {
            "applied": False,
            "allowed_symbols": sorted(decision_market_snapshots.keys()),
            "blocked_symbols": {},
        }
        if (
            loop_mode != "1h"
            or self._has_live_positions(account_state)
            or self._has_live_open_orders(account_state)
            or not decision_market_snapshots
        ):
            return decision_market_snapshots, alpha_summary
        filtered_snapshots, blocked_symbols = self.alpha_policy_engine.filter_market_snapshots(
            decision_market_snapshots,
            policy_override=alpha_policy_override,
            regime_key=regime_key,
        )
        alpha_summary = {
            "applied": bool(alpha_policy_override or self.alpha_policy_engine.get_active_policy(regime_key=regime_key)),
            "allowed_symbols": sorted(filtered_snapshots.keys()),
            "blocked_symbols": dict(blocked_symbols or {}),
        }
        return filtered_snapshots, alpha_summary

    def _extract_entry_symbols(self, intent) -> list[str]:
        if getattr(intent, "decision", "") == "REPLACE_ENTRY":
            return []
        entry_symbols = []
        for order in getattr(intent, "orders", []) or []:
            if bool(getattr(order, "reduce_only", False)) or getattr(order, "action", "") == "CANCEL":
                continue
            symbol = str(getattr(order, "symbol", "") or "").strip()
            if symbol and symbol not in entry_symbols:
                entry_symbols.append(symbol)
        return entry_symbols

    def _record_shadow_policy_decisions(
        self,
        audit_log_id: int,
        loop_mode: str,
        live_intent,
        account_state,
        market_snapshots,
        risk_mode: str,
        symbol_journal_entries: list[dict],
        regime_key: str = "",
    ):
        if not hasattr(self.state_store, "get_candidate_policy_versions"):
            return
        trade_case_by_symbol = {
            str((entry or {}).get("symbol") or ""): str((entry or {}).get("trade_case_id") or "")
            for entry in (symbol_journal_entries or [])
            if (entry or {}).get("symbol")
        }
        shadow_entries = []
        if loop_mode == "15m":
            candidates = self.state_store.get_candidate_policy_versions(
                domain="position_15m",
                regime_key=regime_key or None,
                statuses=["candidate", "shadow"],
                limit=5,
            )
            if not candidates and regime_key:
                candidates = self.state_store.get_candidate_policy_versions(
                    domain="position_15m",
                    statuses=["candidate", "shadow"],
                    limit=5,
                )
            live_summary = {
                "decision": live_intent.decision,
                "orders": [order.model_dump() if hasattr(order, "model_dump") else dict(order) for order in (live_intent.orders or [])],
                "exit_reason": getattr(live_intent, "exit_reason", None),
            }
            for candidate in candidates:
                candidate_result = self.position_policy_engine.evaluate(
                    account_state=account_state,
                    market_snapshots=market_snapshots,
                    risk_mode=risk_mode,
                    loop_mode=loop_mode,
                    regime_key=regime_key,
                    policy_override=candidate.get("config") or {},
                )
                if candidate_result.get("escalate_to_ai"):
                    continue
                shadow_payload = candidate_result.get("decision_result") or {}
                divergence_type = self._classify_shadow_divergence(live_summary, shadow_payload)
                if divergence_type == "none":
                    continue
                symbol = ""
                shadow_orders = shadow_payload.get("orders") or []
                if shadow_orders:
                    symbol = str(getattr(shadow_orders[0], "symbol", None) or shadow_orders[0].get("symbol") or "")
                if not symbol and live_intent.orders:
                    symbol = str(getattr(live_intent.orders[0], "symbol", None) or "")
                shadow_entries.append(
                    {
                        "audit_log_id": audit_log_id,
                        "candidate_policy_id": candidate.get("id"),
                        "domain": "position_15m",
                        "symbol": symbol or None,
                        "loop_mode": loop_mode,
                        "trade_case_id": trade_case_by_symbol.get(symbol or "", None),
                        "divergence_type": divergence_type,
                        "payload": {
                            "live_intent": live_summary,
                            "shadow_intent": {
                                "decision": shadow_payload.get("decision"),
                                "orders": [
                                    order.model_dump() if hasattr(order, "model_dump") else dict(order)
                                    for order in shadow_orders
                                ],
                                "exit_reason": shadow_payload.get("exit_reason"),
                                "rationale": shadow_payload.get("rationale") or [],
                            },
                            "candidate_policy": candidate,
                        },
                    }
                )
        if loop_mode == "5m":
            candidates = self.state_store.get_candidate_policy_versions(
                domain="safety_5m",
                regime_key=regime_key or None,
                statuses=["candidate", "shadow"],
                limit=5,
            )
            if not candidates and regime_key:
                candidates = self.state_store.get_candidate_policy_versions(
                    domain="safety_5m",
                    statuses=["candidate", "shadow"],
                    limit=5,
                )
            live_summary = {
                "decision": live_intent.decision,
                "orders": [order.model_dump() if hasattr(order, "model_dump") else dict(order) for order in (live_intent.orders or [])],
                "exit_reason": getattr(live_intent, "exit_reason", None),
            }
            for candidate in candidates:
                candidate_result = self.safety_policy_engine.evaluate(
                    account_state=account_state,
                    market_snapshots=market_snapshots,
                    risk_mode=risk_mode,
                    loop_mode=loop_mode,
                    regime_key=regime_key,
                    policy_override=candidate.get("config") or {},
                )
                if candidate_result.get("escalate_to_ai"):
                    continue
                shadow_payload = candidate_result.get("decision_result") or {}
                divergence_type = self._classify_safety_shadow_divergence(live_summary, shadow_payload)
                if divergence_type == "none":
                    continue
                symbol = ""
                shadow_orders = shadow_payload.get("orders") or []
                if shadow_orders:
                    symbol = str(getattr(shadow_orders[0], "symbol", None) or shadow_orders[0].get("symbol") or "")
                if not symbol and live_intent.orders:
                    symbol = str(getattr(live_intent.orders[0], "symbol", None) or "")
                shadow_entries.append(
                    {
                        "audit_log_id": audit_log_id,
                        "candidate_policy_id": candidate.get("id"),
                        "domain": "safety_5m",
                        "symbol": symbol or None,
                        "loop_mode": loop_mode,
                        "trade_case_id": trade_case_by_symbol.get(symbol or "", None),
                        "divergence_type": divergence_type,
                        "payload": {
                            "live_intent": live_summary,
                            "shadow_intent": {
                                "decision": shadow_payload.get("decision"),
                                "orders": [
                                    order.model_dump() if hasattr(order, "model_dump") else dict(order)
                                    for order in shadow_orders
                                ],
                                "exit_reason": shadow_payload.get("exit_reason"),
                                "rationale": shadow_payload.get("rationale") or [],
                            },
                            "candidate_policy": candidate,
                        },
                    }
                )
        if loop_mode == "1h":
            candidates = self.state_store.get_candidate_policy_versions(
                domain="alpha_1h",
                regime_key=regime_key or None,
                statuses=["candidate", "shadow"],
                limit=5,
            )
            if not candidates and regime_key:
                candidates = self.state_store.get_candidate_policy_versions(
                    domain="alpha_1h",
                    statuses=["candidate", "shadow"],
                    limit=5,
                )
            entry_symbols = self._extract_entry_symbols(live_intent)
            live_orders = [
                order.model_dump() if hasattr(order, "model_dump") else dict(order)
                for order in (live_intent.orders or [])
            ]
            for candidate in candidates:
                policy_config = candidate.get("config") or {}
                for symbol in entry_symbols:
                    snapshot = (market_snapshots or {}).get(symbol)
                    blocked, reasons = self.alpha_policy_engine.would_block_symbol(snapshot, policy_config)
                    if not blocked:
                        continue
                    shadow_entries.append(
                        {
                            "audit_log_id": audit_log_id,
                            "candidate_policy_id": candidate.get("id"),
                            "domain": "alpha_1h",
                            "symbol": symbol,
                            "loop_mode": loop_mode,
                            "trade_case_id": trade_case_by_symbol.get(symbol),
                            "divergence_type": "entry_block",
                            "payload": {
                                "live_intent": {
                                    "decision": live_intent.decision,
                                    "orders": live_orders,
                                    "exit_reason": getattr(live_intent, "exit_reason", None),
                                },
                                "shadow_intent": {
                                    "decision": "HOLD",
                                    "orders": [],
                                    "blocked_reasons": reasons,
                                    "rationale": [
                                        f"{symbol} blocked by alpha policy candidate: {', '.join(reasons)}"
                                    ],
                                },
                                "candidate_policy": candidate,
                            },
                        }
                    )
        if shadow_entries and hasattr(self.state_store, "save_shadow_decisions"):
            self.state_store.save_shadow_decisions(shadow_entries)

    def _classify_shadow_divergence(self, live_summary: dict, shadow_payload: dict) -> str:
        live_decision = str(live_summary.get("decision") or "")
        shadow_decision = str(shadow_payload.get("decision") or "")
        if live_decision != shadow_decision:
            return "decision"
        live_orders = live_summary.get("orders") or []
        shadow_orders = shadow_payload.get("orders") or []
        if len(live_orders) != len(shadow_orders):
            return "order_count"
        if live_orders and shadow_orders:
            live_qty = round(sum(float(order.get("quantity") or 0.0) for order in live_orders), 8)
            shadow_qty = round(
                sum(
                    float(getattr(order, "quantity", None) or (order.get("quantity") if isinstance(order, dict) else 0.0) or 0.0)
                    for order in shadow_orders
                ),
                8,
            )
            if live_qty != shadow_qty:
                return "size"
        return "none"

    def _classify_safety_shadow_divergence(self, live_summary: dict, shadow_payload: dict) -> str:
        live_decision = str(live_summary.get("decision") or "")
        shadow_decision = str(shadow_payload.get("decision") or "")
        if live_decision == shadow_decision:
            return self._classify_shadow_divergence(live_summary, shadow_payload)
        conservative_rank = {"HOLD": 0, "CANCEL": 1, "REPLACE_ENTRY": 1, "REDUCE": 2, "EXIT": 3}
        live_rank = conservative_rank.get(live_decision, 0)
        shadow_rank = conservative_rank.get(shadow_decision, 0)
        if shadow_rank > live_rank:
            if shadow_decision == "EXIT":
                return "risk_exit"
        return "decision"

    def _build_context_budget(self):
        configured = self.risk_engine.policy.get("context_budget", {})
        budget = dict(self.DEFAULT_CONTEXT_BUDGET)
        budget.update(configured or {})
        budget["journal_fetch_limit"] = max(6, int(budget.get("journal_fetch_limit", 24) or 24))
        budget["journal_anchor_limit"] = max(0, int(budget.get("journal_anchor_limit", 24) or 24))
        budget["journal_keep_limit"] = max(3, int(budget.get("journal_keep_limit", 6) or 6))
        budget["memory_keep_limit"] = max(3, int(budget.get("memory_keep_limit", 8) or 8))
        budget["similar_case_keep_limit"] = max(1, int(budget.get("similar_case_keep_limit", 5) or 5))
        budget["similar_case_normal_keep_limit"] = max(
            1,
            int(budget.get("similar_case_normal_keep_limit", 2) or 2),
        )
        budget["max_rationale_items_per_journal"] = max(
            1,
            int(budget.get("max_rationale_items_per_journal", 2) or 2),
        )
        budget["max_evidence_per_rationale_item"] = max(
            1,
            int(budget.get("max_evidence_per_rationale_item", 2) or 2),
        )
        budget["max_ignore_when_per_memory"] = max(1, int(budget.get("max_ignore_when_per_memory", 2) or 2))
        return budget

    def _build_replay_batch_config(self):
        configured = self.risk_engine.policy.get("replay_batch", {})
        config = dict(self.DEFAULT_REPLAY_BATCH)
        config.update(configured or {})
        config["enabled"] = bool(config.get("enabled", True))
        config["window_hours"] = max(1, int(config.get("window_hours", 24) or 24))
        config["limit"] = max(1, int(config.get("limit", 30) or 30))
        config["max_limit"] = max(config["limit"], int(config.get("max_limit", config["limit"] * 2) or (config["limit"] * 2)))
        config["coverage_target"] = min(1.0, max(0.1, float(config.get("coverage_target", 0.5) or 0.5)))
        config["hold_quota"] = max(0, min(int(config.get("hold_quota", 8) or 8), config["max_limit"]))
        config["candidate_pool_limit"] = max(
            config["max_limit"],
            int(config.get("candidate_pool_limit", max(120, config["max_limit"] * 4)) or max(120, config["max_limit"] * 4)),
        )
        config["save"] = bool(config.get("save", True))
        return config

    def _build_execution_guards(self):
        configured = self.risk_engine.policy.get("execution_guards", {})
        guards = dict(self.DEFAULT_EXECUTION_GUARDS)
        guards.update(configured or {})
        guards["loop_lock_ttl_seconds"] = max(30, int(guards.get("loop_lock_ttl_seconds", 240) or 240))
        guards["decision_cooldown_minutes"] = max(1, int(guards.get("decision_cooldown_minutes", 10) or 10))
        guards["entry_mutex_ttl_seconds"] = max(30, int(guards.get("entry_mutex_ttl_seconds", 180) or 180))
        guards["unknown_order_block_minutes"] = max(
            1,
            int(guards.get("unknown_order_block_minutes", 30) or 30),
        )
        guards["recovery_sweep_minutes"] = max(1, int(guards.get("recovery_sweep_minutes", 5) or 5))
        guards["symbol_reentry_close_cooldown_minutes"] = max(
            0,
            int(guards.get("symbol_reentry_close_cooldown_minutes", 10) or 0),
        )
        guards["symbol_reentry_loss_cooldown_minutes"] = max(
            guards["symbol_reentry_close_cooldown_minutes"],
            int(guards.get("symbol_reentry_loss_cooldown_minutes", 30) or 0),
        )
        guards["symbol_reentry_loss_threshold_pct"] = min(
            0.0,
            float(guards.get("symbol_reentry_loss_threshold_pct", 0.0) or 0.0),
        )
        return guards

    def _build_adaptive_loop_config(self):
        intervals = self.risk_engine.policy.get("intervals", {})
        configured = self.risk_engine.policy.get("adaptive_loop", {})
        config = dict(self.DEFAULT_ADAPTIVE_LOOP)
        config["base_loop_minutes"] = int(intervals.get("strategy_loop_minutes", config["base_loop_minutes"]) or 60)
        config["position_loop_minutes"] = int(intervals.get("position_loop_minutes", config["position_loop_minutes"]) or 15)
        config["trigger_loop_minutes"] = int(intervals.get("trigger_loop_minutes", config["trigger_loop_minutes"]) or 5)
        config.update(configured or {})
        config["base_loop_minutes"] = max(5, int(config.get("base_loop_minutes", 60) or 60))
        config["position_loop_minutes"] = max(1, int(config.get("position_loop_minutes", 15) or 15))
        config["trigger_loop_minutes"] = max(1, int(config.get("trigger_loop_minutes", 5) or 5))
        config.pop("trigger_min_duration_minutes", None)
        config["trigger_clear_cycles"] = max(1, int(config.get("trigger_clear_cycles", 2) or 2))
        config["position_min_duration_minutes"] = max(
            config["position_loop_minutes"],
            int(config.get("position_min_duration_minutes", 30) or 30),
        )
        config["flat_clear_cycles"] = max(1, int(config.get("flat_clear_cycles", 4) or 4))
        config["recent_execution_window_minutes"] = max(
            1,
            int(config.get("recent_execution_window_minutes", 20) or 20),
        )
        config["recent_close_window_minutes"] = max(
            1,
            int(config.get("recent_close_window_minutes", 20) or 20),
        )
        config["poll_seconds"] = max(1, int(config.get("poll_seconds", 1) or 1))
        config["trade_case_snapshot_poll_seconds"] = max(
            1,
            int(config.get("trade_case_snapshot_poll_seconds", 5) or 5),
        )
        return config

    def _initialize_runtime_state(self):
        self.adaptive_loop_config = self._build_adaptive_loop_config()
        self.active_loop_mode = "1h"
        self.active_loop_mode_since = time.time()
        self.trigger_clear_streak = 0
        self.flat_clear_streak = 0
        self.last_execution_at = None
        self.last_close_at = None
        self.last_recovery_sweep_at = 0.0
        self.last_daily_batch_date = None
        self.daily_batch_running = False
        self.daily_batch_started_at = None
        self.daily_batch_finished_at = None
        self.daily_batch_last_status = None
        self._daily_batch_thread = None
        self._daily_batch_lock = threading.Lock()
        self.last_trade_case_snapshot_at = 0.0
        self.last_similar_case_matches = []
        self._learning_artifact_cache = {}
        self.last_context_selection_summary = self._build_empty_context_selection_summary()
        self._daily_kill_switch_active = None
        self._drawdown_mode_active = None
        self._cooldown_extension_signature = self.ALERT_STATE_UNSET
        self._public_ws_stale = None
        self._private_ws_stale = None
        self._learning_artifact_cache = {}
