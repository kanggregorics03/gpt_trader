from orchestrator.cycle_artifact_persistence import TradingCycleArtifactPersistenceMixin
from orchestrator.cycle_error_audit import TradingCycleErrorAuditMixin
from orchestrator.cycle_result_summary import TradingCycleResultSummaryMixin


class TradingCycleAuditMixin(
    TradingCycleResultSummaryMixin,
    TradingCycleArtifactPersistenceMixin,
    TradingCycleErrorAuditMixin,
):
    pass
