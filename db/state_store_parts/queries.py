from db.state_store_parts.audit_queries import StateStoreAuditQueryMixin
from db.state_store_parts.monitoring_queries import StateStoreMonitoringQueryMixin
from db.state_store_parts.risk_queries import StateStoreRiskQueryMixin


class StateStoreQueryMixin(
    StateStoreMonitoringQueryMixin,
    StateStoreAuditQueryMixin,
    StateStoreRiskQueryMixin,
):
    pass
