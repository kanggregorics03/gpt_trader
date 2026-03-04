import os
import subprocess
import sys
import time

import schedule
from dotenv import load_dotenv

from ai.decision_engine import DecisionEngine, DecisionResult
from ai.rag_engine import RagEngine
from core.models import DecisionRequest, EvidenceItem, RationaleItem, TradeIntent
from core.risk_engine import RiskEngine
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
    def __init__(self):
        log.info("Initializing Trading Orchestrator...")
        self.api_key = os.getenv("BYBIT_API_KEY")
        self.api_secret = os.getenv("BYBIT_API_SECRET")
        self.trading_mode = normalize_trading_mode(
            os.getenv("BYBIT_TRADING_MODE"),
            os.getenv("BYBIT_TESTNET"),
        )
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        self.gemini_default_model = os.getenv("GEMINI_DEFAULT_MODEL") or os.getenv("GEMINI_MODEL")
        self.gemini_trigger_model = os.getenv("GEMINI_TRIGGER_MODEL")
        self.db_path = os.getenv("DB_PATH", "data/gpt_trader.db")

        if not all([self.api_key, self.api_secret, self.gemini_key]):
            log.error("Missing required environment variables. Please check .env")
            exit(1)

        self.state_store = StateStore(self.db_path)
        self.risk_engine = RiskEngine()
        self.rag_engine = RagEngine(self.db_path)
        self.symbols = self.risk_engine.policy.get('trading', {}).get('symbols', ['BTCUSDT'])

        self.private_exchange = create_private_exchange(
            self.api_key,
            self.api_secret,
            trading_mode=self.trading_mode,
        )
        self.public_exchange = create_public_exchange()
        log.info(
            f"Bybit exchange routing: market_data=mainnet, private_trading={self.trading_mode}"
        )
        self.public_feed = create_public_market_feed(self.symbols)
        self.private_account_feed = create_private_account_feed(
            self.api_key,
            self.api_secret,
            trading_mode=self.trading_mode,
        )

        self.account_reader = AccountReader(
            self.private_exchange,
            trading_mode=self.trading_mode,
            private_feed=self.private_account_feed,
        )
        self.market_collector = MarketCollector(self.public_exchange, public_feed=self.public_feed)
        self.execution_engine = ExecutionEngine(
            self.private_exchange,
            private_feed=self.private_account_feed,
        )
        
        if self.gemini_default_model or self.gemini_trigger_model:
            self.ai_engine = DecisionEngine(
                self.gemini_key,
                cache_store=self.state_store,
                default_model=self.gemini_default_model,
                trigger_model=self.gemini_trigger_model,
            )
        else:
            self.ai_engine = DecisionEngine(self.gemini_key, cache_store=self.state_store)
        log.info(
            f"Gemini models: default={self.ai_engine.flash_model}, trigger={self.ai_engine.pro_model}"
        )
        self.alert_manager = AlertManager()

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

    def _build_similar_past_cases(self, market_snapshots, is_trigger_mode: bool):
        if not is_trigger_mode:
            return []

        current_markets = {
            symbol: snapshot.model_dump(mode="json")
            for symbol, snapshot in market_snapshots.items()
        }
        batch_cases = self.rag_engine.get_similar_cases_batch(
            current_markets,
            limit_per_symbol=3,
        )

        cases = []
        seen = set()
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
                cases.append(tagged_case)
        return cases

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

        risk_limits = self.risk_engine.policy.get("risk_limits", {})
        daily_max_loss_pct = float(risk_limits.get("daily_max_loss_pct", 0.0) or 0.0)
        daily_pnl_pct = float(risk_state.get("daily_pnl_pct", 0.0) or 0.0)
        if daily_max_loss_pct > 0 and daily_pnl_pct <= -daily_max_loss_pct:
            return "daily_loss_kill_switch"

        max_consecutive_losses = int(risk_limits.get("max_consecutive_losses", 0) or 0)
        consecutive_losses = int(risk_state.get("consecutive_losses", 0) or 0)
        if max_consecutive_losses > 0 and consecutive_losses >= max_consecutive_losses:
            return "consecutive_loss_kill_switch"

        if bool(risk_state.get("cooldown_active")):
            return "cooldown_active"

        if market_snapshots and all(
            "wide_spread" in snapshot.risk_trigger_reasons
            for snapshot in market_snapshots.values()
        ):
            return "all_symbols_wide_spread"

        allowed_actions = {str(action).upper() for action in risk_profile.get("allowed_actions", [])}
        if "BUY" not in allowed_actions and "SELL" not in allowed_actions:
            return f"risk_profile_{risk_mode}_blocks_entries"

        return None

    def _build_skip_rationale_items(self, skip_reason: str, risk_state, market_snapshots, risk_profile):
        if skip_reason == "cooldown_active":
            remaining = float(risk_state.get("cooldown_remaining_minutes", 0) or 0.0)
            return [
                RationaleItem(
                    summary="Cooldown blocks new entries.",
                    why_it_matters="The strategy is in mandatory cooldown after recent losses.",
                    trade_implication="Keep the portfolio flat until cooldown expires.",
                    failure_mode="Cooldown expires and no other blocking risk state remains.",
                    evidence=[
                        EvidenceItem(
                            signal="cooldown_remaining_minutes",
                            timeframe="risk",
                            operator="gt",
                            observed_value=remaining,
                            threshold=0.0,
                            units="minutes",
                            source="risk_state.cooldown_remaining_minutes",
                        )
                    ],
                )
            ]
        if skip_reason == "all_symbols_wide_spread":
            first_symbol = next(iter(market_snapshots.keys()), None)
            spread = 0.0
            source = "market_snapshot.order_book.spread_bps"
            if first_symbol and first_symbol in market_snapshots:
                spread = float(market_snapshots[first_symbol].order_book.spread_bps or 0.0)
                source = f"market_snapshots.{first_symbol}.order_book.spread_bps"
            return [
                RationaleItem(
                    summary="Execution quality is too poor for new entries.",
                    why_it_matters="Wide spreads materially increase slippage and degrade entry quality.",
                    trade_implication="Do not open new positions while spreads stay elevated.",
                    failure_mode="Spread compresses below the risk threshold.",
                    evidence=[
                        EvidenceItem(
                            signal="spread_bps",
                            symbol=first_symbol,
                            timeframe="microstructure",
                            operator="gte",
                            observed_value=spread,
                            threshold=10.0,
                            units="bps",
                            source=source,
                        )
                    ],
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
            daily_pnl_pct = float(risk_state.get("daily_pnl_pct", 0.0) or 0.0)
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
            losses = float(risk_state.get("consecutive_losses", 0) or 0.0)
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

    def run_cycle(self, loop_type: str):
        log.info(f"--- Starting {loop_type} Cycle ---")
        try:
            # 1. Fetch State
            account_state = self.account_reader.get_account_state(self.symbols)
            risk_limits = self.risk_engine.policy.get("risk_limits", {})
            cooldown_minutes = risk_limits.get("cooldown_minutes", 0)
            drawdown_activate_pct = risk_limits.get("drawdown_activate_pct")
            if drawdown_activate_pct is None:
                daily_max_loss_pct = float(risk_limits.get("daily_max_loss_pct", 0.0) or 0.0)
                drawdown_activate_pct = round(daily_max_loss_pct * 0.5, 4) if daily_max_loss_pct > 0 else None
            risk_state = self.state_store.get_risk_state(
                current_balance=account_state.total_balance,
                cooldown_minutes=cooldown_minutes,
                drawdown_activate_pct=drawdown_activate_pct,
            )
            recent_journal_context = self.state_store.get_recent_journal_context(limit=10)
            long_term_memory = self.state_store.get_long_term_memory()
            policy_summary = self.risk_engine.get_summary()

            market_snapshots = {}
            market_prices = {}
            for symbol in self.symbols:
                log.info(f"Processing {symbol}")
                trigger_state = self.state_store.get_symbol_trigger_state(symbol)
                market_snapshot = self.market_collector.get_market_snapshot(symbol, trigger_state=trigger_state)
                self.state_store.save_symbol_trigger_state(
                    symbol,
                    self.market_collector.build_next_trigger_state(market_snapshot),
                )
                market_snapshots[symbol] = market_snapshot
                market_prices[symbol] = market_snapshot.current_price

            is_trigger_mode = any(
                snapshot.is_opportunity_trigger for snapshot in market_snapshots.values()
            ) or ("Trigger" in loop_type)
            risk_mode = self.risk_engine.get_active_risk_mode(
                risk_state=risk_state,
                is_trigger_mode=is_trigger_mode,
            )
            risk_profile = self.risk_engine.get_active_risk_profile(
                risk_state=risk_state,
                is_trigger_mode=is_trigger_mode,
            )
            similar_past_cases = self._build_similar_past_cases(
                market_snapshots,
                is_trigger_mode=is_trigger_mode,
            )

            request = DecisionRequest(
                policy_summary=policy_summary,
                account_state=account_state,
                market_snapshot=next(iter(market_snapshots.values())) if len(market_snapshots) == 1 else None,
                market_snapshots=market_snapshots,
                risk_state=risk_state,
                recent_journal_context=recent_journal_context,
                long_term_memory=long_term_memory,
                similar_past_cases=similar_past_cases,
                is_trigger_mode=is_trigger_mode,
                risk_mode=risk_mode,
                risk_profile=risk_profile,
            )

            # 3. Decision
            skip_reason = self._get_deterministic_skip_reason(
                account_state=account_state,
                market_snapshots=market_snapshots,
                risk_state=risk_state,
                risk_mode=risk_mode,
                risk_profile=risk_profile,
            )
            if skip_reason:
                input_hash = self.ai_engine._build_candidate_input_hash(request)
                log.info(f"Skipping AI decision call: {skip_reason}")
                rationale_items = self._build_skip_rationale_items(
                    skip_reason=skip_reason,
                    risk_state=risk_state,
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
                decision_result = self.ai_engine.generate_decision(request)
            intent = decision_result.intent

            # 4. Risk Evaluation
            is_approved, reason = self.risk_engine.evaluate_intent(
                intent,
                account_state,
                market_snapshots=market_snapshots,
                market_prices=market_prices,
                risk_state=risk_state,
                is_trigger_mode=is_trigger_mode,
            )
            evidence_quality_summary = self.risk_engine.get_evidence_quality_summary(intent)
            entry_size_cap_summary = self.risk_engine.get_effective_max_order_notional_pct_balance(
                intent=intent,
                active_profile=risk_profile,
            )

            # 5. Execution
            planned_orders = []
            receipts = []
            fills = []
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
                "risk_state": risk_state,
                "decision_input_hash": decision_result.input_hash,
                "ai_usage": decision_result.usage_metadata or {},
                "evidence_quality": evidence_quality_summary,
                "entry_size_cap": entry_size_cap_summary,
                "executed_order_count": 0,
            }
            post_account_state = None
            if is_approved and intent.decision in ("BUY", "SELL", "REDUCE", "EXIT"):
                planned_orders = self.execution_engine.resolve_orders(intent, account_state=account_state)
                log.info(f"Risk Approved. Executing {len(planned_orders)} orders.")
                receipts = self.execution_engine.execute_intent(intent, account_state=account_state)
                execution_summary["executed_order_count"] = len(receipts)
                execution_summary["receipt_summary"] = self.execution_engine.summarize_receipts(receipts)
                fills = self.execution_engine.fetch_fills_for_receipts(receipts)
                execution_summary["fill_summary"] = {
                    "fill_count": len(fills),
                    "fee_cost": round(sum(float(fill.get("fee_cost") or 0.0) for fill in fills), 8),
                    "realized_pnl": round(sum(float(fill.get("realized_pnl") or 0.0) for fill in fills), 8),
                }
                try:
                    self.account_reader.invalidate_cache()
                    post_account_state = self.account_reader.get_account_state(
                        self.symbols,
                        force_refresh=True,
                    )
                    execution_summary["post_account_state"] = post_account_state.model_dump()
                    execution_summary["balance_delta"] = round(
                        post_account_state.total_balance - account_state.total_balance,
                        8,
                    )
                    execution_summary["balance_delta_pct"] = self._calculate_balance_delta_pct(
                        account_state.total_balance,
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
            elif not is_approved:
                log.warning(f"Risk Rejected: {reason}")
                if "Kill switch" in reason or "Cooldown" in reason:
                    self.alert_manager.send(
                        "Risk Engine Blocked Trade",
                        f"Portfolio {intent.decision} rejected: {reason}",
                        level="warning",
                    )

            # 6. Logging
            estimated_pnl_pct = execution_summary.get("balance_delta_pct")
            if estimated_pnl_pct is None:
                estimated_pnl_pct = self._estimate_realized_pnl_pct(intent, account_state)
            loss_signal = estimated_pnl_pct is not None and estimated_pnl_pct < 0
            audit_market = {
                "symbols": list(market_snapshots.keys()),
                "market_snapshots": {
                    symbol: snapshot.model_dump(mode="json")
                    for symbol, snapshot in market_snapshots.items()
                },
                "trigger_symbols": trigger_symbols,
                "risk_trigger_symbols": risk_trigger_symbols,
            }
            audit_id = self.state_store.save_audit(
                account=account_state.model_dump(),
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
                market_vectors=self.rag_engine.build_market_vectors(audit_market["market_snapshots"]),
            )
            if audit_id is not None:
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
                        account_state,
                        post_account_state,
                        planned_orders,
                        receipts,
                    )
                    self.state_store.save_order_state_transitions(audit_id, order_state_transitions)
                    self.state_store.save_position_state_transitions(audit_id, position_state_transitions)
            self.state_store.save_journal(
                intent.decision,
                intent.rationale,
                intent.confidence,
                audit_log_id=audit_id,
                risk_reason=reason,
                estimated_pnl_pct=estimated_pnl_pct,
                loss_signal=loss_signal,
                rationale_items=intent.rationale_items,
            )
                
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

    def run_daily_batch(self):
        log.info("Running daily memory updater batch...")
        try:
            # Reuse the current interpreter so the batch job sees the same virtualenv.
            subprocess.run([sys.executable, "batch/memory_updater.py"], check=True)
            log.info("Daily batch finished successfully.")
        except Exception as e:
            log.error(f"Daily batch failed: {e}")

    def start_scheduler(self):
        intervals = self.risk_engine.policy.get('intervals', {})
        strategy_freq = intervals.get('strategy_loop_minutes', 60)
        position_freq = intervals.get('position_loop_minutes', 15)
        trigger_freq = intervals.get('trigger_loop_minutes', 5)

        schedule.every(strategy_freq).minutes.do(self.run_cycle, "Strategy (1h)")
        schedule.every(position_freq).minutes.do(self.run_cycle, "Position (15m)")
        schedule.every(trigger_freq).minutes.do(self.run_cycle, "Trigger (5m)")
        
        # Schedule the daily long-term memory aggregation at 04:00 KST
        # If server is in UTC, this should be adjusted. Assuming local time is KST.
        schedule.every().day.at("04:00").do(self.run_daily_batch)

        log.info(f"Scheduler started. Loop intervals: {strategy_freq}m, {position_freq}m, {trigger_freq}m")
        
        # Run once immediately on startup
        self.run_cycle("Startup")

        while True:
            schedule.run_pending()
            time.sleep(1)

if __name__ == "__main__":
    orchestrator = TradingOrchestrator()
    orchestrator.start_scheduler()
