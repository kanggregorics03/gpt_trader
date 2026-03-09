from orchestrator.trade_case_annotation import TradingTradeCaseAnnotationMixin
from orchestrator.trade_case_lifecycle import TradingTradeCaseLifecycleMixin
from orchestrator.trade_case_retrieval import TradingTradeCaseRetrievalMixin
from orchestrator.trade_case_snapshots import TradingTradeCaseSnapshotMixin


class TradingTradeCaseServiceMixin(
    TradingTradeCaseRetrievalMixin,
    TradingTradeCaseLifecycleMixin,
    TradingTradeCaseSnapshotMixin,
    TradingTradeCaseAnnotationMixin,
):
    pass
