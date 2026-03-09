from db.state_store_parts.audit_log_writes import StateStoreAuditLogWriteMixin
from db.state_store_parts.journal_repository import StateStoreJournalRepositoryMixin
from db.state_store_parts.retrieval_journal import StateStoreRetrievalJournalMixin


class StateStoreAuditJournalMixin(
    StateStoreAuditLogWriteMixin,
    StateStoreJournalRepositoryMixin,
    StateStoreRetrievalJournalMixin,
):
    pass
