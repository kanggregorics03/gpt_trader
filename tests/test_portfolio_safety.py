from unittest.mock import Mock

import ccxt
import pytest

from core.models import AccountState, OrderIntent, TradeIntent
from core.risk_engine import RiskEngine
from db.state_store import StateStore
from exchange.bybit_factory import create_private_exchange, normalize_trading_mode
from exchange.execution_engine import ExecutionEngine


def test_implicit_trading_mode_is_demo():
    assert normalize_trading_mode() == "demo"
    assert normalize_trading_mode(None, "false") == "demo"
    assert create_private_exchange("fake", "fake").options["enableDemoTrading"]


def test_private_exchange_rejects_typo_in_mode():
    with pytest.raises(ValueError):
        create_private_exchange("fake", "fake", trading_mode="dmeo")


def test_old_unknown_order_blocks_until_confirmed_recovery(tmp_path):
    store = StateStore(str(tmp_path / "test.db"))
    try:
        entry = dict(
            symbol="BTCUSDT",
            client_order_id="fake-client",
            exchange_order_id=None,
            from_state="submitted",
            to_state="unknown",
            event_type="timeout",
            payload={},
        )
        store.save_order_state_transitions(1, [entry])
        with store.conn:
            store.conn.execute(
                "UPDATE order_runtime_state SET updated_at = '2000-01-01 00:00:00'"
            )
        assert store.count_unknown_orders() == 1
        assert (
            store.count_recent_unknown_orders(minutes=30) == 0
        )  # monitoring window only
        store.save_order_state_transitions(
            1, [{**entry, "from_state": "unknown", "to_state": "completed"}]
        )
        assert store.count_unknown_orders() == 0
    finally:
        store.close()


@pytest.mark.parametrize("decision", ["BUY", "SELL", "REPLACE_ENTRY"])
def test_unknown_order_blocks_all_entry_decisions(decision):
    intent = TradeIntent(
        decision=decision, orders=[], rationale=["test"], risk_notes=[], confidence=0.5
    )
    account = AccountState(
        total_balance=1000, available_balance=1000, positions=[], open_orders_count=0
    )
    approved, reason = RiskEngine().evaluate_intent(
        intent, account, unresolved_timeout_count=1
    )
    assert not approved
    assert "remain unknown" in reason


@pytest.mark.parametrize("error", [ccxt.RequestTimeout, ccxt.NetworkError])
def test_submission_network_failure_recovers_partial_fill_without_resubmission(error):
    exchange = Mock()
    exchange.has = {"fetchClosedOrders": True}
    exchange.create_order.side_effect = error("fake network failure")

    def orders(symbol):
        client_id = exchange.create_order.call_args.kwargs["params"]["clientOrderId"]
        return [
            dict(
                id="fake-order",
                clientOrderId=client_id,
                symbol=symbol,
                status="open",
                filled=0.25,
            )
        ]

    exchange.fetch_open_orders.side_effect = orders
    engine = ExecutionEngine(exchange)
    order = OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=1)
    receipt = engine._execute_single_order(order)
    assert receipt["status"] == "recovered"
    assert receipt["id"] == "fake-order"
    assert receipt["filled"] == 0.25
    assert (
        engine.build_order_state_transitions([order], [receipt], [])[-1]["to_state"]
        == "partial_filled"
    )
    exchange.create_order.assert_called_once()


def test_ws_failure_falls_back_to_rest_and_rest_failure_keeps_unknown():
    exchange = Mock()
    exchange.has = {"fetchClosedOrders": True}
    exchange.create_order.side_effect = ccxt.RequestTimeout("fake timeout")
    exchange.fetch_open_orders.side_effect = ccxt.ExchangeNotAvailable("fake outage")
    exchange.fetch_closed_orders.return_value = []
    feed = Mock()
    feed.get_health_status.return_value = "healthy"
    feed.get_recent_orders.side_effect = RuntimeError("fake cache failure")
    receipt = ExecutionEngine(exchange, feed)._execute_single_order(
        OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=1)
    )
    assert receipt["status"] == "timeout_unresolved"
    assert receipt["clientOrderId"]
    exchange.fetch_closed_orders.assert_called_once()
    exchange.create_order.assert_called_once()


def test_stale_ws_cache_does_not_override_confirmed_rest_order():
    exchange = Mock()
    exchange.has = {"fetchClosedOrders": True}
    exchange.fetch_open_orders.return_value = []
    exchange.fetch_closed_orders.return_value = [
        {"clientOrderId": "fake-client", "status": "closed"}
    ]
    feed = Mock()
    feed.get_health_status.return_value = "stale"
    feed.get_recent_orders.return_value = [
        {"clientOrderId": "fake-client", "status": "open"}
    ]
    result = ExecutionEngine(exchange, feed)._recover_timed_out_order(
        "BTCUSDT", "fake-client"
    )
    assert result["status"] == "closed"
    feed.get_recent_orders.assert_not_called()


