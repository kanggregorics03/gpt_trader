from orchestrator.cycle_execution import TradingCycleExecutionMixin
from orchestrator.cycle_support import TradingCycleSupportMixin
from orchestrator.maintenance import TradingMaintenanceMixin


class TradingCycleRunnerMixin(
    TradingCycleSupportMixin,
    TradingCycleExecutionMixin,
    TradingMaintenanceMixin,
):
    pass
