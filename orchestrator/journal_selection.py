from orchestrator.journal_scoring import TradingJournalScoringMixin
from orchestrator.recent_journal_selection import TradingRecentJournalSelectionMixin


class TradingJournalSelectionMixin(
    TradingJournalScoringMixin,
    TradingRecentJournalSelectionMixin,
):
    pass
