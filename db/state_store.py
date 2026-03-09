from db.state_store_parts.audit_journal import StateStoreAuditJournalMixin
from db.state_store_parts.queries import StateStoreQueryMixin
from db.state_store_parts.runtime_policy import StateStoreRuntimePolicyMixin
from db.state_store_parts.schema import StateStoreSchemaMixin
from db.state_store_parts.trade_cases import StateStoreTradeCaseMixin


class StateStore(
    StateStoreSchemaMixin,
    StateStoreAuditJournalMixin,
    StateStoreTradeCaseMixin,
    StateStoreRuntimePolicyMixin,
    StateStoreQueryMixin,
):
    pass
