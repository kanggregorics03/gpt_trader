from db.state_store_parts.fill_ledger_queries import StateStoreFillLedgerQueryMixin
from db.state_store_parts.market_path_queries import StateStoreMarketPathQueryMixin
from db.state_store_parts.transition_queries import StateStoreTransitionQueryMixin


class StateStoreLedgerAuditQueryMixin(
    StateStoreMarketPathQueryMixin,
    StateStoreFillLedgerQueryMixin,
    StateStoreTransitionQueryMixin,
):
    pass
