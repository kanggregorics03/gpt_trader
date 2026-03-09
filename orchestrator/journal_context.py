from orchestrator.journal_context_support import TradingJournalContextSupportMixin
from orchestrator.journal_selection import TradingJournalSelectionMixin
from orchestrator.market_vector_context import TradingMarketVectorContextMixin


class TradingJournalContextMixin(
    TradingJournalContextSupportMixin,
    TradingMarketVectorContextMixin,
    TradingJournalSelectionMixin,
):
    pass
