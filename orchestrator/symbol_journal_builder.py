class TradingSymbolJournalBuilderMixin:
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
            symbol_order_actions.setdefault(order.symbol, []).append(action_text)

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
