import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

from ai.decision_engine import DecisionEngine, DecisionResult
from ai.llm_provider import normalize_provider_name, resolve_api_key, resolve_decision_models
from ai.rag_engine import RagEngine
from core.alpha_policy import AlphaPolicyEngine
from core.execution_policy import ExecutionPolicyEngine
from core.models import (
    DecisionRequest,
    EvidenceItem,
    RationaleItem,
    RiskStateSnapshot,
    TradeIntent,
    coerce_risk_state_snapshot,
)
from core.position_policy import PositionPolicyEngine
from core.risk_engine import RiskEngine
from core.safety_policy import SafetyPolicyEngine
from core.snapshot_service import SnapshotService
from db.state_store import StateStore
from exchange.account_reader import AccountReader
from exchange.bybit_factory import (
    create_private_account_feed,
    create_private_exchange,
    create_public_exchange,
    create_public_market_feed,
    normalize_trading_mode,
)
from exchange.collector import MarketCollector
from exchange.execution_engine import ExecutionEngine
from utils.alerter import AlertManager
from utils.logger import log

load_dotenv()

class TradingOrchestrator:
    ALERT_STATE_UNSET = object()
    DEFAULT_EXECUTION_GUARDS = {
        "loop_lock_ttl_seconds": 240,
        "decision_cooldown_minutes": 10,
        "entry_mutex_ttl_seconds": 180,
        "unknown_order_block_minutes": 30,
        "recovery_sweep_minutes": 5,
        "symbol_reentry_close_cooldown_minutes": 10,
        "symbol_reentry_loss_cooldown_minutes": 30,
        "symbol_reentry_loss_threshold_pct": 0.0,
    }
    DEFAULT_CONTEXT_BUDGET = {
        "journal_fetch_limit": 24,
        "journal_keep_limit": 6,
        "memory_keep_limit": 8,
        "similar_case_keep_limit": 5,
        "max_rationale_items_per_journal": 2,
        "max_evidence_per_rationale_item": 2,
        "max_ignore_when_per_memory": 2,
    }
    DEFAULT_REPLAY_BATCH = {
        "enabled": True,
        "window_hours": 24,
        "limit": 30,
        "save": True,
    }
    DEFAULT_ADAPTIVE_LOOP = {
        "base_loop_minutes": 60,
        "position_loop_minutes": 15,
        "trigger_loop_minutes": 5,
        "trigger_clear_cycles": 2,
        "position_min_duration_minutes": 30,
        "flat_clear_cycles": 4,
        "recent_execution_window_minutes": 20,
        "recent_close_window_minutes": 20,
        "poll_seconds": 1,
        "trade_case_snapshot_poll_seconds": 5,
    }
    DEFAULT_PRIVATE_ACCOUNT_WS = {
        "reconcile_ttl_ms": 600000,
        "ws_stale_after_seconds": 35,
        "ws_recover_stability_seconds": 12,
        "forced_reconcile_on_recover": True,
        "execution_cache_size": 200,
    }
    DEFAULT_PUBLIC_MARKET_WS = {
        "orderbook_depth": 10,
        "stream_ticker": True,
        "stream_orderbook": True,
        "stream_timeframes": [],
        "max_kline_rows": 120,
    }
    DEFAULT_MARKET_DATA_CACHE = {
        "ticker_ttl_ms": 5000,
        "order_book_ttl_ms": 5000,
        "funding_ttl_ms": 300000,
        "open_interest_ttl_ms": 30000,
    }
    LOOP_MODE_LABELS = {
        "1h": "Adaptive (1h)",
        "15m": "Adaptive (15m)",
        "5m": "Adaptive (5m)",
    }
    LOOP_MODE_PRIORITY = {"1h": 1, "15m": 2, "5m": 3}

    def __init__(self):
        log.info("Initializing Trading Orchestrator...")
        self.api_key = os.getenv("BYBIT_API_KEY")
        self.api_secret = os.getenv("BYBIT_API_SECRET")
        self.trading_mode = normalize_trading_mode(
            os.getenv("BYBIT_TRADING_MODE"),
            os.getenv("BYBIT_TESTNET"),
        )
        self.ai_provider = normalize_provider_name(os.getenv("AI_PROVIDER"))
        self.ai_api_key = os.getenv("AI_API_KEY")
        self.ai_base_url = os.getenv("AI_BASE_URL")
        self.ai_default_model = (
            os.getenv("AI_DEFAULT_MODEL")
            or os.getenv("GEMINI_DEFAULT_MODEL")
            or os.getenv("GEMINI_MODEL")
        )
        self.ai_trigger_model = os.getenv("AI_TRIGGER_MODEL") or os.getenv("GEMINI_TRIGGER_MODEL")
        self.db_path = os.getenv("DB_PATH", "data/gpt_trader.db")

        if not all([self.api_key, self.api_secret]):
            log.error("Missing required environment variables. Please check .env")
            exit(1)
        try:
            self._validate_ai_configuration()
        except ValueError as exc:
            log.error(str(exc))
            exit(1)

        self.state_store = StateStore(self.db_path)
        self.risk_engine = RiskEngine()
        self.rag_engine = RagEngine(self.db_path)
        self.symbols = self.risk_engine.get_trading_symbols()
        self.symbol = self.risk_engine.get_trading_symbol()

        self.private_exchange = create_private_exchange(
            self.api_key,
            self.api_secret,
            trading_mode=self.trading_mode,
        )
        self.public_exchange = create_public_exchange()
        log.info(
            f"Bybit exchange routing: market_data=mainnet, private_trading={self.trading_mode}"
        )
        public_market_ws_config = self._build_public_market_ws_config()
        self.public_feed = create_public_market_feed(
            self.symbols,
            **public_market_ws_config,
        )
        private_account_ws_config = self._build_private_account_ws_config()
        self.private_account_feed = create_private_account_feed(
            self.api_key,
            self.api_secret,
            trading_mode=self.trading_mode,
            **private_account_ws_config,
        )

        self.account_reader = AccountReader(
            self.private_exchange,
            trading_mode=self.trading_mode,
            private_feed=self.private_account_feed,
        )
        self.market_collector = MarketCollector(
            self.public_exchange,
            public_feed=self.public_feed,
            response_cache_ttls_ms=self._build_market_data_cache_config(),
        )
        self.execution_engine = ExecutionEngine(
            self.private_exchange,
            private_feed=self.private_account_feed,
        )
        self.execution_policy_engine = ExecutionPolicyEngine(
            policy=self.risk_engine.policy,
            state_store=self.state_store,
        )
        self.alpha_policy_engine = AlphaPolicyEngine(
            policy=self.risk_engine.policy,
            state_store=self.state_store,
        )
        self.position_policy_engine = PositionPolicyEngine(
            policy=self.risk_engine.policy,
            state_store=self.state_store,
        )
        self.safety_policy_engine = SafetyPolicyEngine(
            policy=self.risk_engine.policy,
            state_store=self.state_store,
        )
        
        self.ai_engine = DecisionEngine(
            api_key=self.ai_api_key,
            cache_store=self.state_store,
            provider=self.ai_provider,
            base_url=self.ai_base_url,
            default_model=self.ai_default_model,
            trigger_model=self.ai_trigger_model,
        )
        log.info(
            "AI routing: provider={} default={} trigger={}",
            getattr(self.ai_engine, "provider_name", "unknown"),
            self.ai_engine.flash_model,
            self.ai_engine.pro_model,
        )
        self.alert_manager = AlertManager()
        materialization_minutes = int(
            self.risk_engine.policy.get("adaptive_loop", {}).get(
                "materialization_minutes",
                self.risk_engine.policy.get("intervals", {}).get("trigger_loop_minutes", 5),
            ) or 5
        )
        self.snapshot_service = SnapshotService(
            state_store=self.state_store,
            account_reader=self.account_reader,
            market_collector=self.market_collector,
            risk_engine=self.risk_engine,
            symbols=self.symbols,
            materialization_minutes=materialization_minutes,
        )
        self._initialize_runtime_state()

    def _validate_ai_configuration(self):
        resolve_api_key(
            self.ai_provider,
            explicit_api_key=self.ai_api_key,
        )
        resolve_decision_models(
            self.ai_provider,
            default_model=self.ai_default_model,
            trigger_model=self.ai_trigger_model,
        )

    def _build_private_account_ws_config(self):
        configured = self.risk_engine.policy.get("private_account_ws", {})
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

    def _build_public_market_ws_config(self):
        configured = self.risk_engine.policy.get("public_market_ws", {})
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

    def _build_market_data_cache_config(self):
        configured = self.risk_engine.policy.get("market_data_cache", {})
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
            position.unrealized_pnl_pct
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
            if shadow_decision == "REDUCE":
                return "risk_reduce"
        return "decision"

    def _build_context_budget(self):
        configured = self.risk_engine.policy.get("context_budget", {})
        budget = dict(self.DEFAULT_CONTEXT_BUDGET)
        budget.update(configured or {})
        budget["journal_fetch_limit"] = max(6, int(budget.get("journal_fetch_limit", 24) or 24))
        budget["journal_keep_limit"] = max(3, int(budget.get("journal_keep_limit", 6) or 6))
        budget["memory_keep_limit"] = max(3, int(budget.get("memory_keep_limit", 8) or 8))
        budget["similar_case_keep_limit"] = max(1, int(budget.get("similar_case_keep_limit", 5) or 5))
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
        self.last_trade_case_snapshot_at = 0.0
        self.last_similar_case_matches = []
        self._daily_kill_switch_active = None
        self._drawdown_mode_active = None
        self._cooldown_extension_signature = self.ALERT_STATE_UNSET
        self._public_ws_stale = None
        self._private_ws_stale = None

    def _coerce_risk_state(self, risk_state) -> RiskStateSnapshot:
        return coerce_risk_state_snapshot(risk_state)

    def _serialize_risk_state(self, risk_state) -> dict:
        return self._coerce_risk_state(risk_state).model_dump(mode="json")

    def _primary_symbol(self) -> str:
        configured = [str(symbol).strip().upper() for symbol in (self.symbols or []) if str(symbol).strip()]
        if configured:
            return configured[0]
        return str(getattr(self, "symbol", "BTCUSDT") or "BTCUSDT").strip().upper()

    def _normalize_market_snapshots(self, market_snapshots):
        market_snapshots = dict(market_snapshots or {})
        if not market_snapshots:
            return {}
        primary_symbol = self._primary_symbol()
        if primary_symbol in market_snapshots:
            return {primary_symbol: market_snapshots[primary_symbol]}
        first_symbol = next(iter(market_snapshots.keys()))
        return {str(first_symbol): market_snapshots[first_symbol]}

    def _normalize_market_prices(self, market_prices, market_snapshots):
        market_prices = dict(market_prices or {})
        primary_symbol = self._primary_symbol()
        if primary_symbol in market_prices:
            return {primary_symbol: market_prices[primary_symbol]}
        if primary_symbol in (market_snapshots or {}):
            return {primary_symbol: market_snapshots[primary_symbol].current_price}
        if market_prices:
            first_symbol = next(iter(market_prices.keys()))
            return {str(first_symbol): market_prices[first_symbol]}
        if market_snapshots:
            first_symbol = next(iter(market_snapshots.keys()))
            return {str(first_symbol): market_snapshots[first_symbol].current_price}
        return {}

    def _get_symbol_reentry_guard(self, symbol: str, execution_guards: dict):
        fetcher = getattr(self.state_store, "get_recent_closed_trade_case_for_symbol", None)
        if not callable(fetcher) or not symbol:
            return {}
        close_minutes = int(execution_guards.get("symbol_reentry_close_cooldown_minutes", 0) or 0)
        loss_minutes = int(execution_guards.get("symbol_reentry_loss_cooldown_minutes", 0) or 0)
        loss_threshold_pct = float(execution_guards.get("symbol_reentry_loss_threshold_pct", 0.0) or 0.0)
        if close_minutes <= 0 and loss_minutes <= 0:
            return {}
        return fetcher(
            symbol=symbol,
            close_cooldown_minutes=close_minutes,
            loss_cooldown_minutes=loss_minutes,
            loss_threshold_pct=loss_threshold_pct,
            require_no_active_case=True,
        ) or {}

    def _enrich_risk_state_for_cycle(self, risk_state, account_state, market_snapshots, execution_guards: dict):
        snapshot = self._coerce_risk_state(risk_state)
        symbol = next(iter((market_snapshots or {}).keys()), self._primary_symbol())
        has_live_position = any(
            str(getattr(position, "symbol", "") or "").strip().upper() == symbol
            and abs(float(getattr(position, "size", 0.0) or 0.0)) > 0
            for position in (getattr(account_state, "positions", []) or [])
        )
        has_live_open_order = any(
            str(getattr(order, "symbol", "") or "").strip().upper() == symbol
            for order in (getattr(account_state, "open_orders", []) or [])
        )
        active_case_fetcher = getattr(self.state_store, "get_active_trade_case", None)
        has_active_trade_case = bool(active_case_fetcher(symbol)) if callable(active_case_fetcher) else False
        if has_live_position or has_live_open_order or has_active_trade_case:
            return snapshot.model_copy(
                update={
                    "reentry_guard_active": False,
                    "reentry_guard_symbol": symbol,
                    "reentry_guard_reason": None,
                    "reentry_guard_remaining_minutes": 0,
                    "reentry_guard_last_closed_at": None,
                    "reentry_guard_last_estimated_pnl_pct": None,
                    "reentry_guard_last_exit_reason": None,
                }
            )
        guard = self._get_symbol_reentry_guard(symbol, execution_guards)
        if not guard or not bool(guard.get("active")):
            return snapshot
        return snapshot.model_copy(
            update={
                "reentry_guard_active": True,
                "reentry_guard_symbol": symbol,
                "reentry_guard_reason": str(guard.get("reason") or "").strip() or "recent_symbol_close",
                "reentry_guard_remaining_minutes": int(guard.get("remaining_minutes", 0) or 0),
                "reentry_guard_last_closed_at": guard.get("closed_at"),
                "reentry_guard_last_estimated_pnl_pct": guard.get("estimated_pnl_pct"),
                "reentry_guard_last_exit_reason": guard.get("exit_reason"),
            }
        )

    def _is_daily_kill_switch_active(self, risk_state) -> bool:
        risk_limits = self.risk_engine.policy.get("risk_limits", {})
        daily_max_loss_pct = float(risk_limits.get("daily_max_loss_pct", 0.0) or 0.0)
        if daily_max_loss_pct <= 0:
            return False
        daily_pnl_pct = float(self._coerce_risk_state(risk_state).daily_pnl_pct or 0.0)
        return daily_pnl_pct <= -abs(daily_max_loss_pct)

    def _is_daily_kill_switch_reason(self, reason: str) -> bool:
        return "Kill switch active: daily pnl" in str(reason or "")

    def _build_cooldown_extension_signature(self, risk_state):
        risk_state = self._coerce_risk_state(risk_state)
        if not bool(risk_state.cooldown_active):
            return None
        risk_limits = self.risk_engine.policy.get("risk_limits", {})
        dynamic_config = risk_limits.get("dynamic_cooldown", {}) or {}
        base_minutes = int(dynamic_config.get("base_minutes", risk_limits.get("cooldown_minutes", 0)) or 0)
        effective_minutes = int(risk_state.cooldown_minutes_effective or 0)
        if effective_minutes <= max(0, base_minutes):
            return None
        return (
            base_minutes,
            effective_minutes,
            tuple(str(component) for component in (risk_state.cooldown_components or [])),
            int(risk_state.consecutive_losses or 0),
            str(risk_state.dominant_failure_reason or "").strip(),
            round(float(risk_state.last_loss_estimated_pnl_pct or 0.0), 4),
        )

    def _maybe_alert_risk_state_transitions(self, risk_state):
        risk_state = self._coerce_risk_state(risk_state)
        daily_kill_switch_active = self._is_daily_kill_switch_active(risk_state)
        if daily_kill_switch_active and self._daily_kill_switch_active is not True:
            daily_max_loss_pct = abs(
                float(self.risk_engine.policy.get("risk_limits", {}).get("daily_max_loss_pct", 0.0) or 0.0)
            )
            daily_pnl_pct = float(risk_state.daily_pnl_pct or 0.0)
            self.alert_manager.send(
                "Daily Kill Switch Activated",
                (
                    f"Daily pnl {daily_pnl_pct:.2f}% breached limit {-daily_max_loss_pct:.2f}%. "
                    "New entries are blocked until the daily reset."
                ),
                level="error",
            )
        self._daily_kill_switch_active = daily_kill_switch_active

        drawdown_active = bool(risk_state.drawdown_active)
        drawdown_activate_pct = risk_state.drawdown_activate_pct
        daily_pnl_pct = float(risk_state.daily_pnl_pct or 0.0)
        if drawdown_active and self._drawdown_mode_active is not True:
            threshold_text = ""
            if drawdown_activate_pct is not None:
                threshold_text = f" (threshold {-abs(float(drawdown_activate_pct)):.2f}%)"
            self.alert_manager.send(
                "Drawdown Mode Entered",
                f"Daily pnl {daily_pnl_pct:.2f}% entered drawdown mode{threshold_text}.",
                level="warning",
            )
        elif not drawdown_active and self._drawdown_mode_active is True:
            self.alert_manager.send(
                "Drawdown Mode Cleared",
                f"Daily pnl recovered to {daily_pnl_pct:.2f}%. Drawdown controls are cleared.",
                level="info",
            )
        self._drawdown_mode_active = drawdown_active

        cooldown_signature = self._build_cooldown_extension_signature(risk_state)
        if cooldown_signature is not None and self._cooldown_extension_signature != cooldown_signature:
            base_minutes, effective_minutes, components, losses, pattern_reason, last_loss_pct = cooldown_signature
            detail_parts = [f"{base_minutes}m -> {effective_minutes}m"]
            remaining_minutes = int(risk_state.cooldown_remaining_minutes or 0)
            if remaining_minutes > 0:
                detail_parts.append(f"{remaining_minutes}m remaining")
            if losses > 0:
                detail_parts.append(f"{losses} consecutive loss(es)")
            if components:
                detail_parts.append(f"reasons={', '.join(components)}")
            if pattern_reason:
                detail_parts.append(f"pattern={pattern_reason}")
            if last_loss_pct != 0.0:
                detail_parts.append(f"last_loss={last_loss_pct:.2f}%")
            self.alert_manager.send(
                "Cooldown Extended",
                "Dynamic cooldown extended: " + "; ".join(detail_parts) + ".",
                level="warning",
            )
        self._cooldown_extension_signature = cooldown_signature

    def _get_public_ws_state(self):
        feed = getattr(self, "public_feed", None)
        if feed is None or not hasattr(feed, "get_diagnostics"):
            return None
        diagnostics = feed.get_diagnostics()
        if not bool(diagnostics.get("started")) or not bool(diagnostics.get("available", True)):
            return None
        stale_after_ms = int(diagnostics.get("ws_stale_after_ms", 0) or 0)
        last_message_age_ms = diagnostics.get("last_message_age_ms")
        stale_symbols = [str(symbol) for symbol in (diagnostics.get("stale_symbols") or []) if symbol]
        is_stale = bool(stale_symbols)
        if not is_stale and last_message_age_ms is not None and stale_after_ms > 0:
            is_stale = int(last_message_age_ms) > stale_after_ms
        return {
            "is_stale": is_stale,
            "last_message_age_ms": last_message_age_ms,
            "stale_after_ms": stale_after_ms,
            "stale_symbols": stale_symbols,
        }

    def _get_private_ws_state(self):
        feed = getattr(self, "private_account_feed", None)
        if feed is None or not hasattr(feed, "get_diagnostics"):
            return None
        diagnostics = feed.get_diagnostics()
        if not bool(diagnostics.get("started")) or not bool(diagnostics.get("available", True)):
            return None
        return {
            "health_status": str(diagnostics.get("health_status") or "").lower(),
            "last_ws_message_age_ms": diagnostics.get("last_ws_message_age_ms"),
            "last_private_event_age_ms": diagnostics.get("last_private_event_age_ms"),
            "stale_after_ms": int(diagnostics.get("ws_stale_after_ms", 0) or 0),
        }

    def _maybe_alert_websocket_transitions(self):
        public_state = self._get_public_ws_state()
        if public_state is not None:
            if public_state["is_stale"] and self._public_ws_stale is not True:
                age_seconds = round(float(public_state.get("last_message_age_ms") or 0.0) / 1000.0, 1)
                threshold_seconds = round(float(public_state.get("stale_after_ms") or 0.0) / 1000.0, 1)
                stale_symbols = public_state.get("stale_symbols") or []
                symbol_suffix = (
                    f" Stale symbols: {', '.join(stale_symbols)}."
                    if stale_symbols
                    else ""
                )
                self.alert_manager.send(
                    "WebSocket Stale",
                    (
                        f"Public market WebSocket exceeded freshness threshold "
                        f"({age_seconds}s > {threshold_seconds}s).{symbol_suffix}"
                    ),
                    level="warning",
                )
            elif not public_state["is_stale"] and self._public_ws_stale is True:
                age_seconds = round(float(public_state.get("last_message_age_ms") or 0.0) / 1000.0, 1)
                self.alert_manager.send(
                    "WebSocket Recovered",
                    f"Public market WebSocket recovered. Latest message age is {age_seconds}s.",
                    level="info",
                )
            self._public_ws_stale = bool(public_state["is_stale"])

        private_state = self._get_private_ws_state()
        if private_state is not None:
            is_stale = private_state["health_status"] == "stale"
            if is_stale and self._private_ws_stale is not True:
                ws_age_seconds = round(float(private_state.get("last_ws_message_age_ms") or 0.0) / 1000.0, 1)
                event_age_seconds = round(float(private_state.get("last_private_event_age_ms") or 0.0) / 1000.0, 1)
                threshold_seconds = round(float(private_state.get("stale_after_ms") or 0.0) / 1000.0, 1)
                self.alert_manager.send(
                    "WebSocket Stale",
                    (
                        "Private account WebSocket entered stale state "
                        f"(ws_age={ws_age_seconds}s, event_age={event_age_seconds}s, threshold={threshold_seconds}s)."
                    ),
                    level="warning",
                )
            elif (
                not is_stale
                and self._private_ws_stale is True
                and private_state["health_status"] == "healthy"
            ):
                ws_age_seconds = round(float(private_state.get("last_ws_message_age_ms") or 0.0) / 1000.0, 1)
                self.alert_manager.send(
                    "WebSocket Recovered",
                    f"Private account WebSocket recovered to healthy state. Latest message age is {ws_age_seconds}s.",
                    level="info",
                )
            self._private_ws_stale = is_stale

    def _loop_minutes_for_mode(self, mode: str) -> int:
        mapping = {
            "1h": self.adaptive_loop_config["base_loop_minutes"],
            "15m": self.adaptive_loop_config["position_loop_minutes"],
            "5m": self.adaptive_loop_config["trigger_loop_minutes"],
        }
        return int(mapping.get(mode, self.adaptive_loop_config["base_loop_minutes"]))

    def _loop_label_for_mode(self, mode: str) -> str:
        return self.LOOP_MODE_LABELS.get(mode, f"Adaptive ({mode})")

    def _next_aligned_epoch(self, minutes: int, now_epoch: float | None = None) -> float:
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        interval_seconds = max(1, int(minutes)) * 60
        return (int(now_epoch // interval_seconds) + 1) * interval_seconds

    def _has_live_positions(self, account_state) -> bool:
        return any(abs(float(getattr(position, "size", 0.0) or 0.0)) > 0 for position in account_state.positions)

    def _has_live_open_orders(self, account_state) -> bool:
        return bool(account_state.open_orders or getattr(account_state, "open_orders_count", 0))

    def _has_recent_activity(self, timestamp_value, window_minutes: int) -> bool:
        if timestamp_value is None:
            return False
        return (time.time() - float(timestamp_value)) < (max(1, int(window_minutes)) * 60)

    def _position_closed_during_cycle(self, before_state, after_state) -> bool:
        if before_state is None or after_state is None:
            return False
        before_symbols = {
            position.symbol
            for position in before_state.positions
            if abs(float(getattr(position, "size", 0.0) or 0.0)) > 0
        }
        after_symbols = {
            position.symbol
            for position in after_state.positions
            if abs(float(getattr(position, "size", 0.0) or 0.0)) > 0
        }
        return bool(before_symbols - after_symbols)

    def _determine_desired_loop_mode(self, cycle_result: dict) -> str:
        final_account_state = cycle_result.get("final_account_state")
        has_positions = bool(cycle_result.get("has_positions"))
        has_open_orders = bool(cycle_result.get("has_open_orders"))
        if final_account_state is not None:
            has_positions = self._has_live_positions(final_account_state)
            has_open_orders = self._has_live_open_orders(final_account_state)
        has_unknown_orders = bool(cycle_result.get("has_unknown_orders"))
        recent_execution = self._has_recent_activity(
            self.last_execution_at,
            self.adaptive_loop_config["recent_execution_window_minutes"],
        )
        recent_close = self._has_recent_activity(
            self.last_close_at,
            self.adaptive_loop_config["recent_close_window_minutes"],
        )
        active_management_target = has_positions or has_open_orders or has_unknown_orders
        if bool(cycle_result.get("trigger_active")) and active_management_target:
            return "5m"
        if active_management_target or recent_execution or recent_close:
            return "15m"
        return "1h"

    def _has_active_management_target(self, cycle_result: dict) -> bool:
        final_account_state = cycle_result.get("final_account_state")
        has_positions = bool(cycle_result.get("has_positions"))
        has_open_orders = bool(cycle_result.get("has_open_orders"))
        if final_account_state is not None:
            has_positions = self._has_live_positions(final_account_state)
            has_open_orders = self._has_live_open_orders(final_account_state)
        has_unknown_orders = bool(cycle_result.get("has_unknown_orders"))
        return has_positions or has_open_orders or has_unknown_orders

    def _promote_loop_mode(self, mode: str, now_epoch: float):
        previous = self.active_loop_mode
        self.active_loop_mode = mode
        self.active_loop_mode_since = now_epoch
        self.trigger_clear_streak = 0
        self.flat_clear_streak = 0
        if previous != mode:
            log.info(f"Adaptive cadence promoted: {previous} -> {mode}")

    def _apply_adaptive_loop_mode(self, desired_mode: str, now_epoch: float, cycle_result: dict | None = None) -> str:
        current_mode = self.active_loop_mode
        if self.LOOP_MODE_PRIORITY.get(desired_mode, 0) > self.LOOP_MODE_PRIORITY.get(current_mode, 0):
            self._promote_loop_mode(desired_mode, now_epoch)
            return self.active_loop_mode

        if desired_mode == current_mode:
            if current_mode == "5m":
                self.trigger_clear_streak = 0
            if current_mode == "15m":
                self.flat_clear_streak = 0
            return current_mode

        has_active_management_target = self._has_active_management_target(cycle_result or {})
        if current_mode == "5m":
            if not has_active_management_target:
                previous = self.active_loop_mode
                self.active_loop_mode = desired_mode
                self.active_loop_mode_since = now_epoch
                self.trigger_clear_streak = 0
                self.flat_clear_streak = 0
                log.info(f"Adaptive cadence downgraded immediately: {previous} -> {desired_mode}")
                return self.active_loop_mode
            self.trigger_clear_streak += 1
            if self.trigger_clear_streak < self.adaptive_loop_config["trigger_clear_cycles"]:
                return current_mode
            previous = self.active_loop_mode
            self.active_loop_mode = desired_mode
            self.active_loop_mode_since = now_epoch
            self.trigger_clear_streak = 0
            self.flat_clear_streak = 0
            log.info(f"Adaptive cadence downgraded: {previous} -> {desired_mode}")
            return self.active_loop_mode

        if current_mode == "15m" and desired_mode == "1h":
            if not has_active_management_target:
                previous = self.active_loop_mode
                self.active_loop_mode = "1h"
                self.active_loop_mode_since = now_epoch
                self.flat_clear_streak = 0
                log.info(f"Adaptive cadence downgraded immediately: {previous} -> 1h")
                return self.active_loop_mode
            self.flat_clear_streak += 1
            held_long_enough = (now_epoch - self.active_loop_mode_since) >= (
                self.adaptive_loop_config["position_min_duration_minutes"] * 60
            )
            if (
                not held_long_enough
                or self.flat_clear_streak < self.adaptive_loop_config["flat_clear_cycles"]
            ):
                return current_mode
            previous = self.active_loop_mode
            self.active_loop_mode = "1h"
            self.active_loop_mode_since = now_epoch
            self.flat_clear_streak = 0
            log.info(f"Adaptive cadence downgraded: {previous} -> 1h")
            return self.active_loop_mode

        return current_mode

    def _refresh_runtime_activity(self, cycle_result: dict, now_epoch: float):
        if int(cycle_result.get("executed_order_count", 0) or 0) > 0:
            self.last_execution_at = now_epoch
        if bool(cycle_result.get("position_closed")):
            self.last_close_at = now_epoch

    def _build_effective_risk_profile(
        self,
        loop_mode: str,
        account_state,
        risk_profile,
        risk_mode: str | None = None,
        regime_key: str = "",
    ):
        effective_profile = dict(risk_profile or {})
        allowed_actions = [
            str(action).upper()
            for action in effective_profile.get("allowed_actions", [])
        ]
        for management_action in ("CANCEL", "REPLACE_ENTRY"):
            if management_action not in allowed_actions:
                allowed_actions.append(management_action)
        if risk_mode == "normal" and regime_key:
            exposure_regime_key = f"{risk_mode}|{regime_key}"
            registry_fetch = getattr(self.state_store, "get_policy_registry_entry_for_regime", None)
            exposure_registry = {}
            if callable(registry_fetch):
                exposure_registry = registry_fetch("exposure_budget", regime_key=exposure_regime_key)
            if exposure_registry:
                config = exposure_registry.get("config") or {}
                for key in ("max_order_notional_pct_balance", "entry_size_multiplier"):
                    if key in config:
                        effective_profile[key] = config[key]
                effective_profile["exposure_policy_key"] = exposure_registry.get("policy_key")
                effective_profile["exposure_regime_key"] = exposure_regime_key
        if loop_mode == "15m":
            effective_profile["cycle_role"] = "position_management"
            effective_profile["cycle_guidance"] = (
                "Prioritize managing live exposure and working orders. Avoid opening new positions in the 15m cycle."
            )
            effective_profile["allowed_actions"] = [
                action for action in allowed_actions
                if action in ("HOLD", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY")
            ]
        elif loop_mode == "5m":
            effective_profile["cycle_role"] = "execution_safety"
            effective_profile["cycle_guidance"] = (
                "Run the 5m safety loop for execution quality, trigger deterioration, and emergency de-risking. "
                "Do not open new positions in the 5m cycle."
            )
            effective_profile["allowed_actions"] = [
                action for action in allowed_actions
                if action in ("HOLD", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY")
            ]
        else:
            effective_profile["cycle_role"] = "strategy"
            effective_profile["cycle_guidance"] = (
                "Set portfolio direction and initiate new exposure only when the higher-timeframe case is justified."
            )
            effective_profile["allowed_actions"] = allowed_actions
        effective_profile["has_live_positions"] = self._has_live_positions(account_state)
        effective_profile["has_live_open_orders"] = self._has_live_open_orders(account_state)
        return effective_profile

    def _run_due_maintenance_tasks(self, now_epoch: float):
        recovery_interval_seconds = self._build_execution_guards()["recovery_sweep_minutes"] * 60
        if (now_epoch - self.last_recovery_sweep_at) >= recovery_interval_seconds:
            self.run_recovery_sweep()
            self.last_recovery_sweep_at = time.time()

        now_local = datetime.now().astimezone()
        if (
            now_local.hour == 4
            and now_local.minute == 0
            and self.last_daily_batch_date != now_local.date().isoformat()
        ):
            self.run_daily_batch()
            self.last_daily_batch_date = now_local.date().isoformat()

    def _extract_entry_orders(self, orders):
        return [
            order
            for order in (orders or [])
            if not getattr(order, "reduce_only", False) and getattr(order, "action", "") in ("BUY", "SELL")
        ]

    def _build_order_size_band(self, order, market_prices):
        reference_price = order.price or market_prices.get(order.symbol) or 0.0
        notional = abs(float(order.quantity or 0.0) * float(reference_price))
        if notional <= 50:
            return "micro"
        if notional <= 250:
            return "tiny"
        if notional <= 1000:
            return "small"
        if notional <= 5000:
            return "medium"
        if notional <= 20000:
            return "large"
        return "xlarge"

    def _build_order_fingerprint(self, order, market_prices):
        size_band = self._build_order_size_band(order, market_prices)
        key = f"{order.symbol}|{order.action}|{size_band}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return {
            "symbol": order.symbol,
            "action": order.action,
            "size_band": size_band,
            "fingerprint": digest,
        }

    def _acquire_entry_mutexes(self, entry_orders, owner: str, ttl_seconds: int):
        acquired = []
        for symbol in sorted({order.symbol for order in entry_orders if order.symbol}):
            if not self.state_store.acquire_entry_mutex(symbol, owner=owner, ttl_seconds=ttl_seconds):
                for locked_symbol in acquired:
                    self.state_store.release_entry_mutex(locked_symbol, owner=owner)
                return False, acquired
            acquired.append(symbol)
        return True, acquired

    def _release_entry_mutexes(self, symbols, owner: str):
        for symbol in symbols or []:
            self.state_store.release_entry_mutex(symbol, owner=owner)

    def _resolve_runtime_state_from_recovered_order(self, recovered_order: dict) -> str:
        status = str(
            (recovered_order or {}).get("status")
            or ((recovered_order or {}).get("info") or {}).get("orderStatus")
            or "unknown"
        ).lower()
        if status in ("closed", "filled"):
            return "completed"
        if status in ("canceled", "cancelled"):
            return "cancelled"
        if status in ("failed", "rejected"):
            return "failed"
        if status in ("open", "partially_filled", "partiallyfilled"):
            return "partial_filled"
        return "unknown"

    def _extract_symbols_from_rationale_items(self, rationale_items):
        symbols = set()
        for item in rationale_items or []:
            evidence_rows = (item.evidence or []) if hasattr(item, "evidence") else (item.get("evidence") or [])
            for evidence in evidence_rows:
                symbol = evidence.symbol if hasattr(evidence, "symbol") else evidence.get("symbol")
                if symbol:
                    symbols.add(str(symbol))
        return symbols

    def _compact_recent_journal_row(self, row, max_rationale_items: int, max_evidence_per_item: int):
        rationale = row.get("rationale") or []
        compact_rationale = rationale[:2] if isinstance(rationale, list) else [str(rationale)]
        compact_items = []
        for item in (row.get("rationale_items") or [])[:max_rationale_items]:
            evidence_rows = (item.evidence or []) if hasattr(item, "evidence") else (item.get("evidence") or [])
            compact_evidence = []
            for evidence in evidence_rows[:max_evidence_per_item]:
                compact_evidence.append(
                    {
                        "signal": evidence.signal if hasattr(evidence, "signal") else evidence.get("signal"),
                        "symbol": evidence.symbol if hasattr(evidence, "symbol") else evidence.get("symbol"),
                        "timeframe": evidence.timeframe if hasattr(evidence, "timeframe") else evidence.get("timeframe"),
                        "operator": evidence.operator if hasattr(evidence, "operator") else evidence.get("operator"),
                        "observed_value": (
                            evidence.observed_value if hasattr(evidence, "observed_value") else evidence.get("observed_value")
                        ),
                        "threshold": evidence.threshold if hasattr(evidence, "threshold") else evidence.get("threshold"),
                        "units": evidence.units if hasattr(evidence, "units") else evidence.get("units"),
                        "source": evidence.source if hasattr(evidence, "source") else evidence.get("source"),
                    }
                )
            compact_items.append(
                {
                    "summary": item.summary if hasattr(item, "summary") else item.get("summary"),
                    "why_it_matters": item.why_it_matters if hasattr(item, "why_it_matters") else item.get("why_it_matters"),
                    "trade_implication": item.trade_implication if hasattr(item, "trade_implication") else item.get("trade_implication"),
                    "failure_mode": item.failure_mode if hasattr(item, "failure_mode") else item.get("failure_mode"),
                    "evidence": compact_evidence,
                }
            )
        return {
            "journal_id": row.get("journal_id") or row.get("id"),
            "audit_log_id": row.get("audit_log_id"),
            "timestamp": row.get("timestamp"),
            "decision": row.get("decision") or "HOLD",
            "rationale": compact_rationale,
            "rationale_items": compact_items,
            "confidence": row.get("confidence") or 0.0,
            "risk_reason": row.get("risk_reason") or "",
            "estimated_pnl_pct": row.get("estimated_pnl_pct"),
            "loss_signal": bool(row.get("loss_signal")),
            "ledger_net_realized_pnl": row.get("ledger_net_realized_pnl") or 0.0,
            "ledger_fee_cost": row.get("ledger_fee_cost") or 0.0,
            "risk_approved": row.get("risk_approved"),
            "risk_mode": row.get("risk_mode"),
            "trigger_symbols": row.get("trigger_symbols") or [],
            "risk_trigger_symbols": row.get("risk_trigger_symbols") or [],
            "evidence_quality": row.get("evidence_quality") or {},
            "entry_size_cap": row.get("entry_size_cap") or {},
            "trade_case_ids": row.get("trade_case_ids") or [],
            "case_statuses": row.get("case_statuses") or {},
            "case_summary": row.get("case_summary") or "",
            "similarity_score": row.get("similarity_score"),
            "matched_features": row.get("matched_features") or [],
            "summary": row.get("summary"),
        }

    def _build_market_case_signature(self, market_snapshots, risk_mode: str) -> dict:
        symbols = set(market_snapshots.keys())
        trigger_reasons = set()
        risk_trigger_reasons = set()
        regime_tags = set()
        for symbol, snapshot in (market_snapshots or {}).items():
            for reason in getattr(snapshot, "opportunity_trigger_reasons", []) or []:
                trigger_reasons.add(f"{symbol}:{reason}")
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

    def _build_active_policy_bundle_summary(self, regime_key: str) -> dict:
        if not hasattr(self.state_store, "get_active_policy_bundle"):
            return {}
        bundle = self.state_store.get_active_policy_bundle(regime_key=regime_key)
        if not bundle:
            return {}
        return {
            "bundle_key": bundle.get("bundle_key"),
            "regime_key": bundle.get("regime_key"),
            "alpha_policy_key": bundle.get("alpha_policy_key"),
            "position_policy_key": bundle.get("position_policy_key"),
            "safety_policy_key": bundle.get("safety_policy_key"),
            "execution_policy_key": bundle.get("execution_policy_key"),
            "metrics": bundle.get("metrics") or {},
        }

    def _build_regime_drift_summary(self, regime_key: str, bundle_summary: dict) -> dict:
        bundle_regime_key = str((bundle_summary or {}).get("regime_key") or "").strip()
        drift_detected = bool(bundle_regime_key and regime_key and bundle_regime_key != regime_key)
        return {
            "current_regime_key": regime_key,
            "bundle_regime_key": bundle_regime_key,
            "drift_detected": drift_detected,
        }

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
            recency_bonus = 0.0
            if row.get("closed_at") or row.get("opened_at"):
                recency_bonus = 0.5
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
            final_score = (
                best_similarity
                + recency_bonus
                + outcome_bonus
                + risk_mode_bonus
                + band_match_bonus
                - slippage_penalty
            )
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
        if decision in ("BUY", "SELL", "REDUCE", "EXIT"):
            score += 12.0
            matched_features.append(f"decision:{decision}")
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

        score += max(0.0, 5.0 - (row_index * 0.2))
        deduped_features = list(dict.fromkeys(matched_features))
        return score, deduped_features

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
            scored.append((score, index, enriched_row))

        selected = sorted(scored, key=lambda item: (-item[0], item[1]))[: max(1, limit)]
        selected_indices = {index for _, index, _ in selected}
        selected_rows = {index: row for _, index, row in selected}
        ordered_rows = [selected_rows[index] for index in range(len(rows)) if index in selected_indices]
        return [
            self._compact_recent_journal_row(
                row,
                max_rationale_items=max_rationale_items,
                max_evidence_per_item=max_evidence_per_item,
            )
            for row in ordered_rows
        ]

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
            "metrics": compact_metrics,
        }

    def _select_long_term_memory_context(self, rows, market_snapshots, risk_mode: str, limit: int, max_ignore_when: int):
        if not rows:
            return []
        current_symbols = set(market_snapshots.keys())
        active_trends = set()
        active_volatility = set()
        for snapshot in market_snapshots.values():
            active_trends.add(str(getattr(snapshot.regime, "trend_alignment", "") or ""))
            active_volatility.add(str(getattr(snapshot.regime, "volatility_regime", "") or ""))

        scored = []
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
            retired = bool(row.get("retired") or metrics.get("retired"))
            if retired or lifecycle_status == "retired":
                continue
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
            if str(risk_mode or "") in apply_regimes:
                score += 8.0
            if active_trends and active_trends.intersection(apply_trends):
                score += 6.0
            if active_volatility and active_volatility.intersection(apply_volatility):
                score += 5.0
            if current_symbols and any(symbol in rule_text for symbol in current_symbols):
                score += 4.0
            score += max(0.0, 4.0 - (index * 0.15))
            scored.append((score, index, row))

        selected = sorted(scored, key=lambda item: (-item[0], item[1]))[: max(1, limit)]
        selected_indices = {index for _, index, _ in selected}
        ordered_rows = [rows[index] for index in range(len(rows)) if index in selected_indices]
        return [
            self._compact_memory_record(
                row,
                max_ignore_when=max_ignore_when,
            )
            for row in ordered_rows
        ]

    def _build_similar_past_cases(
        self,
        market_snapshots,
        account_state,
        risk_state,
        is_trigger_mode: bool,
        limit: int = 5,
    ):
        self.last_similar_case_matches = []
        if not is_trigger_mode:
            return []
        current_feature_vectors = self._build_current_market_vectors(
            market_snapshots,
            account_state=account_state,
            risk_state=risk_state,
        )
        vector_matches = self._search_trade_case_vector_matches(
            current_feature_vectors=current_feature_vectors,
            risk_mode="trigger" if is_trigger_mode else "",
            limit=max(limit * 3, 12),
        )
        cases = []
        seen = set()
        for trade_case_id, match in sorted(
            vector_matches.items(),
            key=lambda item: float((item[1] or {}).get("final_score") or 0.0),
            reverse=True,
        ):
            trade_case = self.state_store.get_trade_case(trade_case_id)
            if not trade_case:
                continue
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
            cases.append(case_text)
            if len(cases) >= max(1, int(limit or 1)):
                return cases[: max(1, int(limit or 1))]

        if cases:
            return cases[: max(1, int(limit or 1))]

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
                cases.append(tagged_case)
                if len(cases) >= max(1, int(limit or 1)):
                    return cases[: max(1, int(limit or 1))]
        return cases[: max(1, int(limit or 1))]

    def _save_retrieval_evaluations_for_audit(
        self,
        audit_log_id: int | None,
        intent,
        estimated_pnl_pct: float | None,
        risk_mode: str,
        symbol_journal_entries: list[dict] | None = None,
    ) -> None:
        if not audit_log_id or not hasattr(self.state_store, "save_retrieval_evaluations"):
            return
        raw_matches = list(getattr(self, "last_similar_case_matches", []) or [])
        if not raw_matches:
            return
        entry_lookup = {
            str((entry or {}).get("symbol") or ""): (entry or {})
            for entry in (symbol_journal_entries or [])
            if str((entry or {}).get("symbol") or "").strip()
        }
        decision_symbols = {
            str(getattr(order, "symbol", "") or "").strip()
            for order in (getattr(intent, "orders", []) or [])
            if str(getattr(order, "symbol", "") or "").strip()
        }
        entries = []
        for index, match in enumerate(raw_matches, start=1):
            matched_symbol = str(match.get("matched_symbol") or match.get("case_symbol") or "").strip()
            symbol_entry = entry_lookup.get(matched_symbol) or entry_lookup.get(str(match.get("case_symbol") or "").strip()) or {}
            entries.append(
                {
                    "context_type": str(match.get("context_type") or "similar_past_cases"),
                    "rank_index": int(match.get("rank_index") or index),
                    "trade_case_id": match.get("trade_case_id"),
                    "case_symbol": match.get("case_symbol"),
                    "matched_symbol": matched_symbol,
                    "decision": getattr(intent, "decision", None),
                    "decision_symbol_aligned": bool(matched_symbol and matched_symbol in decision_symbols),
                    "estimated_pnl_pct": estimated_pnl_pct,
                    "similarity": match.get("similarity"),
                    "final_score": match.get("final_score"),
                    "retrieval_source": match.get("retrieval_source"),
                    "matched_band_count": match.get("matched_band_count") or 0,
                    "risk_mode": risk_mode,
                    "outcome_trade_case_id": symbol_entry.get("trade_case_id"),
                    "outcome_status": symbol_entry.get("case_status"),
                    "outcome_realized_pnl": symbol_entry.get("net_realized_pnl"),
                    "outcome_estimated_pnl_pct": symbol_entry.get("estimated_pnl_pct"),
                    "outcome_holding_minutes": symbol_entry.get("holding_minutes"),
                    "outcome_observed_mfe_pct": symbol_entry.get("observed_mfe_pct"),
                    "outcome_observed_mae_pct": symbol_entry.get("observed_mae_pct"),
                    "outcome_slippage_bps": symbol_entry.get("slippage_bps"),
                    "outcome_updated_at": symbol_entry.get("case_closed_at") or symbol_entry.get("case_opened_at"),
                    "payload": match.get("payload") or {},
                }
            )
        self.state_store.save_retrieval_evaluations(audit_log_id, entries)

    def _backfill_retrieval_outcomes_for_symbol_entries(
        self,
        symbol_journal_entries: list[dict] | None,
    ) -> None:
        if not hasattr(self.state_store, "update_retrieval_evaluations_for_trade_case"):
            return
        for entry in symbol_journal_entries or []:
            trade_case_id = str(entry.get("trade_case_id") or "").strip()
            symbol = str(entry.get("symbol") or "").strip()
            if not trade_case_id or not symbol:
                continue
            self.state_store.update_retrieval_evaluations_for_trade_case(
                trade_case_id=trade_case_id,
                case_symbol=symbol,
                entry_audit_log_id=entry.get("audit_log_id"),
                outcome_status=entry.get("case_status"),
                outcome_realized_pnl=entry.get("net_realized_pnl"),
                outcome_estimated_pnl_pct=entry.get("estimated_pnl_pct"),
                outcome_holding_minutes=entry.get("holding_minutes"),
                outcome_observed_mfe_pct=entry.get("observed_mfe_pct"),
                outcome_observed_mae_pct=entry.get("observed_mae_pct"),
                outcome_slippage_bps=entry.get("slippage_bps"),
                outcome_updated_at=entry.get("case_closed_at") or entry.get("case_opened_at"),
            )

    def _backfill_shadow_policy_outcomes_for_symbol_entries(
        self,
        symbol_journal_entries: list[dict] | None,
    ) -> None:
        if not hasattr(self.state_store, "update_shadow_decisions_for_trade_case"):
            return
        for entry in symbol_journal_entries or []:
            trade_case_id = str(entry.get("trade_case_id") or "").strip()
            if not trade_case_id:
                continue
            self.state_store.update_shadow_decisions_for_trade_case(
                trade_case_id=trade_case_id,
                outcome_status=entry.get("case_status"),
                outcome_estimated_pnl_pct=entry.get("estimated_pnl_pct"),
            )

    def _build_position_ledger_entries(self, intent, fills, execution_summary):
        if not fills:
            return []

        grouped = {}
        for fill in fills:
            symbol = fill.get("symbol") or "UNKNOWN"
            entry = grouped.setdefault(
                symbol,
                {
                    "symbol": symbol,
                    "decision": intent.decision,
                    "fill_count": 0,
                    "total_amount": 0.0,
                    "gross_realized_pnl": 0.0,
                    "fee_cost": 0.0,
                    "net_realized_pnl": 0.0,
                    "balance_delta": 0.0,
                    "balance_delta_pct": 0.0,
                },
            )
            entry["fill_count"] += 1
            entry["total_amount"] += float(fill.get("amount") or 0.0)
            entry["gross_realized_pnl"] += float(fill.get("realized_pnl") or 0.0)
            entry["fee_cost"] += float(fill.get("fee_cost") or 0.0)

        weights = {}
        total_weight = 0.0
        for symbol, entry in grouped.items():
            weight = abs(entry["gross_realized_pnl"]) + abs(entry["fee_cost"]) + abs(entry["total_amount"])
            if weight == 0:
                weight = 1.0
            weights[symbol] = weight
            total_weight += weight

        balance_delta = execution_summary.get("balance_delta")
        balance_delta_pct = execution_summary.get("balance_delta_pct")
        for symbol, entry in grouped.items():
            entry["net_realized_pnl"] = round(entry["gross_realized_pnl"] - entry["fee_cost"], 8)
            entry["total_amount"] = round(entry["total_amount"], 8)
            entry["gross_realized_pnl"] = round(entry["gross_realized_pnl"], 8)
            entry["fee_cost"] = round(entry["fee_cost"], 8)
            if balance_delta is not None and total_weight > 0:
                entry["balance_delta"] = round(float(balance_delta) * (weights[symbol] / total_weight), 8)
            else:
                entry["balance_delta"] = None
            if balance_delta_pct is not None and total_weight > 0:
                entry["balance_delta_pct"] = round(float(balance_delta_pct) * (weights[symbol] / total_weight), 8)
            else:
                entry["balance_delta_pct"] = None

        return list(grouped.values())

    def _build_execution_policy_evaluation_entries(
        self,
        audit_log_id: int,
        intent,
        planned_orders,
        receipts,
        fills,
        market_snapshots,
        symbol_journal_entries,
        loop_mode: str,
        risk_mode: str,
        is_trigger_mode: bool,
    ):
        if not audit_log_id or not planned_orders:
            return []
        journal_by_symbol = {
            str((entry or {}).get("symbol") or ""): (entry or {})
            for entry in (symbol_journal_entries or [])
            if (entry or {}).get("symbol")
        }
        evaluations = []
        for index, order in enumerate(planned_orders):
            receipt = receipts[index] if index < len(receipts) else {}
            matched_fills = self.execution_engine._get_fills_for_receipt(order, receipt, fills)
            requested_quantity = abs(float(order.quantity or 0.0))
            filled_quantity = round(sum(float(fill.get("amount") or 0.0) for fill in matched_fills), 8)
            if filled_quantity <= 0:
                filled_quantity = round(abs(float(receipt.get("filled") or 0.0)), 8)
            average_fill_price = self._calculate_average_fill_price(matched_fills, receipt)
            snapshot = market_snapshots.get(order.symbol) if isinstance(market_snapshots, dict) else None
            reference_price = self._resolve_execution_reference_price(order, snapshot)
            context = dict(getattr(order, "execution_context", {}) or {})
            symbol_entry = journal_by_symbol.get(order.symbol, {})
            order_action = str(getattr(order, "action", "") or "").upper()
            is_cancel = order_action == "CANCEL"
            if is_cancel:
                status = str(receipt.get("status") or "unknown").lower()
                cancel_success = status in ("canceled", "cancelled", "closed")
                fill_ratio = 1.0 if cancel_success else 0.0
                slippage_bps = None
                requested_quantity = 1.0
                filled_quantity = 1.0 if cancel_success else 0.0
                average_fill_price = None
                reference_price = None
                action_type = context.get("action_type") or "cancel"
                order_type = "CANCEL"
                execution_profile = context.get("execution_profile") or "cancel_live_order"
            else:
                fill_ratio = round(min(1.0, filled_quantity / requested_quantity), 6) if requested_quantity > 0 else None
                slippage_bps = self._estimate_order_execution_slippage_bps(
                    order=order,
                    reference_price=reference_price,
                    average_fill_price=average_fill_price,
                )
                action_type = context.get("action_type") or ("reduce" if order.reduce_only else "entry")
                order_type = order.order_type
                execution_profile = order.execution_profile
            evaluations.append(
                {
                    "symbol": order.symbol,
                    "loop_mode": loop_mode,
                    "risk_mode": risk_mode,
                    "action_type": action_type,
                    "urgency_bucket": context.get("urgency_bucket") or "unknown",
                    "liquidity_bucket": context.get("liquidity_bucket") or "unknown",
                    "decision": intent.decision,
                    "order_action": order.action,
                    "order_type": order_type,
                    "execution_profile": execution_profile,
                    "reduce_only": bool(order.reduce_only),
                    "trigger_mode": bool(is_trigger_mode),
                    "spread_bps": context.get("spread_bps"),
                    "spread_percentile_1h": context.get("spread_percentile_1h"),
                    "depth_percentile_1h": context.get("depth_percentile_1h"),
                    "reference_price": reference_price,
                    "order_price": order.price,
                    "average_fill_price": average_fill_price,
                    "requested_quantity": requested_quantity,
                    "filled_quantity": filled_quantity,
                    "fill_ratio": fill_ratio,
                    "slippage_bps": slippage_bps,
                    "latency_seconds": self._estimate_order_latency_seconds(receipt, matched_fills),
                    "status": receipt.get("status") or "unknown",
                    "outcome_trade_case_id": symbol_entry.get("trade_case_id"),
                    "outcome_estimated_pnl_pct": symbol_entry.get("estimated_pnl_pct"),
                    "outcome_realized_pnl": symbol_entry.get("net_realized_pnl"),
                    "payload": {
                        "audit_log_id": audit_log_id,
                        "time_in_force": getattr(order, "time_in_force", None),
                        "post_only": bool(getattr(order, "post_only", False)),
                        "max_slippage_bps": getattr(order, "max_slippage_bps", None),
                        "limit_offset_bps": getattr(order, "limit_offset_bps", None),
                        "execution_context": context,
                        "receipt": receipt,
                    },
                }
            )
        return evaluations

    def _calculate_average_fill_price(self, fills, receipt):
        total_amount = sum(float(fill.get("amount") or 0.0) for fill in fills)
        if total_amount > 0:
            weighted_cost = sum(float(fill.get("price") or 0.0) * float(fill.get("amount") or 0.0) for fill in fills)
            return round(weighted_cost / total_amount, 8)
        average = receipt.get("average")
        if average not in (None, ""):
            return round(float(average), 8)
        price = receipt.get("price")
        if price not in (None, ""):
            return round(float(price), 8)
        return None

    def _resolve_execution_reference_price(self, order, snapshot):
        if snapshot is None:
            return order.price
        order_book = getattr(snapshot, "order_book", None)
        for candidate in (
            getattr(order_book, "mid_price", None),
            getattr(order_book, "microprice", None),
            getattr(snapshot, "mark_price", None),
            getattr(snapshot, "current_price", None),
            order.price,
        ):
            try:
                if candidate in (None, ""):
                    continue
                value = float(candidate)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    def _estimate_order_execution_slippage_bps(self, order, reference_price, average_fill_price):
        if reference_price in (None, 0) or average_fill_price in (None, 0):
            return None
        reference_price = float(reference_price)
        average_fill_price = float(average_fill_price)
        if order.action == "BUY":
            adverse_delta = max(0.0, average_fill_price - reference_price)
        else:
            adverse_delta = max(0.0, reference_price - average_fill_price)
        return round((adverse_delta / reference_price) * 10000.0, 4)

    def _estimate_order_latency_seconds(self, receipt, matched_fills):
        receipt_timestamp = receipt.get("timestamp")
        if receipt_timestamp in (None, ""):
            return None
        try:
            receipt_ts = float(receipt_timestamp)
            if receipt_ts > 1_000_000_000_000:
                receipt_ts /= 1000.0
        except (TypeError, ValueError):
            return None
        if not matched_fills:
            return None
        normalized_fill_ts = []
        for fill in matched_fills:
            value = fill.get("timestamp")
            if value in (None, ""):
                continue
            try:
                timestamp = float(value)
                if timestamp > 1_000_000_000_000:
                    timestamp /= 1000.0
                normalized_fill_ts.append(timestamp)
            except (TypeError, ValueError):
                continue
        if not normalized_fill_ts:
            return None
        return round(max(0.0, min(normalized_fill_ts) - receipt_ts), 4)

    def _build_symbol_journal_entries(
        self,
        audit_log_id: int,
        intent,
        market_snapshots,
        trigger_symbols,
        risk_trigger_symbols,
        risk_reason: str,
        risk_mode: str,
        risk_approved: bool,
        estimated_pnl_pct,
        ledger_entries,
    ):
        ledger_by_symbol = {
            str((entry or {}).get("symbol")): (entry or {})
            for entry in (ledger_entries or [])
            if (entry or {}).get("symbol")
        }
        symbol_order_actions = {}
        for order in (intent.orders or []):
            action_text = f"{order.action}:{order.order_type}:{order.quantity}:{getattr(order, 'execution_profile', '')}"
            if getattr(order, "action", "") == "CANCEL":
                target = getattr(order, "target_client_order_id", None) or getattr(order, "target_order_id", None)
                action_text = f"CANCEL:{target or 'unknown'}"
            symbol_order_actions.setdefault(order.symbol, []).append(
                action_text
            )

        symbol_evidence = {}
        for item in (intent.rationale_items or []):
            evidence_rows = (item.evidence or []) if hasattr(item, "evidence") else (item.get("evidence") or [])
            for evidence in evidence_rows:
                signal = evidence.signal if hasattr(evidence, "signal") else evidence.get("signal")
                timeframe = evidence.timeframe if hasattr(evidence, "timeframe") else evidence.get("timeframe")
                symbol = evidence.symbol if hasattr(evidence, "symbol") else evidence.get("symbol")
                if not signal:
                    continue
                tag = f"{signal}@{timeframe or 'context'}"
                key = symbol or "__GLOBAL__"
                symbol_evidence.setdefault(key, [])
                if tag not in symbol_evidence[key]:
                    symbol_evidence[key].append(tag)

        symbols = sorted(
            set(market_snapshots.keys())
            | set(ledger_by_symbol.keys())
            | set(symbol_order_actions.keys())
        )
        summary_seed = intent.rationale[0] if intent.rationale else ""
        entries = []
        for symbol in symbols:
            ledger_entry = ledger_by_symbol.get(symbol, {})
            local_signals = list(symbol_evidence.get(symbol, []))
            local_signals.extend(
                signal for signal in symbol_evidence.get("__GLOBAL__", [])
                if signal not in local_signals
            )
            entries.append(
                {
                    "audit_log_id": audit_log_id,
                    "symbol": symbol,
                    "decision": intent.decision,
                    "risk_reason": risk_reason,
                    "risk_mode": risk_mode,
                    "risk_approved": risk_approved,
                    "triggered": symbol in trigger_symbols,
                    "risk_triggered": symbol in risk_trigger_symbols,
                    "net_realized_pnl": ledger_entry.get("net_realized_pnl"),
                    "fee_cost": ledger_entry.get("fee_cost"),
                    "estimated_pnl_pct": estimated_pnl_pct,
                    "evidence_signals": local_signals,
                    "summary": summary_seed,
                    "payload": {
                        "order_actions": symbol_order_actions.get(symbol, []),
                        "thesis_id": getattr(intent, "thesis_id", None),
                        "thesis_summary": getattr(intent, "thesis_summary", None),
                        "invalidation_reason": getattr(intent, "invalidation_reason", None),
                        "management_template_id": getattr(intent, "management_template_id", None),
                        "exit_reason": getattr(intent, "exit_reason", None),
                        "trigger_reasons": (
                            market_snapshots[symbol].trigger_reasons
                            if symbol in market_snapshots
                            else []
                        ),
                        "opportunity_trigger_reasons": (
                            market_snapshots[symbol].opportunity_trigger_reasons
                            if symbol in market_snapshots
                            else []
                        ),
                        "risk_trigger_reasons": (
                            market_snapshots[symbol].risk_trigger_reasons
                            if symbol in market_snapshots
                            else []
                        ),
                    },
                }
            )
        return entries

    def _make_trade_case_id(self, symbol: str, audit_log_id: int, observed_at: str) -> str:
        case_seed = f"{symbol}|{audit_log_id}|{observed_at}"
        digest = hashlib.sha1(case_seed.encode("utf-8")).hexdigest()[:10]
        compact_ts = (
            str(observed_at or "")
            .replace("-", "")
            .replace(":", "")
            .replace(" ", "T")
        )
        return f"{symbol}-{compact_ts}-{digest}"

    def _calculate_holding_minutes(self, opened_at: str | None, closed_at: str | None) -> float | None:
        if not opened_at or not closed_at:
            return None
        try:
            opened_dt = datetime.strptime(opened_at, "%Y-%m-%d %H:%M:%S")
            closed_dt = datetime.strptime(closed_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        elapsed_seconds = max(0.0, (closed_dt - opened_dt).total_seconds())
        return round(elapsed_seconds / 60.0, 2)

    def _get_positions_by_symbol(self, account_state) -> dict[str, object]:
        if account_state is None:
            return {}
        return {
            position.symbol: position
            for position in getattr(account_state, "positions", []) or []
            if getattr(position, "symbol", None)
        }

    def _get_symbol_observed_pnl_pct(self, symbol: str, before_state, after_state) -> float | None:
        positions_after = self._get_positions_by_symbol(after_state)
        positions_before = self._get_positions_by_symbol(before_state)
        after_position = positions_after.get(symbol)
        before_position = positions_before.get(symbol)
        for candidate in (after_position, before_position):
            if candidate is None:
                continue
            try:
                return float(getattr(candidate, "unrealized_pnl_pct", None))
            except (TypeError, ValueError):
                continue
        return None

    def _get_symbol_entry_price(self, symbol: str, before_state, after_state) -> float | None:
        positions_after = self._get_positions_by_symbol(after_state)
        positions_before = self._get_positions_by_symbol(before_state)
        for candidate in (positions_after.get(symbol), positions_before.get(symbol)):
            if candidate is None:
                continue
            try:
                entry_price = float(getattr(candidate, "entry_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if entry_price > 0:
                return entry_price
        return None

    def _get_symbol_entry_side(self, symbol: str, before_state, after_state, symbol_orders: list) -> str | None:
        positions_after = self._get_positions_by_symbol(after_state)
        positions_before = self._get_positions_by_symbol(before_state)
        for candidate in (positions_after.get(symbol), positions_before.get(symbol)):
            side = str(getattr(candidate, "side", "") or "").upper()
            if side in ("LONG", "SHORT"):
                return side
        for order in symbol_orders:
            action = str(getattr(order, "action", "") or "").upper()
            if action == "BUY":
                return "LONG"
            if action == "SELL":
                return "SHORT"
        return None

    def _normalize_case_timestamp(self, value) -> str | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            timestamp = float(value)
            if timestamp > 1_000_000_000_000:
                timestamp /= 1000.0
            try:
                return datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
            except (OverflowError, OSError, ValueError):
                return None
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit():
            return self._normalize_case_timestamp(int(text))
        candidate = text.replace("T", " ").replace("Z", "")
        if "." in candidate:
            candidate = candidate.split(".", 1)[0]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(candidate, fmt)
                return parsed.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(UTC).replace(tzinfo=None)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def _get_symbol_fill_boundary_timestamp(
        self,
        audit_log_id: int | None,
        symbol: str,
        boundary: str,
    ) -> str | None:
        if not audit_log_id or not symbol or not hasattr(self.state_store, "get_execution_fills"):
            return None
        fills = self.state_store.get_execution_fills(audit_log_id)
        normalized = []
        for fill in fills or []:
            if str(fill.get("symbol") or "") != symbol:
                continue
            fill_timestamp = self._normalize_case_timestamp(fill.get("fill_timestamp"))
            if fill_timestamp:
                normalized.append(fill_timestamp)
        if not normalized:
            return None
        return min(normalized) if boundary == "first" else max(normalized)

    def _resolve_trade_case_snapshot_window(
        self,
        symbol: str,
        trade_case: dict | None,
        entry_audit_log_id: int | None = None,
        exit_audit_log_id: int | None = None,
        fallback_opened_at: str | None = None,
        fallback_closed_at: str | None = None,
    ) -> tuple[str | None, str | None]:
        trade_case = dict(trade_case or {})
        start_timestamp = self._get_symbol_fill_boundary_timestamp(
            entry_audit_log_id or trade_case.get("entry_audit_log_id"),
            symbol,
            boundary="first",
        )
        end_timestamp = self._get_symbol_fill_boundary_timestamp(
            exit_audit_log_id or trade_case.get("exit_audit_log_id"),
            symbol,
            boundary="last",
        )
        start_timestamp = start_timestamp or self._normalize_case_timestamp(
            fallback_opened_at or trade_case.get("opened_at")
        )
        end_timestamp = end_timestamp or self._normalize_case_timestamp(
            fallback_closed_at or trade_case.get("closed_at")
        )
        if start_timestamp and end_timestamp and start_timestamp > end_timestamp:
            end_timestamp = None
        return start_timestamp, end_timestamp

    def _recompute_trade_case_excursions(
        self,
        trade_case_id: str | None,
        symbol: str | None = None,
        trade_case: dict | None = None,
        entry_audit_log_id: int | None = None,
        exit_audit_log_id: int | None = None,
        fallback_opened_at: str | None = None,
        fallback_closed_at: str | None = None,
        current_observed_pnl_pct: float | None = None,
    ) -> tuple[float | None, float | None]:
        if not trade_case_id or not hasattr(self.state_store, "get_trade_case_snapshots"):
            return current_observed_pnl_pct, current_observed_pnl_pct
        snapshots = []
        start_timestamp = None
        end_timestamp = None
        if symbol:
            start_timestamp, end_timestamp = self._resolve_trade_case_snapshot_window(
                symbol=symbol,
                trade_case=trade_case,
                entry_audit_log_id=entry_audit_log_id,
                exit_audit_log_id=exit_audit_log_id,
                fallback_opened_at=fallback_opened_at,
                fallback_closed_at=fallback_closed_at,
            )
        window_applied = bool(start_timestamp or end_timestamp)
        if hasattr(self.state_store, "get_trade_case_snapshots_between") and window_applied:
            snapshots = self.state_store.get_trade_case_snapshots_between(
                trade_case_id,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
        if not snapshots and not window_applied:
            snapshots = self.state_store.get_trade_case_snapshots(trade_case_id)
        pnl_path = []
        for snapshot in snapshots:
            try:
                if snapshot.get("unrealized_pnl_pct") is not None:
                    pnl_path.append(float(snapshot["unrealized_pnl_pct"]))
            except (TypeError, ValueError):
                continue
        if current_observed_pnl_pct is not None:
            pnl_path.append(float(current_observed_pnl_pct))
        if not pnl_path:
            return None, None
        return max(pnl_path), min(pnl_path)

    def _build_trade_case_feature_vector(
        self,
        symbol: str,
        market_snapshots,
        account_state,
        risk_state,
    ) -> list[float]:
        if symbol not in (market_snapshots or {}):
            return []
        vectors = self._build_current_market_vectors(
            {symbol: market_snapshots.get(symbol)},
            account_state=account_state,
            risk_state=risk_state,
        )
        return list(vectors.get(symbol) or [])

    def _build_trade_case_snapshot_entries(
        self,
        audit_log_id: int | None,
        observed_at: str,
        account_state,
        market_snapshots,
        symbol_journal_entries: list[dict],
    ) -> list[dict]:
        fetch_active_cases = getattr(self.state_store, "get_all_active_trade_cases", None)
        active_cases = fetch_active_cases() if callable(fetch_active_cases) else []
        active_by_symbol = {
            str((row or {}).get("symbol") or ""): (row or {})
            for row in (active_cases or [])
            if (row or {}).get("symbol") and (row or {}).get("trade_case_id")
        }
        entries_by_symbol = {
            str((row or {}).get("symbol") or ""): (row or {})
            for row in (symbol_journal_entries or [])
            if (row or {}).get("symbol") and (row or {}).get("trade_case_id")
        }
        positions_by_symbol = self._get_positions_by_symbol(account_state)
        snapshot_entries = []
        for symbol in sorted(set(active_by_symbol.keys()) | set(entries_by_symbol.keys())):
            case_entry = entries_by_symbol.get(symbol) or {}
            active_case = active_by_symbol.get(symbol) or {}
            trade_case_id = case_entry.get("trade_case_id") or active_case.get("trade_case_id")
            if not trade_case_id:
                continue
            snapshot = market_snapshots.get(symbol) if isinstance(market_snapshots, dict) else None
            position = positions_by_symbol.get(symbol)
            order_book = getattr(snapshot, "order_book", None) if snapshot is not None else None
            unrealized_pnl_pct = case_entry.get("observed_pnl_pct")
            if unrealized_pnl_pct is None and position is not None:
                unrealized_pnl_pct = getattr(position, "unrealized_pnl_pct", None)
            snapshot_entries.append(
                {
                    "trade_case_id": trade_case_id,
                    "timestamp": observed_at,
                    "audit_log_id": audit_log_id,
                    "symbol": symbol,
                    "current_price": getattr(snapshot, "current_price", None) if snapshot is not None else None,
                    "mark_price": getattr(snapshot, "mark_price", None) if snapshot is not None else None,
                    "mid_price": getattr(order_book, "mid_price", None) if order_book is not None else None,
                    "spread_bps": getattr(order_book, "spread_bps", None) if order_book is not None else None,
                    "volume_ratio_15m": getattr(snapshot, "volume_ratio_15m", None) if snapshot is not None else None,
                    "unrealized_pnl_pct": unrealized_pnl_pct,
                    "payload": {
                        "case_status": case_entry.get("case_status") or "active",
                        "holding_minutes": case_entry.get("holding_minutes"),
                        "opportunity_trigger_reasons": (
                            getattr(snapshot, "opportunity_trigger_reasons", []) if snapshot is not None else []
                        ),
                        "risk_trigger_reasons": (
                            getattr(snapshot, "risk_trigger_reasons", []) if snapshot is not None else []
                        ),
                    },
                }
            )
        return snapshot_entries

    def _read_live_account_state_for_case_sampling(self):
        private_feed = getattr(self.account_reader, "private_feed", None)
        if private_feed is not None:
            try:
                ws_state = private_feed.get_account_state(self.symbols)
                if ws_state is not None:
                    return ws_state
            except Exception:
                pass
        try:
            return self.account_reader.get_account_state(self.symbols, force_refresh=False)
        except Exception:
            return None

    def _capture_active_trade_case_snapshots(self, now_epoch: float | None = None) -> int:
        fetch_active_cases = getattr(self.state_store, "get_all_active_trade_cases", None)
        if not callable(fetch_active_cases):
            return 0
        active_cases = fetch_active_cases()
        if not active_cases:
            return 0
        account_state = self._read_live_account_state_for_case_sampling()
        if account_state is None:
            return 0
        positions_by_symbol = self._get_positions_by_symbol(account_state)
        observed_at = datetime.fromtimestamp(now_epoch or time.time()).strftime("%Y-%m-%d %H:%M:%S")
        snapshot_entries = []
        for active_case in active_cases:
            symbol = str(active_case.get("symbol") or "").strip()
            trade_case_id = str(active_case.get("trade_case_id") or "").strip()
            if not symbol or not trade_case_id:
                continue
            ticker = None
            order_book = None
            if getattr(self, "public_feed", None) is not None:
                try:
                    ticker = self.public_feed.get_ticker(symbol)
                except Exception:
                    ticker = None
                try:
                    order_book = self.public_feed.get_order_book(symbol, limit=10)
                except Exception:
                    order_book = None
            position = positions_by_symbol.get(symbol)
            best_bid = None
            best_ask = None
            if isinstance(order_book, dict):
                bids = order_book.get("bids") or []
                asks = order_book.get("asks") or []
                try:
                    best_bid = float(bids[0][0]) if bids else None
                except (TypeError, ValueError, IndexError):
                    best_bid = None
                try:
                    best_ask = float(asks[0][0]) if asks else None
                except (TypeError, ValueError, IndexError):
                    best_ask = None
            mid_price = ((best_bid + best_ask) / 2.0) if best_bid is not None and best_ask is not None else None
            spread_bps = (
                ((best_ask - best_bid) / mid_price) * 10000.0
                if mid_price not in (None, 0.0) and best_bid is not None and best_ask is not None
                else None
            )
            current_price = None
            mark_price = None
            if isinstance(ticker, dict):
                for candidate in (ticker.get("last"), ticker.get("close"), ticker.get("markPrice"), ticker.get("mark")):
                    try:
                        value = float(candidate)
                    except (TypeError, ValueError):
                        value = 0.0
                    if value > 0:
                        current_price = value
                        break
                for candidate in (ticker.get("markPrice"), ticker.get("mark"), getattr(position, "mark_price", None), current_price):
                    try:
                        value = float(candidate)
                    except (TypeError, ValueError):
                        value = 0.0
                    if value > 0:
                        mark_price = value
                        break
            if mark_price is None and position is not None:
                mark_price = getattr(position, "mark_price", None)
            if current_price is None and mark_price is not None:
                current_price = mark_price
            snapshot_entries.append(
                {
                    "trade_case_id": trade_case_id,
                    "timestamp": observed_at,
                    "audit_log_id": active_case.get("last_audit_log_id"),
                    "symbol": symbol,
                    "current_price": current_price,
                    "mark_price": mark_price,
                    "mid_price": mid_price,
                    "spread_bps": round(spread_bps, 6) if spread_bps is not None else None,
                    "volume_ratio_15m": None,
                    "unrealized_pnl_pct": getattr(position, "unrealized_pnl_pct", None) if position is not None else None,
                    "payload": {
                        "sampling_source": "poll_sampler",
                        "entry_decision": active_case.get("entry_decision"),
                        "last_decision": active_case.get("last_decision"),
                    },
                }
            )
        self.state_store.save_trade_case_snapshots(snapshot_entries)
        return len(snapshot_entries)

    def _sample_trade_case_paths_if_due(self, now_epoch: float) -> int:
        interval_seconds = float(self.adaptive_loop_config.get("trade_case_snapshot_poll_seconds", 5) or 5)
        if interval_seconds <= 0:
            return 0
        if self.last_trade_case_snapshot_at and (now_epoch - self.last_trade_case_snapshot_at) < interval_seconds:
            return 0
        captured = self._capture_active_trade_case_snapshots(now_epoch=now_epoch)
        if captured > 0:
            self.last_trade_case_snapshot_at = now_epoch
        return captured

    def _estimate_symbol_slippage(
        self,
        symbol: str,
        symbol_orders: list,
        fills: list[dict],
        market_snapshots,
    ) -> tuple[float | None, float | None, float | None]:
        symbol_fills = [fill for fill in (fills or []) if str(fill.get("symbol") or "") == symbol]
        if not symbol_fills:
            return None, None, None

        total_amount = sum(abs(float(fill.get("amount") or 0.0)) for fill in symbol_fills)
        if total_amount <= 0:
            return None, None, None
        avg_fill_price = (
            sum(float(fill.get("price") or 0.0) * abs(float(fill.get("amount") or 0.0)) for fill in symbol_fills)
            / total_amount
        )

        planned_prices = [
            float(getattr(order, "price", 0.0) or 0.0)
            for order in (symbol_orders or [])
            if float(getattr(order, "price", 0.0) or 0.0) > 0
        ]
        snapshot = market_snapshots.get(symbol) if isinstance(market_snapshots, dict) else None
        reference_price = None
        if planned_prices:
            reference_price = sum(planned_prices) / len(planned_prices)
        elif snapshot is not None:
            for candidate in (
                getattr(snapshot.order_book, "mid_price", None),
                getattr(snapshot.order_book, "microprice", None),
                getattr(snapshot, "current_price", None),
                getattr(snapshot, "mark_price", None),
            ):
                try:
                    candidate_value = float(candidate)
                except (TypeError, ValueError):
                    candidate_value = 0.0
                if candidate_value > 0:
                    reference_price = candidate_value
                    break
        if not reference_price or reference_price <= 0:
            return avg_fill_price, None, None

        action = str(getattr(symbol_orders[0], "action", "") or "").upper() if symbol_orders else ""
        if action == "BUY":
            adverse_delta = max(0.0, avg_fill_price - reference_price)
        elif action == "SELL":
            adverse_delta = max(0.0, reference_price - avg_fill_price)
        else:
            adverse_delta = abs(avg_fill_price - reference_price)
        slippage_cost = round(adverse_delta * total_amount, 8)
        slippage_bps = round((adverse_delta / reference_price) * 10000.0, 4) if reference_price > 0 else None
        return avg_fill_price, slippage_cost, slippage_bps

    def _get_symbol_exit_price(self, symbol: str, avg_fill_price: float | None, market_snapshots) -> float | None:
        if avg_fill_price and avg_fill_price > 0:
            return avg_fill_price
        snapshot = market_snapshots.get(symbol) if isinstance(market_snapshots, dict) else None
        if snapshot is None:
            return None
        for candidate in (
            getattr(snapshot, "current_price", None),
            getattr(snapshot, "mark_price", None),
            getattr(snapshot.order_book, "mid_price", None),
        ):
            try:
                candidate_value = float(candidate)
            except (TypeError, ValueError):
                candidate_value = 0.0
            if candidate_value > 0:
                return candidate_value
        return None

    def _extract_symbol_failure_mode(self, symbol: str, rationale_items) -> str:
        for item in rationale_items or []:
            evidence_rows = (item.evidence or []) if hasattr(item, "evidence") else (item.get("evidence") or [])
            evidence_symbols = {
                (e.symbol if hasattr(e, "symbol") else e.get("symbol"))
                for e in evidence_rows
            }
            if evidence_symbols and symbol not in evidence_symbols and None not in evidence_symbols:
                continue
            failure_mode = (
                item.failure_mode if hasattr(item, "failure_mode") else item.get("failure_mode")
            )
            if failure_mode:
                return str(failure_mode)
        return ""

    def _derive_thesis_failure_reason(
        self,
        symbol: str,
        entry: dict,
        symbol_snapshot,
        rationale_items,
        risk_reason: str,
        observed_pnl_pct: float | None,
        realized_pnl: float | None,
    ) -> str | None:
        risk_reason_text = str(risk_reason or "").lower()
        if "kill switch" in risk_reason_text:
            return "risk_kill_switch"
        if "cooldown" in risk_reason_text or "drawdown" in risk_reason_text:
            return "risk_profile_pressure"
        if symbol_snapshot is not None and getattr(symbol_snapshot, "risk_trigger_reasons", None):
            return "execution_quality_degraded:" + ",".join(symbol_snapshot.risk_trigger_reasons[:3])
        failure_mode = self._extract_symbol_failure_mode(symbol, rationale_items)
        negative_outcome = (
            (realized_pnl is not None and float(realized_pnl) < 0)
            or (observed_pnl_pct is not None and float(observed_pnl_pct) < 0)
            or bool(entry.get("loss_signal"))
        )
        if negative_outcome and failure_mode:
            return failure_mode
        if negative_outcome and symbol_snapshot is not None and getattr(symbol_snapshot, "opportunity_trigger_reasons", None):
            return "setup_invalidated:" + ",".join(symbol_snapshot.opportunity_trigger_reasons[:3])
        if negative_outcome:
            return "adverse_price_follow_through"
        return None

    def _annotate_trade_case_entries(
        self,
        audit_log_id: int,
        symbol_journal_entries: list[dict],
        position_state_transitions: list[dict],
        intent,
        before_state,
        after_state,
        market_snapshots,
        fills: list[dict],
        risk_mode: str,
        risk_state=None,
        observed_at: str | None = None,
    ) -> list[dict]:
        observed_at = observed_at or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        transitions_by_symbol = {
            str((entry or {}).get("symbol")): (entry or {})
            for entry in (position_state_transitions or [])
            if (entry or {}).get("symbol")
        }
        symbol_orders = {}
        for order in (intent.orders or []):
            symbol_orders.setdefault(order.symbol, []).append(order)

        annotated_entries = []
        for entry in symbol_journal_entries or []:
            symbol = str(entry.get("symbol") or "")
            if not symbol:
                continue
            transition = transitions_by_symbol.get(symbol, {})
            from_state = str(transition.get("from_state") or "")
            to_state = str(transition.get("to_state") or "")
            active_case = self.state_store.get_active_trade_case(symbol)
            trade_case_id = active_case.get("trade_case_id")
            case_opened_at = active_case.get("opened_at")
            case_closed_at = None
            holding_minutes = None
            case_status = None
            symbol_snapshot = market_snapshots.get(symbol) if isinstance(market_snapshots, dict) else None
            current_case = self.state_store.get_trade_case(trade_case_id) if trade_case_id else {}
            observed_pnl_pct = self._get_symbol_observed_pnl_pct(symbol, before_state, after_state)
            avg_fill_price, slippage_cost, slippage_bps = self._estimate_symbol_slippage(
                symbol=symbol,
                symbol_orders=symbol_orders.get(symbol, []),
                fills=fills,
                market_snapshots=market_snapshots,
            )
            realized_pnl = entry.get("net_realized_pnl")
            fee_cost = entry.get("fee_cost")
            thesis_failure_reason = self._derive_thesis_failure_reason(
                symbol=symbol,
                entry=entry,
                symbol_snapshot=symbol_snapshot,
                rationale_items=intent.rationale_items,
                risk_reason=entry.get("risk_reason") or "",
                observed_pnl_pct=observed_pnl_pct,
                realized_pnl=realized_pnl,
            )
            thesis_id = (
                str(getattr(intent, "thesis_id", None) or "").strip()
                or active_case.get("thesis_id")
                or current_case.get("thesis_id")
            )
            management_template_id = (
                str(getattr(intent, "management_template_id", None) or "").strip()
                or active_case.get("management_template_id")
                or current_case.get("management_template_id")
            )
            exit_reason = str(getattr(intent, "exit_reason", None) or "").strip() or None

            if from_state == "flat" and to_state in ("opening", "open"):
                trade_case_id = self._make_trade_case_id(symbol, audit_log_id, observed_at)
                case_opened_at = observed_at
                case_status = "opened"
                self.state_store.upsert_active_trade_case(
                    symbol=symbol,
                    trade_case_id=trade_case_id,
                    opened_at=case_opened_at,
                    entry_decision=entry.get("decision"),
                    thesis_id=thesis_id,
                    management_template_id=management_template_id,
                    last_decision=entry.get("decision"),
                    last_audit_log_id=audit_log_id,
                    payload={
                        "opened_via": transition.get("event_type"),
                        "from_state": from_state,
                        "to_state": to_state,
                        "thesis_summary": getattr(intent, "thesis_summary", None),
                        "invalidation_reason": getattr(intent, "invalidation_reason", None),
                    },
                )
            elif from_state == "open" and to_state == "closed":
                closed_case = self.state_store.close_active_trade_case(
                    symbol=symbol,
                    closed_at=observed_at,
                    last_decision=entry.get("decision"),
                    last_audit_log_id=audit_log_id,
                )
                trade_case_id = closed_case.get("trade_case_id") or trade_case_id
                case_opened_at = closed_case.get("opened_at") or case_opened_at
                case_closed_at = observed_at
                holding_minutes = self._calculate_holding_minutes(case_opened_at, case_closed_at)
                case_status = "closed"
            elif to_state == "reducing":
                if not trade_case_id:
                    trade_case_id = self._make_trade_case_id(symbol, audit_log_id, observed_at)
                    case_opened_at = observed_at
                self.state_store.upsert_active_trade_case(
                    symbol=symbol,
                    trade_case_id=trade_case_id,
                    opened_at=case_opened_at,
                    entry_decision=(active_case.get("entry_decision") or entry.get("decision")),
                    thesis_id=thesis_id,
                    management_template_id=management_template_id,
                    last_decision=entry.get("decision"),
                    last_audit_log_id=audit_log_id,
                    payload={
                        **(active_case.get("payload") or {}),
                        "from_state": from_state,
                        "to_state": to_state,
                        "synthetic_opened_at": case_opened_at is None,
                    },
                )
                case_status = "reduced"
            elif to_state == "open" and from_state == "open":
                if trade_case_id:
                    self.state_store.upsert_active_trade_case(
                        symbol=symbol,
                        trade_case_id=trade_case_id,
                        opened_at=case_opened_at,
                        entry_decision=active_case.get("entry_decision"),
                        thesis_id=thesis_id,
                        management_template_id=management_template_id,
                        last_decision=entry.get("decision"),
                        last_audit_log_id=audit_log_id,
                        payload=active_case.get("payload") or {},
                    )
                case_status = "active" if trade_case_id else None
            elif to_state == "unknown":
                case_status = "unknown"
            elif trade_case_id:
                case_status = "active"

            if trade_case_id and case_opened_at and holding_minutes is None and case_closed_at is None:
                holding_minutes = self._calculate_holding_minutes(case_opened_at, observed_at)

            entry_audit_log_id = current_case.get("entry_audit_log_id") or (
                audit_log_id if case_status == "opened" else active_case.get("last_audit_log_id")
            )
            exit_audit_log_id = (
                audit_log_id if case_status == "closed" else current_case.get("exit_audit_log_id")
            )
            observed_mfe_pct, observed_mae_pct = self._recompute_trade_case_excursions(
                trade_case_id,
                symbol=symbol,
                trade_case=current_case,
                entry_audit_log_id=entry_audit_log_id,
                exit_audit_log_id=exit_audit_log_id,
                fallback_opened_at=case_opened_at,
                fallback_closed_at=case_closed_at,
                current_observed_pnl_pct=observed_pnl_pct,
            )

            entry["trade_case_id"] = trade_case_id
            entry["case_status"] = case_status
            entry["case_opened_at"] = case_opened_at
            entry["case_closed_at"] = case_closed_at
            entry["holding_minutes"] = holding_minutes
            entry["observed_pnl_pct"] = observed_pnl_pct
            entry["observed_mfe_pct"] = observed_mfe_pct
            entry["observed_mae_pct"] = observed_mae_pct
            entry["slippage_cost"] = slippage_cost
            entry["slippage_bps"] = slippage_bps
            entry["entry_price"] = current_case.get("entry_price") or self._get_symbol_entry_price(symbol, before_state, after_state)
            entry["exit_price"] = self._get_symbol_exit_price(symbol, avg_fill_price, market_snapshots)
            entry["entry_side"] = current_case.get("entry_side") or self._get_symbol_entry_side(
                symbol,
                before_state,
                after_state,
                symbol_orders.get(symbol, []),
            )
            entry["thesis_failure_reason"] = thesis_failure_reason
            feature_vector = self._build_trade_case_feature_vector(
                symbol=symbol,
                market_snapshots=market_snapshots,
                account_state=before_state,
                risk_state=risk_state,
            )
            self.state_store.upsert_trade_case(
                {
                    "trade_case_id": trade_case_id,
                    "symbol": symbol,
                    "status": "closed" if case_status == "closed" else ("open" if case_status == "opened" else (case_status or "active")),
                    "risk_mode": risk_mode,
                    "opened_at": case_opened_at,
                    "closed_at": case_closed_at,
                    "entry_audit_log_id": entry_audit_log_id,
                    "exit_audit_log_id": exit_audit_log_id,
                    "entry_decision": current_case.get("entry_decision") or entry.get("decision"),
                    "exit_decision": entry.get("decision") if case_status == "closed" else current_case.get("exit_decision"),
                    "entry_side": entry.get("entry_side"),
                    "entry_price": entry.get("entry_price"),
                    "exit_price": entry.get("exit_price"),
                    "estimated_pnl_pct": observed_pnl_pct,
                    "realized_pnl": realized_pnl,
                    "fee_cost": fee_cost,
                    "holding_minutes": holding_minutes,
                    "observed_mfe_pct": observed_mfe_pct,
                    "observed_mae_pct": observed_mae_pct,
                     "slippage_cost": slippage_cost,
                     "slippage_bps": slippage_bps,
                     "thesis_failure_reason": thesis_failure_reason,
                     "thesis_id": thesis_id,
                     "management_template_id": management_template_id,
                     "exit_reason": (
                         exit_reason
                         if case_status == "closed"
                         else current_case.get("exit_reason")
                     ),
                     "evidence_signals": entry.get("evidence_signals") or [],
                     "trigger_reasons": (entry.get("payload") or {}).get("trigger_reasons") or [],
                     "risk_trigger_reasons": (entry.get("payload") or {}).get("risk_trigger_reasons") or [],
                     "feature_vector": feature_vector or current_case.get("feature_vector") or [],
                     "payload": {
                        "case_status": case_status,
                        "from_state": from_state,
                        "to_state": to_state,
                         "risk_reason": entry.get("risk_reason"),
                         "thesis_summary": getattr(intent, "thesis_summary", None),
                         "invalidation_reason": getattr(intent, "invalidation_reason", None),
                         "management_template_id": management_template_id,
                         "exit_reason": exit_reason,
                         "regime_tags": [
                            f"trend:{getattr(symbol_snapshot.regime, 'trend_alignment', '')}"
                            if symbol_snapshot is not None else "",
                            f"volatility:{getattr(symbol_snapshot.regime, 'volatility_regime', '')}"
                            if symbol_snapshot is not None else "",
                        ],
                        "entry_evidence_signals": entry.get("evidence_signals") or [],
                        "feature_snapshot": {
                            "spread_percentile_1h": getattr(symbol_snapshot, "spread_percentile_1h", None)
                            if symbol_snapshot is not None else None,
                            "oi_change_15m": getattr(symbol_snapshot, "oi_change_15m", None)
                            if symbol_snapshot is not None else None,
                            "oi_change_1h": getattr(symbol_snapshot, "oi_change_1h", None)
                            if symbol_snapshot is not None else None,
                            "volume_ratio_15m": getattr(symbol_snapshot, "volume_ratio_15m", None)
                            if symbol_snapshot is not None else None,
                            "distance_to_invalid_level": getattr(symbol_snapshot, "distance_to_invalid_level", None)
                            if symbol_snapshot is not None else None,
                            "funding_rate_change_1h": getattr(symbol_snapshot, "funding_rate_change_1h", None)
                            if symbol_snapshot is not None else None,
                            "basis_change_1h": getattr(symbol_snapshot, "basis_change_1h", None)
                            if symbol_snapshot is not None else None,
                        },
                    },
                }
            )
            annotated_entries.append(entry)
        return annotated_entries

    def _summarize_trade_cases(self, symbol_journal_entries: list[dict]) -> tuple[list[str], dict[str, str], str]:
        trade_case_ids = []
        case_statuses = {}
        summary_parts = []
        for entry in symbol_journal_entries or []:
            symbol = str(entry.get("symbol") or "")
            trade_case_id = entry.get("trade_case_id")
            case_status = entry.get("case_status")
            if trade_case_id and trade_case_id not in trade_case_ids:
                trade_case_ids.append(trade_case_id)
            if symbol and case_status:
                case_statuses[symbol] = case_status
                summary_parts.append(f"{symbol}:{case_status}")
        return trade_case_ids, case_statuses, ", ".join(summary_parts[:6])

    def _get_deterministic_skip_reason(
        self,
        account_state,
        market_snapshots,
        risk_state,
        risk_mode: str,
        risk_profile,
    ):
        if account_state.positions or account_state.open_orders:
            return None

        risk_state = self._coerce_risk_state(risk_state)
        risk_limits = self.risk_engine.policy.get("risk_limits", {})
        daily_max_loss_pct = float(risk_limits.get("daily_max_loss_pct", 0.0) or 0.0)
        daily_pnl_pct = float(risk_state.daily_pnl_pct or 0.0)
        if daily_max_loss_pct > 0 and daily_pnl_pct <= -daily_max_loss_pct:
            return "daily_loss_kill_switch"

        max_consecutive_losses = int(risk_limits.get("max_consecutive_losses", 0) or 0)
        consecutive_losses = int(risk_state.consecutive_losses or 0)
        if max_consecutive_losses > 0 and consecutive_losses >= max_consecutive_losses:
            return "consecutive_loss_kill_switch"

        if bool(risk_state.cooldown_active):
            return "cooldown_active"

        if bool(risk_state.reentry_guard_active):
            return str(risk_state.reentry_guard_reason or "recent_symbol_close_guard")

        allowed_actions = {str(action).upper() for action in risk_profile.get("allowed_actions", [])}
        if "BUY" not in allowed_actions and "SELL" not in allowed_actions:
            return f"risk_profile_{risk_mode}_blocks_entries"

        return None

    def _build_skip_rationale_items(self, skip_reason: str, risk_state, market_snapshots, risk_profile):
        risk_state_payload = (
            dict(risk_state)
            if isinstance(risk_state, dict)
            else self._serialize_risk_state(risk_state)
        )
        risk_state_snapshot = self._coerce_risk_state(risk_state)
        if skip_reason == "alpha_policy_filtered_all":
            blocked_symbols = risk_state_payload.get("alpha_policy_blocked_symbols") or {}
            symbol, reasons = next(iter(blocked_symbols.items()), ("portfolio", ["policy_filtered"]))
            return [
                RationaleItem(
                    summary="Alpha policy blocked all candidate entries.",
                    why_it_matters="The champion 1h entry filter judged every current setup as low quality.",
                    trade_implication="Stay flat until at least one symbol clears the alpha entry filter.",
                    failure_mode="A future 1h snapshot satisfies the active alpha policy thresholds.",
                    evidence=[
                        EvidenceItem(
                            signal="alpha_policy_block",
                            symbol=symbol if symbol != "portfolio" else None,
                            timeframe="1h",
                            operator="note",
                            observed_value=", ".join(str(reason) for reason in reasons[:3]),
                            threshold=None,
                            units="rule",
                            source="alpha_policy.blocked_symbols",
                        )
                    ],
                )
            ]
        if skip_reason == "cooldown_active":
            remaining = float(risk_state_snapshot.cooldown_remaining_minutes or 0.0)
            effective_minutes = float(risk_state_snapshot.cooldown_minutes_effective or 0.0)
            failure_pattern_score = float(risk_state_snapshot.failure_pattern_score or 0.0)
            dominant_failure_reason = str(risk_state_snapshot.dominant_failure_reason or "").strip()
            evidence = [
                EvidenceItem(
                    signal="cooldown_remaining_minutes",
                    timeframe="risk",
                    operator="gt",
                    observed_value=remaining,
                    threshold=0.0,
                    units="minutes",
                    source="risk_state.cooldown_remaining_minutes",
                )
            ]
            if effective_minutes > 0:
                evidence.append(
                    EvidenceItem(
                        signal="cooldown_minutes_effective",
                        timeframe="risk",
                        operator="gte",
                        observed_value=effective_minutes,
                        threshold=effective_minutes,
                        units="minutes",
                        source="risk_state.cooldown_minutes_effective",
                    )
                )
            if failure_pattern_score > 0:
                evidence.append(
                    EvidenceItem(
                        signal="failure_pattern_score",
                        timeframe="risk",
                        operator="gt",
                        observed_value=failure_pattern_score,
                        threshold=0.0,
                        units="score",
                        source="risk_state.failure_pattern_score",
                    )
                )
            why_it_matters = "The strategy is in mandatory cooldown after recent losses."
            if dominant_failure_reason:
                why_it_matters += f" Repeated failure pattern: {dominant_failure_reason}."
            return [
                RationaleItem(
                    summary="Cooldown blocks new entries.",
                    why_it_matters=why_it_matters,
                    trade_implication="Keep the portfolio flat until cooldown expires and risk pressure normalizes.",
                    failure_mode="Cooldown expires and no other blocking risk state remains.",
                    evidence=evidence,
                )
            ]
        if skip_reason in ("recent_symbol_close_guard", "recent_symbol_loss_guard"):
            remaining = float(risk_state_snapshot.reentry_guard_remaining_minutes or 0.0)
            symbol = str(risk_state_snapshot.reentry_guard_symbol or self._primary_symbol())
            last_pnl = risk_state_snapshot.reentry_guard_last_estimated_pnl_pct
            last_exit_reason = str(risk_state_snapshot.reentry_guard_last_exit_reason or "").strip()
            why_it_matters = (
                f"{symbol} was closed too recently, so immediate re-entry would likely be churn."
                if skip_reason == "recent_symbol_close_guard"
                else f"{symbol} recently closed at a loss, so the symbol-level re-entry guard is still active."
            )
            if last_exit_reason:
                why_it_matters += f" Last exit reason: {last_exit_reason}."
            evidence = [
                EvidenceItem(
                    signal="reentry_guard_remaining_minutes",
                    symbol=symbol,
                    timeframe="risk",
                    operator="gt",
                    observed_value=remaining,
                    threshold=0.0,
                    units="minutes",
                    source="risk_state.reentry_guard_remaining_minutes",
                )
            ]
            if last_pnl is not None:
                evidence.append(
                    EvidenceItem(
                        signal="reentry_guard_last_estimated_pnl_pct",
                        symbol=symbol,
                        timeframe="risk",
                        operator="note",
                        observed_value=float(last_pnl),
                        threshold=None,
                        units="pct",
                        source="risk_state.reentry_guard_last_estimated_pnl_pct",
                    )
                )
            return [
                RationaleItem(
                    summary="Recent symbol exit blocks immediate re-entry.",
                    why_it_matters=why_it_matters,
                    trade_implication="Stay flat on the configured symbol until the symbol-level re-entry window expires.",
                    failure_mode="The re-entry guard expires and no other blocking risk state remains.",
                    evidence=evidence,
                )
            ]
        if skip_reason.startswith("risk_profile_"):
            max_positions = float(risk_profile.get("max_open_positions", 0.0) or 0.0)
            return [
                RationaleItem(
                    summary="Active risk profile disallows new entries.",
                    why_it_matters="Current policy mode only permits defensive actions.",
                    trade_implication="Return HOLD until the risk profile re-enables BUY or SELL.",
                    failure_mode="Risk mode changes and entry actions are allowed again.",
                    evidence=[
                        EvidenceItem(
                            signal="max_open_positions",
                            timeframe="risk",
                            operator="note",
                            observed_value=max_positions,
                            threshold=None,
                            units="count",
                            source="risk_profile.max_open_positions",
                        )
                    ],
                )
            ]
        if skip_reason == "daily_loss_kill_switch":
            daily_pnl_pct = float(risk_state_snapshot.daily_pnl_pct or 0.0)
            limit = float(self.risk_engine.policy.get("risk_limits", {}).get("daily_max_loss_pct", 0.0) or 0.0)
            return [
                RationaleItem(
                    summary="Daily loss kill switch is active.",
                    why_it_matters="Further entries would violate the daily drawdown control.",
                    trade_implication="Stop initiating new risk for the rest of the daily window.",
                    failure_mode="A new trading day resets the daily loss state.",
                    evidence=[
                        EvidenceItem(
                            signal="daily_pnl_pct",
                            timeframe="risk",
                            operator="lte",
                            observed_value=daily_pnl_pct,
                            threshold=-limit,
                            units="pct",
                            source="risk_state.daily_pnl_pct",
                        )
                    ],
                )
            ]
        if skip_reason == "consecutive_loss_kill_switch":
            losses = float(risk_state_snapshot.consecutive_losses or 0.0)
            limit = float(self.risk_engine.policy.get("risk_limits", {}).get("max_consecutive_losses", 0) or 0.0)
            return [
                RationaleItem(
                    summary="Consecutive-loss kill switch is active.",
                    why_it_matters="The recent loss streak signals degraded strategy conditions.",
                    trade_implication="Pause new entries until the loss streak resets.",
                    failure_mode="The loss streak resets under the configured policy rules.",
                    evidence=[
                        EvidenceItem(
                            signal="consecutive_losses",
                            timeframe="risk",
                            operator="gte",
                            observed_value=losses,
                            threshold=limit,
                            units="count",
                            source="risk_state.consecutive_losses",
                        )
                    ],
                )
            ]
        return []

    def run_cycle(self, loop_type: str, snapshot_bundle=None, loop_mode: str | None = None):
        log.info(f"--- Starting {loop_type} Cycle ---")
        execution_guards = self._build_execution_guards()
        loop_mode = loop_mode or self.active_loop_mode
        loop_lock_name = "trading_orchestrator_loop"
        loop_lock_owner = f"{loop_type}:{os.getpid()}:{int(time.time() * 1000)}"
        if not self.state_store.try_acquire_runtime_lock(
            loop_lock_name,
            owner=loop_lock_owner,
            ttl_seconds=execution_guards["loop_lock_ttl_seconds"],
        ):
            log.warning(f"Skipping {loop_type}: loop lock is already held by another worker.")
            return {
                "loop_type": loop_type,
                "trigger_active": self.active_loop_mode == "5m",
                "has_positions": True,
                "has_open_orders": True,
                "has_unknown_orders": True,
                "executed_order_count": 0,
                "position_closed": False,
                "final_account_state": None,
                "locked": True,
            }
        try:
            if snapshot_bundle is None:
                snapshot_bundle = self.snapshot_service.build_cycle_snapshot()

            account_state = snapshot_bundle.account_state
            market_snapshots = self._normalize_market_snapshots(snapshot_bundle.market_snapshots)
            market_prices = self._normalize_market_prices(snapshot_bundle.market_prices, market_snapshots)
            risk_state = self._enrich_risk_state_for_cycle(
                snapshot_bundle.risk_state,
                account_state=account_state,
                market_snapshots=market_snapshots,
                execution_guards=execution_guards,
            )
            policy_summary = snapshot_bundle.policy_summary
            is_trigger_mode = snapshot_bundle.is_trigger_mode
            risk_mode = snapshot_bundle.risk_mode
            risk_profile = snapshot_bundle.risk_profile
            regime_key = self._derive_regime_key(market_snapshots)
            active_bundle_summary = self._build_active_policy_bundle_summary(regime_key=regime_key)
            regime_drift_summary = self._build_regime_drift_summary(regime_key=regime_key, bundle_summary=active_bundle_summary)
            alpha_bundle_override = self._resolve_bundle_policy_config(active_bundle_summary, "alpha_1h")
            position_bundle_override = self._resolve_bundle_policy_config(active_bundle_summary, "position_15m")
            safety_bundle_override = self._resolve_bundle_policy_config(active_bundle_summary, "safety_5m")
            risk_state_payload = self._serialize_risk_state(risk_state)
            self._maybe_alert_risk_state_transitions(risk_state)
            self._maybe_alert_websocket_transitions()
            effective_risk_profile = self._build_effective_risk_profile(
                loop_mode,
                account_state=account_state,
                risk_profile=risk_profile,
                risk_mode=risk_mode,
                regime_key=regime_key,
            )
            decision_market_snapshots, alpha_policy_summary = self._prepare_decision_market_snapshots(
                account_state=account_state,
                market_snapshots=market_snapshots,
                loop_mode=loop_mode,
                regime_key=regime_key,
                alpha_policy_override=alpha_bundle_override,
            )

            # 3. Decision
            skip_reason = self._get_deterministic_skip_reason(
                account_state=account_state,
                market_snapshots=market_snapshots,
                risk_state=risk_state,
                risk_mode=risk_mode,
                risk_profile=effective_risk_profile,
            )
            if (
                not skip_reason
                and loop_mode == "1h"
                and not self._has_live_positions(account_state)
                and not self._has_live_open_orders(account_state)
                and not decision_market_snapshots
            ):
                skip_reason = "alpha_policy_filtered_all"
            decision_risk_state_payload = {
                **risk_state_payload,
                "alpha_policy_blocked_symbols": alpha_policy_summary.get("blocked_symbols") or {},
            }
            request = None
            position_policy_result = None
            safety_policy_result = None
            if loop_mode == "15m" and (
                self._has_live_positions(account_state) or self._has_live_open_orders(account_state)
            ):
                position_policy_result = self.position_policy_engine.evaluate(
                    account_state=account_state,
                    market_snapshots=market_snapshots,
                    risk_mode=risk_mode,
                    loop_mode=loop_mode,
                    regime_key=regime_key,
                    policy_override=position_bundle_override or None,
                )
            if loop_mode == "5m":
                safety_policy_result = self.safety_policy_engine.evaluate(
                    account_state=account_state,
                    market_snapshots=market_snapshots,
                    risk_mode=risk_mode,
                    loop_mode=loop_mode,
                    regime_key=regime_key,
                    policy_override=safety_bundle_override or None,
                )
            if safety_policy_result and not safety_policy_result.get("escalate_to_ai"):
                request = DecisionRequest(
                    policy_summary=policy_summary,
                    account_state=account_state,
                    market_snapshot=next(iter(decision_market_snapshots.values())) if len(decision_market_snapshots) == 1 else None,
                    market_snapshots=decision_market_snapshots,
                    risk_state=risk_state,
                    recent_journal_context=[],
                    long_term_memory=[],
                    similar_past_cases=[],
                    is_trigger_mode=is_trigger_mode,
                    risk_mode=risk_mode,
                    risk_profile=effective_risk_profile,
                    loop_mode=loop_mode,
                    cycle_role=effective_risk_profile.get("cycle_role"),
                )
                input_hash = self.ai_engine._build_candidate_input_hash(request)
                decision_result = self._build_policy_decision_result(
                    decision_payload=safety_policy_result.get("decision_result") or {},
                    input_hash=input_hash,
                    model_name="safety_fsm",
                )
            elif position_policy_result and not position_policy_result.get("escalate_to_ai"):
                request = DecisionRequest(
                    policy_summary=policy_summary,
                    account_state=account_state,
                    market_snapshot=next(iter(decision_market_snapshots.values())) if len(decision_market_snapshots) == 1 else None,
                    market_snapshots=decision_market_snapshots,
                    risk_state=risk_state,
                    recent_journal_context=[],
                    long_term_memory=[],
                    similar_past_cases=[],
                    is_trigger_mode=is_trigger_mode,
                    risk_mode=risk_mode,
                    risk_profile=effective_risk_profile,
                    loop_mode=loop_mode,
                    cycle_role=effective_risk_profile.get("cycle_role"),
                )
                input_hash = self.ai_engine._build_candidate_input_hash(request)
                decision_result = self._build_policy_decision_result(
                    decision_payload=position_policy_result.get("decision_result") or {},
                    input_hash=input_hash,
                    model_name="position_fsm",
                )
            elif skip_reason:
                request = DecisionRequest(
                    policy_summary=policy_summary,
                    account_state=account_state,
                    market_snapshot=next(iter(decision_market_snapshots.values())) if len(decision_market_snapshots) == 1 else None,
                    market_snapshots=decision_market_snapshots,
                    risk_state=decision_risk_state_payload,
                    recent_journal_context=[],
                    long_term_memory=[],
                    similar_past_cases=[],
                    is_trigger_mode=is_trigger_mode,
                    risk_mode=risk_mode,
                    risk_profile=effective_risk_profile,
                    loop_mode=loop_mode,
                    cycle_role=effective_risk_profile.get("cycle_role"),
                )
                input_hash = self.ai_engine._build_candidate_input_hash(request)
                log.info(f"Skipping AI decision call: {skip_reason}")
                rationale_items = self._build_skip_rationale_items(
                    skip_reason=skip_reason,
                    risk_state=decision_risk_state_payload,
                    market_snapshots=market_snapshots,
                    risk_profile=risk_profile,
                )
                decision_result = DecisionResult(
                    intent=TradeIntent(
                        decision="HOLD",
                        orders=[],
                        rationale=[f"Deterministic HOLD: {skip_reason}."],
                        rationale_items=rationale_items,
                        risk_notes=[skip_reason],
                        confidence=1.0,
                    ),
                    prompt="",
                    raw_response="",
                    model_name="deterministic_skip",
                    input_hash=input_hash,
                    cache_hit=False,
                    usage_metadata={"skipped": True, "reason": skip_reason},
                )
            else:
                context_budget = self._build_context_budget()
                raw_recent_journal_context = self.state_store.get_recent_journal_context(
                    limit=context_budget["journal_fetch_limit"]
                )
                raw_recent_trade_cases = self.state_store.get_recent_trade_cases(
                    limit=max(
                        context_budget["journal_fetch_limit"],
                        context_budget["journal_keep_limit"] * 4,
                    )
                )
                raw_long_term_memory = self.state_store.get_long_term_memory()
                recent_journal_context = self._select_recent_journal_context(
                    raw_recent_journal_context,
                    market_snapshots=decision_market_snapshots,
                    risk_mode=risk_mode,
                    limit=context_budget["journal_keep_limit"],
                    max_rationale_items=context_budget["max_rationale_items_per_journal"],
                    max_evidence_per_item=context_budget["max_evidence_per_rationale_item"],
                    trade_cases=raw_recent_trade_cases,
                    account_state=account_state,
                    risk_state=risk_state,
                )
                long_term_memory = self._select_long_term_memory_context(
                    raw_long_term_memory,
                    market_snapshots=decision_market_snapshots,
                    risk_mode=risk_mode,
                    limit=context_budget["memory_keep_limit"],
                    max_ignore_when=context_budget["max_ignore_when_per_memory"],
                )
                similar_past_cases = self._build_similar_past_cases(
                    decision_market_snapshots,
                    account_state=account_state,
                    risk_state=risk_state,
                    is_trigger_mode=is_trigger_mode,
                    limit=context_budget["similar_case_keep_limit"],
                )
                log.info(
                    "Context budget applied: journals {}->{}, trade_cases={}, memory {}->{}, similar_cases={}",
                    len(raw_recent_journal_context),
                    len(recent_journal_context),
                    len(raw_recent_trade_cases),
                    len(raw_long_term_memory),
                    len(long_term_memory),
                    len(similar_past_cases),
                )
                request = DecisionRequest(
                    policy_summary=policy_summary,
                    account_state=account_state,
                    market_snapshot=next(iter(decision_market_snapshots.values())) if len(decision_market_snapshots) == 1 else None,
                    market_snapshots=decision_market_snapshots,
                    risk_state=risk_state,
                    recent_journal_context=recent_journal_context,
                    long_term_memory=long_term_memory,
                    similar_past_cases=similar_past_cases,
                    is_trigger_mode=is_trigger_mode,
                    risk_mode=risk_mode,
                    risk_profile=effective_risk_profile,
                    loop_mode=loop_mode,
                    cycle_role=effective_risk_profile.get("cycle_role"),
                )
                decision_result = self.ai_engine.generate_decision(request)
            intent = decision_result.intent
            intent = self._enrich_intent_metadata(
                intent,
                input_hash=decision_result.input_hash,
                loop_mode=loop_mode,
                risk_mode=risk_mode,
                market_snapshots=market_snapshots,
            )
            intent, execution_policy_summary = self.execution_policy_engine.apply_to_intent(
                intent=intent,
                market_snapshots=market_snapshots,
                account_state=account_state,
                loop_mode=loop_mode,
                risk_mode=risk_mode,
                is_trigger_mode=is_trigger_mode,
            )

            # 4. Risk Evaluation
            unresolved_timeout_count = self.state_store.count_recent_unknown_orders(
                minutes=execution_guards["unknown_order_block_minutes"]
            )
            is_approved, reason = self.risk_engine.evaluate_intent(
                intent,
                account_state,
                market_snapshots=market_snapshots,
                market_prices=market_prices,
                risk_state=risk_state,
                is_trigger_mode=is_trigger_mode,
                unresolved_timeout_count=unresolved_timeout_count,
                active_profile_override=effective_risk_profile,
            )
            evidence_quality_summary = self.risk_engine.get_evidence_quality_summary(intent)
            entry_size_cap_summary = self.risk_engine.get_effective_max_order_notional_pct_balance(
                intent=intent,
                active_profile=effective_risk_profile,
                risk_state=risk_state,
                market_snapshots=market_snapshots,
            )

            # 5. Execution
            planned_orders = []
            receipts = []
            fills = []
            effective_pre_execution_state = account_state
            trigger_symbols = [
                symbol
                for symbol, snapshot in market_snapshots.items()
                if snapshot.is_opportunity_trigger
            ]
            risk_trigger_symbols = [
                symbol
                for symbol, snapshot in market_snapshots.items()
                if snapshot.is_risk_trigger
            ]
            execution_summary = {
                "loop_type": loop_type,
                "symbols": list(market_snapshots.keys()),
                "trigger_mode": is_trigger_mode,
                "trigger_symbols": trigger_symbols,
                "risk_trigger_symbols": risk_trigger_symbols,
                "risk_mode": risk_mode,
                "risk_profile": risk_profile,
                "effective_risk_profile": effective_risk_profile,
                "cycle_role": effective_risk_profile.get("cycle_role"),
                "risk_state": risk_state,
                "decision_input_hash": decision_result.input_hash,
                "ai_usage": decision_result.usage_metadata or {},
                "regime_key": regime_key,
                "policy_bundle": active_bundle_summary,
                "regime_drift": regime_drift_summary,
                "alpha_policy": alpha_policy_summary,
                "execution_policy": execution_policy_summary,
                "evidence_quality": evidence_quality_summary,
                "entry_size_cap": entry_size_cap_summary,
                "executed_order_count": 0,
                "unknown_order_states_before_execution": unresolved_timeout_count,
            }
            post_account_state = None
            if is_approved and intent.decision in ("BUY", "SELL", "REDUCE", "EXIT", "CANCEL", "REPLACE_ENTRY"):
                locked_entry_symbols = []
                try:
                    self.account_reader.invalidate_cache()
                    effective_pre_execution_state = self.account_reader.get_account_state(
                        self.symbols,
                        force_refresh=True,
                    )
                    unresolved_after_reconcile = self.state_store.count_recent_unknown_orders(
                        minutes=execution_guards["unknown_order_block_minutes"]
                    )
                    reapproved, rereason = self.risk_engine.evaluate_intent(
                        intent,
                        effective_pre_execution_state,
                        market_snapshots=market_snapshots,
                        market_prices=market_prices,
                        risk_state=risk_state,
                        is_trigger_mode=is_trigger_mode,
                        unresolved_timeout_count=unresolved_after_reconcile,
                        active_profile_override=effective_risk_profile,
                    )
                    execution_summary["pre_trade_reconciliation"] = {
                        "reapproved": bool(reapproved),
                        "reason": rereason,
                        "unknown_order_states": unresolved_after_reconcile,
                    }
                    if not reapproved:
                        is_approved = False
                        reason = f"Pre-trade reconciliation rejected: {rereason}"
                    else:
                        planned_orders = self.execution_engine.resolve_orders(
                            intent,
                            account_state=effective_pre_execution_state,
                        )
                        entry_orders = [] if intent.decision == "REPLACE_ENTRY" else self._extract_entry_orders(planned_orders)
                        fingerprint_entries = [
                            self._build_order_fingerprint(order, market_prices)
                            for order in entry_orders
                        ]
                        cooldown_minutes = execution_guards["decision_cooldown_minutes"]
                        repeated = []
                        for fp in fingerprint_entries:
                            if self.state_store.has_recent_decision_fingerprint(
                                symbol=fp["symbol"],
                                action=fp["action"],
                                size_band=fp["size_band"],
                                within_minutes=cooldown_minutes,
                            ):
                                repeated.append(f"{fp['symbol']}:{fp['action']}:{fp['size_band']}")
                        if repeated:
                            is_approved = False
                            reason = (
                                f"Decision cooldown active ({cooldown_minutes}m): repeated order fingerprint "
                                f"detected for {', '.join(sorted(repeated))}."
                            )
                            execution_summary["decision_cooldown_blocked"] = sorted(repeated)
                        else:
                            lock_ok, locked_entry_symbols = self._acquire_entry_mutexes(
                                entry_orders,
                                owner=loop_lock_owner,
                                ttl_seconds=execution_guards["entry_mutex_ttl_seconds"],
                            )
                            if not lock_ok:
                                is_approved = False
                                reason = "Entry mutex active for at least one symbol. Skipping duplicate entry execution."
                            else:
                                if fingerprint_entries:
                                    self.state_store.save_decision_fingerprints(
                                        [
                                            {
                                                **fp,
                                                "audit_log_id": None,
                                                "status": "planned",
                                            }
                                            for fp in fingerprint_entries
                                        ]
                                    )
                                log.info(f"Risk Approved. Executing {len(planned_orders)} orders.")
                                receipts = self.execution_engine.execute_intent(
                                    intent,
                                    account_state=effective_pre_execution_state,
                                )
                                execution_summary["executed_order_count"] = len(receipts)
                                execution_summary["receipt_summary"] = self.execution_engine.summarize_receipts(receipts)
                                fills = self.execution_engine.fetch_fills_for_receipts(receipts)
                                execution_summary["fill_summary"] = {
                                    "fill_count": len(fills),
                                    "fee_cost": round(sum(float(fill.get("fee_cost") or 0.0) for fill in fills), 8),
                                    "realized_pnl": round(sum(float(fill.get("realized_pnl") or 0.0) for fill in fills), 8),
                                }
                finally:
                    self._release_entry_mutexes(locked_entry_symbols, owner=loop_lock_owner)
                if is_approved and receipts:
                    try:
                        self.account_reader.invalidate_cache()
                        post_account_state = self.account_reader.get_account_state(
                            self.symbols,
                            force_refresh=True,
                        )
                        execution_summary["post_account_state"] = post_account_state.model_dump()
                        execution_summary["balance_delta"] = round(
                            post_account_state.total_balance - effective_pre_execution_state.total_balance,
                            8,
                        )
                        execution_summary["balance_delta_pct"] = self._calculate_balance_delta_pct(
                            effective_pre_execution_state.total_balance,
                            post_account_state.total_balance,
                        )
                    except Exception as execution_state_error:
                        execution_summary["post_account_state_error"] = str(execution_state_error)
                        log.warning(f"Failed to fetch post-execution account state: {execution_state_error}")
                unresolved = [
                    receipt for receipt in receipts
                    if receipt.get("status") == "timeout_unresolved"
                ]
                if unresolved:
                    unresolved_symbols = sorted(
                        {
                            receipt.get("symbol")
                            for receipt in unresolved
                            if receipt.get("symbol")
                        }
                    )
                    self.alert_manager.send(
                        "Execution Timeout",
                        (
                            f"{len(unresolved)} unresolved timed out order(s)"
                            f" for {', '.join(unresolved_symbols) if unresolved_symbols else 'portfolio'}."
                        ),
                        level="error",
                    )
            if not is_approved:
                log.warning(f"Risk Rejected: {reason}")
                if (
                    not self._is_daily_kill_switch_reason(reason)
                    and (
                        "Kill switch" in reason
                        or "Cooldown" in reason
                        or "unresolved order lifecycle" in reason.lower()
                        or "decision cooldown active" in reason.lower()
                    )
                ):
                    self.alert_manager.send(
                        "Risk Engine Blocked Trade",
                        f"Portfolio {intent.decision} rejected: {reason}",
                        level="warning",
                    )

            # 6. Logging
            estimated_pnl_pct = execution_summary.get("balance_delta_pct")
            if estimated_pnl_pct is None:
                estimated_pnl_pct = self._estimate_realized_pnl_pct(intent, effective_pre_execution_state)
            loss_signal = estimated_pnl_pct is not None and estimated_pnl_pct < 0
            account_payload = effective_pre_execution_state.model_dump(mode="json")
            risk_payload = self._serialize_risk_state(risk_state)
            audit_market = {
                "symbols": list(market_snapshots.keys()),
                "market_snapshots": {
                    symbol: snapshot.model_dump(mode="json")
                    for symbol, snapshot in market_snapshots.items()
                },
                "trigger_symbols": trigger_symbols,
                "risk_trigger_symbols": risk_trigger_symbols,
                "rag_context": {
                    "risk_state": risk_payload,
                    "account_state": account_payload,
                },
            }
            audit_id = self.state_store.save_audit(
                account=account_payload,
                market=audit_market,
                intent=intent.model_dump(),
                risk_ok=is_approved,
                reason=reason,
                receipts=receipts,
                policy_snapshot=policy_summary,
                decision_input_hash=decision_result.input_hash,
                risk_mode=risk_mode,
                risk_profile=risk_profile,
                decision_request=request.model_dump(),
                ai_raw_response=decision_result.raw_response,
                execution_summary=execution_summary,
                market_vectors=self.rag_engine.build_market_vectors(
                    audit_market["market_snapshots"],
                    account_state=account_payload,
                    risk_state=risk_payload,
                ),
            )
            if audit_id is not None:
                ledger_entries = []
                position_state_transitions = []
                if fills:
                    self.state_store.save_execution_fills(audit_id, fills)
                    ledger_entries = self._build_position_ledger_entries(intent, fills, execution_summary)
                    self.state_store.save_position_ledger_entries(audit_id, ledger_entries)
                if planned_orders:
                    order_state_transitions = self.execution_engine.build_order_state_transitions(
                        planned_orders,
                        receipts,
                        fills,
                    )
                    position_state_transitions = self.execution_engine.build_position_state_transitions(
                        intent,
                        effective_pre_execution_state,
                        post_account_state,
                        planned_orders,
                        receipts,
                    )
                    self.state_store.save_order_state_transitions(audit_id, order_state_transitions)
                    self.state_store.save_position_state_transitions(audit_id, position_state_transitions)
                symbol_journal_entries = self._build_symbol_journal_entries(
                    audit_log_id=audit_id,
                    intent=intent,
                    market_snapshots=market_snapshots,
                    trigger_symbols=trigger_symbols,
                    risk_trigger_symbols=risk_trigger_symbols,
                    risk_reason=reason,
                    risk_mode=risk_mode,
                    risk_approved=is_approved,
                    estimated_pnl_pct=estimated_pnl_pct,
                    ledger_entries=ledger_entries,
                )
                observed_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                symbol_journal_entries = self._annotate_trade_case_entries(
                    audit_log_id=audit_id,
                    symbol_journal_entries=symbol_journal_entries,
                    position_state_transitions=position_state_transitions,
                    intent=intent,
                    before_state=effective_pre_execution_state,
                    after_state=post_account_state,
                    market_snapshots=market_snapshots,
                    fills=fills,
                    risk_mode=risk_mode,
                    risk_state=risk_state,
                    observed_at=observed_at,
                )
                self._save_retrieval_evaluations_for_audit(
                    audit_log_id=audit_id,
                    intent=intent,
                    estimated_pnl_pct=estimated_pnl_pct,
                    risk_mode=risk_mode,
                    symbol_journal_entries=symbol_journal_entries,
                )
                self._backfill_retrieval_outcomes_for_symbol_entries(symbol_journal_entries)
                self._backfill_shadow_policy_outcomes_for_symbol_entries(symbol_journal_entries)
                execution_policy_evaluations = self._build_execution_policy_evaluation_entries(
                    audit_log_id=audit_id,
                    intent=intent,
                    planned_orders=planned_orders,
                    receipts=receipts,
                    fills=fills,
                    market_snapshots=market_snapshots,
                    symbol_journal_entries=symbol_journal_entries,
                    loop_mode=loop_mode,
                    risk_mode=risk_mode,
                    is_trigger_mode=is_trigger_mode,
                )
                if execution_policy_evaluations and hasattr(self.state_store, "save_execution_policy_evaluations"):
                    self.state_store.save_execution_policy_evaluations(
                        audit_id,
                        execution_policy_evaluations,
                    )
                self._record_shadow_policy_decisions(
                    audit_log_id=audit_id,
                    loop_mode=loop_mode,
                    live_intent=intent,
                    account_state=post_account_state or effective_pre_execution_state,
                    market_snapshots=market_snapshots,
                    risk_mode=risk_mode,
                    symbol_journal_entries=symbol_journal_entries,
                    regime_key=regime_key,
                )
                self.state_store.save_symbol_journal_entries(symbol_journal_entries)
                trade_case_snapshot_entries = self._build_trade_case_snapshot_entries(
                    audit_log_id=audit_id,
                    observed_at=observed_at,
                    account_state=post_account_state or effective_pre_execution_state,
                    market_snapshots=market_snapshots,
                    symbol_journal_entries=symbol_journal_entries,
                )
                self.state_store.save_trade_case_snapshots(trade_case_snapshot_entries)
            else:
                symbol_journal_entries = []
            trade_case_ids, case_statuses, case_summary = self._summarize_trade_cases(symbol_journal_entries)
            self.state_store.save_journal(
                intent.decision,
                intent.rationale,
                intent.confidence,
                audit_log_id=audit_id,
                risk_reason=reason,
                estimated_pnl_pct=estimated_pnl_pct,
                loss_signal=loss_signal,
                rationale_items=intent.rationale_items,
                trade_case_ids=trade_case_ids,
                case_statuses=case_statuses,
                case_summary=case_summary,
            )
            final_account_state = post_account_state or effective_pre_execution_state or account_state
            final_unknown_order_count = self.state_store.count_recent_unknown_orders(
                minutes=execution_guards["unknown_order_block_minutes"]
            )
            return {
                "loop_type": loop_type,
                "trigger_active": is_trigger_mode,
                "has_positions": self._has_live_positions(final_account_state),
                "has_open_orders": self._has_live_open_orders(final_account_state),
                "has_unknown_orders": final_unknown_order_count > 0,
                "executed_order_count": execution_summary.get("executed_order_count", 0),
                "position_closed": self._position_closed_during_cycle(
                    effective_pre_execution_state,
                    post_account_state,
                ),
                "final_account_state": final_account_state,
            }
                
        except Exception as e:
            log.error(f"Error in {loop_type} cycle: {e}")
            self.state_store.save_audit(
                account={},
                market={},
                intent={
                    "decision": "HOLD",
                    "orders": [],
                    "rationale": [f"Loop error during {loop_type}."],
                    "risk_notes": ["System Error"],
                    "confidence": 0.0,
                },
                risk_ok=False,
                reason=f"Error in {loop_type} cycle: {e}",
                receipts=[],
                execution_summary={"loop_type": loop_type, "error": str(e)},
            )
            self.alert_manager.send(
                "Trading Loop Error",
                f"{loop_type} cycle failed: {e}",
                level="error",
            )
            return {
                "loop_type": loop_type,
                "trigger_active": self.active_loop_mode == "5m",
                "has_positions": True,
                "has_open_orders": True,
                "has_unknown_orders": True,
                "executed_order_count": 0,
                "position_closed": False,
                "final_account_state": None,
                "error": str(e),
            }
        finally:
            self.state_store.release_runtime_lock(loop_lock_name, owner=loop_lock_owner)

    def run_recovery_sweep(self):
        log.info("Running unresolved order recovery sweep...")
        try:
            unknown_rows = self.state_store.get_unknown_order_runtime_states(limit=100)
            if not unknown_rows:
                log.info("Recovery sweep: no unknown runtime orders.")
                return
            recovered_count = 0
            for row in unknown_rows:
                symbol = row.get("symbol")
                client_order_id = row.get("client_order_id")
                if not symbol or not client_order_id:
                    continue
                recovered_order = self.execution_engine._recover_timed_out_order(symbol, client_order_id)
                if not recovered_order:
                    continue
                to_state = self._resolve_runtime_state_from_recovered_order(recovered_order)
                self.state_store.save_order_state_transitions(
                    audit_log_id=int(row.get("last_audit_log_id") or 0),
                    entries=[
                        {
                            "symbol": symbol,
                            "client_order_id": client_order_id,
                            "exchange_order_id": recovered_order.get("id") or (recovered_order.get("info") or {}).get("orderId"),
                            "from_state": "unknown",
                            "to_state": to_state,
                            "event_type": "recovery_sweep",
                            "payload": {"recovered_order": recovered_order},
                        }
                    ],
                )
                recovered_count += 1
            if recovered_count > 0:
                self.alert_manager.send(
                    "Recovery Sweep",
                    f"Recovered {recovered_count} unknown order lifecycle state(s).",
                    level="info",
                )
            log.info(
                "Recovery sweep finished: unknown_rows={}, recovered={}",
                len(unknown_rows),
                recovered_count,
            )
        except Exception as e:
            log.error(f"Recovery sweep failed: {e}")
            self.alert_manager.send(
                "Recovery Sweep Error",
                f"Recovery sweep failed: {e}",
                level="warning",
            )

    def run_daily_batch(self):
        replay_config = self._build_replay_batch_config()
        if replay_config["enabled"]:
            log.info("Running incremental replay batch before daily memory updater...")
            try:
                replay_args = [
                    sys.executable,
                    "tools/replay_audit.py",
                    "--auto",
                    "--limit",
                    str(replay_config["limit"]),
                    "--window-hours",
                    str(replay_config["window_hours"]),
                ]
                if replay_config["save"]:
                    replay_args.append("--save")
                subprocess.run(replay_args, check=True)
                log.info("Incremental replay batch finished successfully.")
            except Exception as e:
                log.error(f"Incremental replay batch failed: {e}")
        log.info("Running daily memory updater batch...")
        try:
            # Reuse the current interpreter so the batch job sees the same virtualenv.
            subprocess.run([sys.executable, "batch/memory_updater.py"], check=True)
            log.info("Daily batch finished successfully.")
        except Exception as e:
            log.error(f"Daily batch failed: {e}")

    def start_scheduler(self):
        execution_guards = self._build_execution_guards()
        log.info(
            "Adaptive scheduler started. Cadence minutes: 1h={} 15m={} 5m={} snapshot={}m recovery={}m case_path={}s",
            self.adaptive_loop_config["base_loop_minutes"],
            self.adaptive_loop_config["position_loop_minutes"],
            self.adaptive_loop_config["trigger_loop_minutes"],
            self.snapshot_service.materialization_minutes,
            execution_guards.get("recovery_sweep_minutes", 5),
            self.adaptive_loop_config["trade_case_snapshot_poll_seconds"],
        )
        next_snapshot_at = time.time()
        next_cycle_at = time.time()
        while True:
            now_epoch = time.time()
            self._run_due_maintenance_tasks(now_epoch)
            self._sample_trade_case_paths_if_due(now_epoch)
            self._maybe_alert_websocket_transitions()
            if now_epoch >= next_snapshot_at:
                snapshot_bundle = self.snapshot_service.materialize(now_epoch=now_epoch)
                self._maybe_alert_risk_state_transitions(snapshot_bundle.risk_state)
                next_snapshot_at = self.snapshot_service.next_materialization_epoch(now_epoch)
            if now_epoch >= next_cycle_at:
                current_mode = self.active_loop_mode
                loop_type = self._loop_label_for_mode(current_mode)
                snapshot_bundle = self.snapshot_service.materialize(now_epoch=now_epoch)
                cycle_result = self.run_cycle(
                    loop_type,
                    snapshot_bundle=snapshot_bundle,
                    loop_mode=current_mode,
                )
                cycle_finished_at = time.time()
                self._refresh_runtime_activity(cycle_result, cycle_finished_at)
                desired_mode = self._determine_desired_loop_mode(cycle_result)
                active_mode = self._apply_adaptive_loop_mode(
                    desired_mode,
                    cycle_finished_at,
                    cycle_result=cycle_result,
                )
                next_cycle_at = self._next_aligned_epoch(
                    self._loop_minutes_for_mode(active_mode),
                    cycle_finished_at,
                )
                log.info(
                    "Next cycle scheduled for epoch {} with active cadence {}",
                    int(next_cycle_at),
                    active_mode,
                )
                continue
            sleep_seconds = min(
                self.adaptive_loop_config["poll_seconds"],
                max(0.1, min(next_cycle_at, next_snapshot_at) - now_epoch),
            )
            time.sleep(sleep_seconds)

if __name__ == "__main__":
    orchestrator = TradingOrchestrator()
    orchestrator.start_scheduler()
