from orchestrator.execution_policy_eval import TradingExecutionPolicyEvaluationMixin
from orchestrator.retrieval_outcomes import TradingRetrievalOutcomeMixin
from orchestrator.symbol_journal_builder import TradingSymbolJournalBuilderMixin


class TradingTradeCaseRetrievalMixin(
    TradingRetrievalOutcomeMixin,
    TradingExecutionPolicyEvaluationMixin,
    TradingSymbolJournalBuilderMixin,
):
    pass
