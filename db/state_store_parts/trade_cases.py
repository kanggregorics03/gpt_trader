from db.state_store_parts.active_trade_cases import StateStoreActiveTradeCaseMixin
from db.state_store_parts.replay_runs import StateStoreReplayRunMixin
from db.state_store_parts.trade_case_repository import StateStoreTradeCaseRepositoryMixin
from db.state_store_parts.trade_case_snapshots import StateStoreTradeCaseSnapshotMixin


class StateStoreTradeCaseMixin(
    StateStoreActiveTradeCaseMixin,
    StateStoreTradeCaseRepositoryMixin,
    StateStoreTradeCaseSnapshotMixin,
    StateStoreReplayRunMixin,
):
    pass
