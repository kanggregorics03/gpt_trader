from orchestrator.cycle_audit import TradingCycleAuditMixin
from orchestrator.cycle_execution_flow import TradingCycleExecutionFlowMixin
from orchestrator.cycle_risk_eval import TradingCycleRiskEvaluationMixin


class TradingCyclePersistenceMixin(
    TradingCycleRiskEvaluationMixin,
    TradingCycleExecutionFlowMixin,
    TradingCycleAuditMixin,
):
    pass
