from orchestrator.cadence_policy import TradingCadencePolicyMixin
from orchestrator.runtime_diagnostics import TradingRuntimeDiagnosticsMixin
from orchestrator.runtime_state import TradingRuntimeStateMixin


class TradingRuntimeSupportMixin(
    TradingRuntimeStateMixin,
    TradingRuntimeDiagnosticsMixin,
    TradingCadencePolicyMixin,
):
    pass
