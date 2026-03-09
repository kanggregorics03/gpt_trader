class TradingExecutionMetricsMixin:
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
