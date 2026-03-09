class TradingExecutionLedgerMixin:
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