def test_ambiguous_order_defers_remaining_entries_in_same_batch():
    exchange = Mock()
    exchange.has = {"fetchClosedOrders": True}
    exchange.create_order.side_effect = ccxt.RequestTimeout("fake timeout")
    exchange.fetch_open_orders.return_value = []
    exchange.fetch_closed_orders.return_value = []
    orders = [
        OrderIntent(symbol="BTCUSDT", action="BUY", order_type="MARKET", quantity=1)
        for _ in range(2)
    ]
    engine = ExecutionEngine(exchange)
    receipts = engine.execute_intent(
        TradeIntent(
            decision="BUY",
            orders=orders,
            rationale=["test"],
            risk_notes=[],
            confidence=0.5,
        )
    )
    assert [r["status"] for r in receipts] == [
        "timeout_unresolved",
        "deferred_unknown_order",
    ]
    assert (
        engine.build_order_state_transitions(orders, receipts, [])[-1]["to_state"]
        == "deferred"
    )
    exchange.create_order.assert_called_once()


@pytest.mark.parametrize(
    "status,filled", [("filled", 1), ("closed", 1), ("canceled", 0.25)]
)
def test_replacement_is_deferred_when_cancel_target_has_fills(status, filled):
    exchange = Mock()
    exchange.cancel_order.return_value = dict(
        id="fake-old", symbol="BTCUSDT", status=status, filled=filled
    )
    orders = [
        OrderIntent(
            symbol="BTCUSDT",
            action="CANCEL",
            order_type="MARKET",
            quantity=0,
            target_order_id="fake-old",
            replace_group_id="fake-group",
            lineage_role="cancel",
        ),
        OrderIntent(
            symbol="BTCUSDT",
            action="BUY",
            order_type="LIMIT",
            quantity=1,
            price=100,
            replace_group_id="fake-group",
            lineage_role="replacement",
            wait_for_cancel_confirmation=True,
        ),
    ]
    receipts = ExecutionEngine(exchange).execute_intent(
        TradeIntent(
            decision="REPLACE_ENTRY",
            orders=orders,
            rationale=["test"],
            risk_notes=[],
            confidence=0.5,
        )
    )
    assert receipts[-1]["status"] == "deferred_waiting_cancel"
    exchange.create_order.assert_not_called()


@pytest.mark.parametrize(
    "status,filled,expected",
    [
        ("open", 0, "submitted"),
        ("New", 0, "submitted"),
        ("open", 0.25, "partial_filled"),
        ("PartiallyFilled", 0, "partial_filled"),
        ("Filled", 1, "completed"),
        ("mystery", 0, "unknown"),
    ],
)
def test_recovery_sweep_distinguishes_open_partial_and_terminal(
    status, filled, expected
):
    from main import TradingOrchestrator

    orchestrator = TradingOrchestrator.__new__(TradingOrchestrator)
    assert (
        orchestrator._resolve_runtime_state_from_recovered_order(
            {"status": status, "filled": filled}
        )
        == expected
    )


@pytest.mark.parametrize(
    "count,alignment,bonus", [(0, 0.5, 0), (3, 0, 0), (3, 1, 0.08)]
)
def test_policy_calibration_bonus_requires_observations(count, alignment, bonus):
    from batch.memory_updater import MemoryUpdater

    updater = MemoryUpdater.__new__(MemoryUpdater)
    updater.state_store = object()
    evaluations = updater._evaluate_candidate_policies(
        [dict(id=1, domain="alpha_1h", metrics={})],
        [],
        confidence_calibration_summary={
            "evaluated_count": count,
            "alignment_score": alignment,
        },
    )
    assert evaluations[0]["score"] == bonus
    assert evaluations[0]["metrics"]["confidence_alignment_score"] == alignment


@pytest.mark.parametrize("kind", ["candidate", "bundle"])
@pytest.mark.parametrize("alignment,promoted", [(0.0, False), (0.6, True)])
def test_promotion_respects_zero_alignment(kind, alignment, promoted):
    from batch.memory_updater import MemoryUpdater

    updater = MemoryUpdater.__new__(MemoryUpdater)
    updater._learning_promotions_allowed = Mock(return_value=True)
    updater._passes_temporal_holdout_gate = Mock(return_value=True)
    updater.state_store = Mock()
    updater.state_store.build_policy_key.return_value = "fake-policy"
    updater.state_store.get_policy_registry_entry.return_value = {}
    updater.state_store.get_active_policy_bundle.return_value = {}
    metrics = {
        "confidence_alignment_score": alignment,
        "trading_balance_summary": {
            "balance_score": 0.8,
            "portfolio_utility_score": 0.8,
            "pressure_ratio": 1,
        },
    }
    evaluation = dict(
        candidate_policy_id=1,
        bundle_key="fake-bundle",
        sample_count=5,
        score=1,
        evaluation_scope="shadow",
        metrics=metrics,
    )
    if kind == "candidate":
        updater._promote_candidate_policies(
            [dict(id=1, domain="alpha_1h")], [evaluation]
        )
        assert updater.state_store.upsert_policy_registry.called is promoted
    else:
        updater._promote_policy_bundles([dict(bundle_key="fake-bundle")], [evaluation])
        assert updater.state_store.upsert_policy_bundle.called is promoted
