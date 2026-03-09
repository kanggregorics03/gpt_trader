from db.state_store_parts.decoding_queries import StateStoreDecodingMixin
from db.state_store_parts.eligibility_queries import StateStoreEligibilityMixin
from db.state_store_parts.risk_state_queries import StateStoreRiskStateMixin


class StateStoreRiskQueryMixin(
    StateStoreRiskStateMixin,
    StateStoreEligibilityMixin,
    StateStoreDecodingMixin,
):
    pass
