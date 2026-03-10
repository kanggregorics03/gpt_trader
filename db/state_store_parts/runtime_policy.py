from db.state_store_parts.execution_evaluation_runtime import StateStoreExecutionEvaluationRuntimeMixin
from db.state_store_parts.learning_runtime import StateStoreLearningRuntimeMixin
from db.state_store_parts.policy_registry_runtime import StateStorePolicyRegistryRuntimeMixin
from db.state_store_parts.runtime_locking import StateStoreRuntimeLockingMixin
from db.state_store_parts.runtime_transitions import StateStoreRuntimeTransitionMixin


class StateStoreRuntimePolicyMixin(
    StateStoreExecutionEvaluationRuntimeMixin,
    StateStorePolicyRegistryRuntimeMixin,
    StateStoreLearningRuntimeMixin,
    StateStoreRuntimeLockingMixin,
    StateStoreRuntimeTransitionMixin,
):
    pass
