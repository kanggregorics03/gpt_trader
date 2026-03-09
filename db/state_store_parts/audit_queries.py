from db.state_store_parts.audit_log_queries import StateStoreAuditLogQueryMixin
from db.state_store_parts.ledger_audit_queries import StateStoreLedgerAuditQueryMixin
from db.state_store_parts.replay_audit_queries import StateStoreReplayAuditQueryMixin


class StateStoreAuditQueryMixin(
    StateStoreAuditLogQueryMixin,
    StateStoreReplayAuditQueryMixin,
    StateStoreLedgerAuditQueryMixin,
):
    pass
