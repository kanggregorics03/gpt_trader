from orchestrator.cycle_alerts import TradingCycleAlertsMixin
from orchestrator.cycle_execution_orchestrator import TradingCycleExecutionOrchestratorMixin
from orchestrator.cycle_order_submission import TradingCycleOrderSubmissionMixin
from orchestrator.cycle_preflight import TradingCyclePreflightMixin


class TradingCycleExecutionFlowMixin(
    TradingCycleExecutionOrchestratorMixin,
    TradingCyclePreflightMixin,
    TradingCycleOrderSubmissionMixin,
    TradingCycleAlertsMixin,
):
    pass
