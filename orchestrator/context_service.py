from orchestrator.journal_context import TradingJournalContextMixin
from orchestrator.memory_context import TradingMemoryContextMixin
from orchestrator.similar_case_context import TradingSimilarCaseContextMixin


class TradingContextServiceMixin(
    TradingJournalContextMixin,
    TradingMemoryContextMixin,
    TradingSimilarCaseContextMixin,
):
    pass
