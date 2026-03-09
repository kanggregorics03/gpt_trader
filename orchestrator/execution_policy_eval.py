from orchestrator.execution_evaluation_entries import TradingExecutionEvaluationEntryMixin
from orchestrator.execution_ledger import TradingExecutionLedgerMixin
from orchestrator.execution_metrics import TradingExecutionMetricsMixin


class TradingExecutionPolicyEvaluationMixin(
    TradingExecutionLedgerMixin,
    TradingExecutionMetricsMixin,
    TradingExecutionEvaluationEntryMixin,
):
    pass
