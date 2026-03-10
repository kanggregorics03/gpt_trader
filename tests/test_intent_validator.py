import unittest

from core.intent_validator import StructuredIntentValidator
from core.models import (
    AccountState,
    DecisionRequest,
    EvidenceItem,
    MarketSnapshot,
    PositionState,
    RationaleItem,
    RiskStateSnapshot,
    TradeIntent,
)


class TestStructuredIntentValidator(unittest.TestCase):
    def setUp(self):
        self.validator = StructuredIntentValidator()
        self.request = DecisionRequest(
            policy_summary="risk: normal",
            account_state=AccountState(
                total_balance=1000.0,
                available_balance=900.0,
            ),
            market_snapshots={
                "BTCUSDT": MarketSnapshot(
                    symbol="BTCUSDT",
                    current_price=30000.0,
                    atr_15m=120.0,
                    atr_1h=240.0,
                    trend_1h="UP",
                    recent_volume_surge=True,
                    timeframes={
                        "15m": {
                            "timeframe": "15m",
                            "last_close": 101.0,
                            "previous_values": {
                                "last_close": 99.0,
                                "rsi_14": 58.2,
                                "volume_ratio": 1.6,
                            },
                            "change_1bar_pct": 2.0202,
                            "rsi_14": 66.4,
                            "volume_ratio": 2.41,
                        }
                    },
                )
            },
            risk_state=RiskStateSnapshot(
                daily_start_balance=1000.0,
                daily_pnl_pct=0.5,
                drawdown_active=False,
                consecutive_losses=0,
                cooldown_active=False,
            ),
            long_term_memory=[],
        )

    def test_validator_accepts_matching_structured_rationale(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[],
            rationale=["BTC momentum is confirmed on 15m."],
            rationale_items=[
                RationaleItem(
                    summary="BTC RSI is above the continuation threshold.",
                    why_it_matters="Momentum remains supportive for continuation.",
                    trade_implication="Long entries are allowed if risk limits pass.",
                    failure_mode="Momentum rolls over below the threshold.",
                    evidence=[
                        EvidenceItem(
                            signal="rsi_14",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="gte",
                            observed_value=66.4,
                            threshold=60.0,
                            units="index",
                            source="market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                        )
                    ],
                )
            ],
            risk_notes=[],
            confidence=0.6,
        )

        result = self.validator.validate(self.request, intent)

        self.assertTrue(result.is_valid)
        self.assertEqual(result.issues, [])

    def test_validator_rejects_missing_or_fabricated_evidence(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[],
            rationale=["BTC momentum is confirmed on 15m."],
            rationale_items=[
                RationaleItem(
                    summary="BTC RSI is above the continuation threshold.",
                    why_it_matters="Momentum remains supportive for continuation.",
                    trade_implication="Long entries are allowed if risk limits pass.",
                    failure_mode="Momentum rolls over below the threshold.",
                    evidence=[
                        EvidenceItem(
                            signal="rsi_14",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="gte",
                            observed_value=72.0,
                            threshold=60.0,
                            units="index",
                            source="market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                        ),
                        EvidenceItem(
                            signal="funding_rate",
                            symbol="BTCUSDT",
                            timeframe="8h",
                            operator="note",
                            observed_value=0.01,
                            threshold=None,
                            units="rate",
                            source="market_snapshots.BTCUSDT.derivatives.missing_field",
                        ),
                    ],
                )
            ],
            risk_notes=[],
            confidence=0.6,
        )

        result = self.validator.validate(self.request, intent)

        self.assertFalse(result.is_valid)
        self.assertTrue(any("observed_value_mismatch" in issue for issue in result.issues))
        self.assertTrue(any("source_missing" in issue for issue in result.issues))

    def test_validator_accepts_market_context_source_alias(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[],
            rationale=["BTC momentum is confirmed on 15m."],
            rationale_items=[
                RationaleItem(
                    summary="BTC RSI is above the continuation threshold.",
                    why_it_matters="Momentum remains supportive for continuation.",
                    trade_implication="Long entries are allowed if risk limits pass.",
                    failure_mode="Momentum rolls over below the threshold.",
                    evidence=[
                        EvidenceItem(
                            signal="rsi_14",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="gte",
                            observed_value=66.4,
                            threshold=60.0,
                            units="index",
                            source="market_context.BTCUSDT.timeframes.15m.rsi_14",
                        )
                    ],
                )
            ],
            risk_notes=[],
            confidence=0.6,
        )

        result = self.validator.validate(self.request, intent)

        self.assertTrue(result.is_valid)

    def test_validator_accepts_boolean_sources_and_trigger_mode_alias(self):
        request = self.request.model_copy(
            update={
                "is_trigger_mode": True,
                "market_snapshots": {
                    "BTCUSDT": self.request.market_snapshots["BTCUSDT"].model_copy(
                        update={
                            "volume_surge_trigger_active": True,
                            "fast_move_trigger_active": False,
                        }
                    )
                },
                "risk_state": self.request.risk_state.model_copy(
                    update={"cooldown_active": False}
                ),
            }
        )
        intent = TradeIntent(
            decision="BUY",
            orders=[],
            rationale=["BTC trigger state remains coherent."],
            rationale_items=[
                RationaleItem(
                    summary="Trigger state is active while cooldown remains off.",
                    why_it_matters="This supports closer monitoring without forcing a trade.",
                    trade_implication="Allow a small long only if price structure also confirms.",
                    failure_mode="Disable the setup if trigger participation fades or cooldown activates.",
                    evidence=[
                        EvidenceItem(
                            signal="trigger_mode_active",
                            symbol="BTCUSDT",
                            timeframe="risk",
                            operator="eq",
                            observed_value=1.0,
                            threshold=1.0,
                            units="flag",
                            source="trigger_mode_active",
                        ),
                        EvidenceItem(
                            signal="volume_surge_trigger_active",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="eq",
                            observed_value=1.0,
                            threshold=1.0,
                            units="flag",
                            source="market_snapshots.BTCUSDT.volume_surge_trigger_active",
                        ),
                        EvidenceItem(
                            signal="cooldown_active",
                            timeframe="risk",
                            operator="eq",
                            observed_value=0.0,
                            threshold=0.0,
                            units="flag",
                            source="risk_state.cooldown_active",
                        ),
                    ],
                )
            ],
            risk_notes=[],
            confidence=0.6,
        )

        result = self.validator.validate(request, intent)

        self.assertTrue(result.is_valid)

    def test_validator_accepts_policy_summary_source_paths(self):
        request = self.request.model_copy(
            update={
                "policy_summary": (
                    "risk_limits:\n"
                    "  max_consecutive_losses: 3\n"
                    "  max_open_positions: 1\n"
                )
            }
        )
        intent = TradeIntent(
            decision="HOLD",
            orders=[],
            rationale=["Loss guard remains active in policy."],
            rationale_items=[
                RationaleItem(
                    summary="Policy loss cap is defined.",
                    why_it_matters="The model can reference binding policy limits as evidence.",
                    trade_implication="Avoid overstating risk flexibility beyond configured limits.",
                    failure_mode="The policy summary is missing the loss-cap field.",
                    evidence=[
                        EvidenceItem(
                            signal="max_consecutive_losses",
                            timeframe="risk",
                            operator="gte",
                            observed_value=3.0,
                            threshold=1.0,
                            units="count",
                            source="policy_summary.risk_limits.max_consecutive_losses",
                        )
                    ],
                )
            ],
            risk_notes=[],
            confidence=0.3,
        )

        result = self.validator.validate(request, intent)

        self.assertTrue(result.is_valid)

    def test_validator_accepts_bracket_index_account_state_sources(self):
        request = self.request.model_copy(
            update={
                "account_state": self.request.account_state.model_copy(
                    update={
                        "positions": [
                            PositionState(
                                symbol="BTCUSDT",
                                side="LONG",
                                size=0.1,
                                entry_price=30000.0,
                                mark_price=30120.0,
                                unrealized_pnl=12.0,
                                unrealized_pnl_pct=0.4,
                                leverage=3.0,
                            )
                        ]
                    }
                )
            }
        )
        intent = TradeIntent(
            decision="HOLD",
            orders=[],
            rationale=["Live position remains small and profitable."],
            rationale_items=[
                RationaleItem(
                    summary="The live BTC position is mildly profitable with moderate leverage.",
                    why_it_matters="Position-management evidence should resolve through list-index account paths.",
                    trade_implication="Holding is acceptable while exposure remains controlled.",
                    failure_mode="The live position path cannot be resolved from account_state.",
                    evidence=[
                        EvidenceItem(
                            signal="unrealized_pnl_pct",
                            symbol="BTCUSDT",
                            timeframe="risk",
                            operator="gte",
                            observed_value=0.4,
                            threshold=0.0,
                            units="pct",
                            source="account_state.positions[0].unrealized_pnl_pct",
                        ),
                        EvidenceItem(
                            signal="leverage",
                            symbol="BTCUSDT",
                            timeframe="risk",
                            operator="lte",
                            observed_value=3.0,
                            threshold=5.0,
                            units="x",
                            source="account_state.positions[0].leverage",
                        ),
                    ],
                )
            ],
            risk_notes=[],
            confidence=0.35,
        )

        result = self.validator.validate(request, intent)

        self.assertTrue(result.is_valid)

    def test_validator_accepts_categorical_eq_evidence(self):
        request = self.request.model_copy(
            update={
                "market_snapshots": {
                    "BTCUSDT": self.request.market_snapshots["BTCUSDT"].model_copy(
                        update={
                            "timeframes": {
                                "15m": self.request.market_snapshots["BTCUSDT"].timeframes["15m"].model_copy(
                                    update={"trend": "FLAT"}
                                ),
                            },
                            "regime": self.request.market_snapshots["BTCUSDT"].regime.model_copy(
                                update={"momentum_regime": "accelerating_down"}
                            ),
                        }
                    )
                }
            }
        )
        intent = TradeIntent.model_validate(
            {
                "decision": "HOLD",
                "orders": [],
                "rationale": ["BTC structure is mixed and momentum is softening."],
                "rationale_items": [
                    {
                        "summary": "Higher-timeframe trend is flat while momentum regime is deteriorating.",
                        "why_it_matters": "Categorical state can explain why a setup is not strong enough to trade.",
                        "trade_implication": "Prefer HOLD until directional evidence strengthens.",
                        "failure_mode": "Momentum turns back in favor of continuation.",
                        "evidence": [
                            {
                                "signal": "trend",
                                "symbol": "BTCUSDT",
                                "timeframe": "15m",
                                "operator": "eq",
                                "observed_value": "FLAT",
                                "threshold": "FLAT",
                                "units": "state",
                                "source": "market_snapshots.BTCUSDT.timeframes.15m.trend",
                            },
                            {
                                "signal": "momentum_regime",
                                "symbol": "BTCUSDT",
                                "timeframe": "1h",
                                "operator": "eq",
                                "observed_value": "accelerating_down",
                                "threshold": "accelerating_down",
                                "units": "state",
                                "source": "market_snapshots.BTCUSDT.regime.momentum_regime",
                            },
                            {
                                "signal": "rsi_14",
                                "symbol": "BTCUSDT",
                                "timeframe": "15m",
                                "operator": "gte",
                                "observed_value": 66.4,
                                "threshold": 60.0,
                                "units": "index",
                                "source": "market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                            },
                        ],
                    }
                ],
                "risk_notes": [],
                "confidence": 0.4,
            }
        )

        result = self.validator.validate(request, intent)

        self.assertTrue(result.is_valid)

    def test_validator_accepts_negative_confidence_contribution(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[],
            rationale=["BTC setup has one offsetting execution-quality risk."],
            rationale_items=[
                RationaleItem(
                    summary="Momentum is constructive but spread quality slightly offsets conviction.",
                    why_it_matters="Negative execution evidence should be representable without invalidating the payload.",
                    trade_implication="Allow a small long only with reduced confidence.",
                    failure_mode="Execution quality degrades further.",
                    evidence=[
                        EvidenceItem(
                            signal="rsi_14",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="gte",
                            observed_value=66.4,
                            threshold=60.0,
                            units="index",
                            source="market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                            confidence_contribution=0.25,
                        ),
                        EvidenceItem(
                            signal="spread_penalty",
                            symbol="BTCUSDT",
                            timeframe="risk",
                            operator="note",
                            observed_value=1.0,
                            threshold=None,
                            units="flag",
                            source="market_snapshots.BTCUSDT.recent_volume_surge",
                            confidence_contribution=-0.15,
                        ),
                    ],
                )
            ],
            risk_notes=["Execution quality slightly reduces conviction."],
            confidence=0.55,
        )

        result = self.validator.validate(self.request, intent)

        self.assertTrue(result.is_valid)

    def test_validator_rejects_failed_operator_condition(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[],
            rationale=["BTC breakout is confirmed."],
            rationale_items=[
                RationaleItem(
                    summary="BTC breakout above the hourly range.",
                    why_it_matters="Breakout continuation would support long entries.",
                    trade_implication="Allow a long entry after confirmation.",
                    failure_mode="Price falls back under the range high.",
                    evidence=[
                        EvidenceItem(
                            signal="last_close",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="cross_up",
                            observed_value=66.4,
                            threshold=70.0,
                            units="index",
                            source="market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                        )
                    ],
                )
            ],
            risk_notes=["Breakout failure would invalidate the entry."],
            confidence=0.6,
        )

        result = self.validator.validate(self.request, intent)

        self.assertFalse(result.is_valid)
        self.assertTrue(any("operator_condition_failed" in issue for issue in result.issues))

    def test_validator_accepts_true_cross_up_using_previous_bar(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[],
            rationale=["BTC crossed back above the threshold."],
            rationale_items=[
                RationaleItem(
                    summary="BTC reclaimed the level.",
                    why_it_matters="A fresh cross can validate continuation.",
                    trade_implication="Allow a long entry after the reclaim.",
                    failure_mode="Price loses the threshold again.",
                    evidence=[
                        EvidenceItem(
                            signal="last_close",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="cross_up",
                            observed_value=101.0,
                            threshold=100.0,
                            units="usdt",
                            source="market_snapshots.BTCUSDT.timeframes.15m.last_close",
                        )
                    ],
                )
            ],
            risk_notes=["A failed reclaim would invalidate the setup."],
            confidence=0.6,
        )

        result = self.validator.validate(self.request, intent)

        self.assertTrue(result.is_valid)

    def test_validator_rejects_cross_up_without_real_cross(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[],
            rationale=["BTC is above the threshold."],
            rationale_items=[
                RationaleItem(
                    summary="BTC remains above the level.",
                    why_it_matters="Price is elevated but not a fresh cross.",
                    trade_implication="Allow a long entry after confirmation.",
                    failure_mode="Price loses the threshold again.",
                    evidence=[
                        EvidenceItem(
                            signal="last_close",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="cross_up",
                            observed_value=101.0,
                            threshold=98.0,
                            units="usdt",
                            source="market_snapshots.BTCUSDT.timeframes.15m.last_close",
                        )
                    ],
                )
            ],
            risk_notes=["A failed reclaim would invalidate the setup."],
            confidence=0.6,
        )

        result = self.validator.validate(self.request, intent)

        self.assertFalse(result.is_valid)
        self.assertTrue(any("operator_condition_failed" in issue for issue in result.issues))

    def test_validator_rejects_action_consistency_mismatch(self):
        intent = TradeIntent(
            decision="BUY",
            orders=[],
            rationale=["BTC setup is acceptable."],
            rationale_items=[
                RationaleItem(
                    summary="BTC momentum remains positive.",
                    why_it_matters="Momentum is supportive.",
                    trade_implication="Reduce risk and wait for confirmation.",
                    failure_mode="Momentum fades.",
                    evidence=[
                        EvidenceItem(
                            signal="rsi_14",
                            symbol="BTCUSDT",
                            timeframe="15m",
                            operator="gte",
                            observed_value=66.4,
                            threshold=60.0,
                            units="index",
                            source="market_snapshots.BTCUSDT.timeframes.15m.rsi_14",
                        )
                    ],
                )
            ],
            risk_notes=[],
            confidence=0.82,
        )

        result = self.validator.validate(self.request, intent)

        self.assertFalse(result.is_valid)
        self.assertIn("trade_implication_action_mismatch", result.issues)
        self.assertIn("confidence_exceeds_evidence_quality", result.issues)


if __name__ == "__main__":
    unittest.main()
