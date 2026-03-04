import os
import subprocess
import sys
import time

import schedule
from dotenv import load_dotenv

from ai.decision_engine import DecisionEngine
from ai.rag_engine import RagEngine
from core.models import DecisionRequest
from core.risk_engine import RiskEngine
from db.state_store import StateStore
from exchange.account_reader import AccountReader
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
        self.testnet = os.getenv("BYBIT_TESTNET", "True").lower() == "true"
        self.gemini_key = os.getenv("GEMINI_API_KEY")
        self.db_path = os.getenv("DB_PATH", "data/gpt_trader.db")

        if not all([self.api_key, self.api_secret, self.gemini_key]):
            log.error("Missing required environment variables. Please check .env")
            exit(1)

        self.state_store = StateStore(self.db_path)
        self.risk_engine = RiskEngine()
        self.rag_engine = RagEngine(self.db_path)
        
        self.account_reader = AccountReader(self.api_key, self.api_secret, self.testnet)
        self.market_collector = MarketCollector(self.account_reader.exchange)
        self.execution_engine = ExecutionEngine(self.account_reader.exchange)
        
        self.ai_engine = DecisionEngine(self.gemini_key)
        self.alert_manager = AlertManager()
        
        self.symbols = self.risk_engine.policy.get('trading', {}).get('symbols', ['BTCUSDT'])

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

    def run_cycle(self, loop_type: str):
        log.info(f"--- Starting {loop_type} Cycle ---")
        try:
            # 1. Fetch State
            account_state = self.account_reader.get_account_state(self.symbols)
            cooldown_minutes = self.risk_engine.policy.get("risk_limits", {}).get("cooldown_minutes", 0)
            
            for symbol in self.symbols:
                log.info(f"Processing {symbol}")
                market_snapshot = self.market_collector.get_market_snapshot(symbol)
                risk_state = self.state_store.get_risk_state(
                    current_balance=account_state.total_balance,
                    cooldown_minutes=cooldown_minutes,
                )
                
                # 2. Prepare Context
                recent_journals = self.state_store.get_recent_journals(limit=10)
                long_term_memory = self.state_store.get_long_term_memory()
                policy_summary = self.risk_engine.get_summary()

                # Trigger detection (Force trigger mode if it's the 5m loop or snapshot flags it)
                is_trigger_mode = market_snapshot.is_trigger_event or ("Trigger" in loop_type)
                
                # Fetch RAG cases if in Trigger mode to help decision making
                similar_past_cases = []
                if is_trigger_mode:
                    similar_past_cases = self.rag_engine.get_similar_cases(market_snapshot.model_dump(), limit=3)

                request = DecisionRequest(
                    policy_summary=policy_summary,
                    account_state=account_state,
                    market_snapshot=market_snapshot,
                    recent_journal_summaries=recent_journals,
                    long_term_memory=long_term_memory,
                    similar_past_cases=similar_past_cases,
                    is_trigger_mode=is_trigger_mode
                )

                # 3. Decision
                decision_result = self.ai_engine.generate_decision(request)
                intent = decision_result.intent
                
                # 4. Risk Evaluation
                market_prices = {market_snapshot.symbol: market_snapshot.current_price}
                is_approved, reason = self.risk_engine.evaluate_intent(
                    intent,
                    account_state,
                    market_prices=market_prices,
                    risk_state=risk_state,
                )
                
                # 5. Execution
                receipts = []
                fills = []
                execution_summary = {
                    "loop_type": loop_type,
                    "trigger_mode": is_trigger_mode,
                    "risk_state": risk_state,
                    "executed_order_count": 0,
                }
                post_account_state = None
                if is_approved and intent.decision in ("BUY", "SELL", "REDUCE", "EXIT"):
                    log.info(f"Risk Approved. Executing {len(intent.orders)} orders.")
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
                        post_account_state = self.account_reader.get_account_state(self.symbols)
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
                        self.alert_manager.send(
                            "Execution Timeout",
                            f"{symbol} has {len(unresolved)} unresolved timed out order(s).",
                            level="error",
                        )
                elif not is_approved:
                    log.warning(f"Risk Rejected: {reason}")
                    if "Kill switch" in reason or "Cooldown" in reason:
                        self.alert_manager.send(
                            "Risk Engine Blocked Trade",
                            f"{symbol} {intent.decision} rejected: {reason}",
                            level="warning",
                        )
                
                # 6. Logging
                estimated_pnl_pct = execution_summary.get("balance_delta_pct")
                if estimated_pnl_pct is None:
                    estimated_pnl_pct = self._estimate_realized_pnl_pct(intent, account_state)
                loss_signal = estimated_pnl_pct is not None and estimated_pnl_pct < 0
                audit_id = self.state_store.save_audit(
                    account=account_state.model_dump(),
                    market=market_snapshot.model_dump(),
                    intent=intent.model_dump(),
                    risk_ok=is_approved,
                    reason=reason,
                    receipts=receipts,
                    policy_snapshot=policy_summary,
                    decision_request=request.model_dump(),
                    ai_raw_response=decision_result.raw_response,
                    execution_summary=execution_summary,
                )
                if audit_id is not None and fills:
                    self.state_store.save_execution_fills(audit_id, fills)
                    ledger_entries = self._build_position_ledger_entries(intent, fills, execution_summary)
                    self.state_store.save_position_ledger_entries(audit_id, ledger_entries)
                self.state_store.save_journal(
                    intent.decision,
                    intent.rationale,
                    intent.confidence,
                    risk_reason=reason,
                    estimated_pnl_pct=estimated_pnl_pct,
                    loss_signal=loss_signal,
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
