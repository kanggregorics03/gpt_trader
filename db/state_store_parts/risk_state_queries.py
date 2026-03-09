from db.state_store_parts.decision_state_queries import StateStoreDecisionStateMixin
from db.state_store_parts.order_runtime_queries import StateStoreOrderRuntimeMixin
from db.state_store_parts.risk_calculation_queries import StateStoreRiskCalculationMixin


class StateStoreRiskStateMixin(
    StateStoreRiskCalculationMixin,
    StateStoreDecisionStateMixin,
    StateStoreOrderRuntimeMixin,
):
    pass
