import unittest

from core.intent_validator import StructuredIntentValidator
from core.models import (
    AccountState,
    DecisionRequest,
    EvidenceItem,
    MarketSnapshot,
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
