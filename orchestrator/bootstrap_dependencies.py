from dataclasses import dataclass

from ai.decision_engine import DecisionEngine
from ai.rag_engine import RagEngine
from core.alpha_policy import AlphaPolicyEngine
from core.execution_policy import ExecutionPolicyEngine
from core.position_policy import PositionPolicyEngine
from core.risk_engine import RiskEngine
from core.safety_policy import SafetyPolicyEngine
from core.snapshot_service import SnapshotService
from db.state_store import StateStore
from exchange.account_reader import AccountReader
from exchange.bybit_factory import (
    create_private_account_feed,
    create_private_exchange,
    create_public_exchange,
    create_public_market_feed,
    normalize_trading_mode,
)
from exchange.collector import MarketCollector
from exchange.execution_engine import ExecutionEngine
from utils.alerter import AlertManager
from utils.logger import log

from orchestrator.bootstrap_config import RuntimeConfig, StartupConfigurationError


@dataclass(frozen=True)
class RuntimeDependencies:
    runtime_config: RuntimeConfig
    trading_mode: str
    state_store: StateStore
    risk_engine: RiskEngine
    rag_engine: RagEngine
    symbols: list[str]
    symbol: str
    private_exchange: object
    public_exchange: object
    public_feed: object
    private_account_feed: object
    account_reader: AccountReader
    market_collector: MarketCollector
    execution_engine: ExecutionEngine
    execution_policy_engine: ExecutionPolicyEngine
    alpha_policy_engine: AlphaPolicyEngine
    position_policy_engine: PositionPolicyEngine
    safety_policy_engine: SafetyPolicyEngine
    ai_engine: DecisionEngine
    alert_manager: AlertManager
    snapshot_service: SnapshotService


class BootstrapDependencyBuilder:
    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    def build(self) -> RuntimeDependencies:
        runtime_config = RuntimeConfig.from_env()
        trading_mode = normalize_trading_mode(
            runtime_config.trading_mode_env,
            runtime_config.trading_testnet_env,
        )
        self._validate_configuration(runtime_config)

        state_store = StateStore(runtime_config.db_path)
        try:
            risk_engine = RiskEngine()
        except ValueError as exc:
            raise StartupConfigurationError(str(exc)) from exc
        rag_engine = RagEngine(runtime_config.db_path)
        symbols = risk_engine.get_trading_symbols()
        symbol = risk_engine.get_trading_symbol()

        private_exchange = create_private_exchange(
            runtime_config.api_key,
            runtime_config.api_secret,
            trading_mode=trading_mode,
        )
        public_exchange = create_public_exchange()
        log.info(
            "Bybit exchange routing: market_data=mainnet, private_trading={}",
            trading_mode,
        )
        public_feed = create_public_market_feed(
            symbols,
            **self.orchestrator._build_public_market_ws_config(risk_engine),
        )
        private_account_feed = create_private_account_feed(
            runtime_config.api_key,
            runtime_config.api_secret,
            trading_mode=trading_mode,
            **self.orchestrator._build_private_account_ws_config(risk_engine),
        )

        account_reader = AccountReader(
            private_exchange,
            trading_mode=trading_mode,
            private_feed=private_account_feed,
        )
        market_collector = MarketCollector(
            public_exchange,
            public_feed=public_feed,
            response_cache_ttls_ms=self.orchestrator._build_market_data_cache_config(risk_engine),
        )
        execution_engine = ExecutionEngine(
            private_exchange,
            private_feed=private_account_feed,
        )
        execution_policy_engine = ExecutionPolicyEngine(
            policy=risk_engine.policy,
            state_store=state_store,
        )
        alpha_policy_engine = AlphaPolicyEngine(
            policy=risk_engine.policy,
            state_store=state_store,
        )
        position_policy_engine = PositionPolicyEngine(
            policy=risk_engine.policy,
            state_store=state_store,
        )
        safety_policy_engine = SafetyPolicyEngine(
            policy=risk_engine.policy,
            state_store=state_store,
        )

        ai_engine = DecisionEngine(
            api_key=runtime_config.ai_api_key,
            cache_store=state_store,
            provider=runtime_config.ai_provider,
            base_url=runtime_config.ai_base_url,
            default_model=runtime_config.ai_default_model,
            trigger_model=runtime_config.ai_trigger_model,
        )
        log.info(
            "AI routing: provider={} default={} trigger={}",
            getattr(ai_engine, "provider_name", "unknown"),
            ai_engine.flash_model,
            ai_engine.pro_model,
        )
        alert_manager = AlertManager()
        materialization_minutes = int(
            risk_engine.policy.get("adaptive_loop", {}).get(
                "materialization_minutes",
                risk_engine.policy.get("intervals", {}).get("trigger_loop_minutes", 5),
            ) or 5
        )
        snapshot_service = SnapshotService(
            state_store=state_store,
            account_reader=account_reader,
            market_collector=market_collector,
            risk_engine=risk_engine,
            symbols=symbols,
            materialization_minutes=materialization_minutes,
        )
        return RuntimeDependencies(
            runtime_config=runtime_config,
            trading_mode=trading_mode,
            state_store=state_store,
            risk_engine=risk_engine,
            rag_engine=rag_engine,
            symbols=symbols,
            symbol=symbol,
            private_exchange=private_exchange,
            public_exchange=public_exchange,
            public_feed=public_feed,
            private_account_feed=private_account_feed,
            account_reader=account_reader,
            market_collector=market_collector,
            execution_engine=execution_engine,
            execution_policy_engine=execution_policy_engine,
            alpha_policy_engine=alpha_policy_engine,
            position_policy_engine=position_policy_engine,
            safety_policy_engine=safety_policy_engine,
            ai_engine=ai_engine,
            alert_manager=alert_manager,
            snapshot_service=snapshot_service,
        )

    def _validate_configuration(self, runtime_config: RuntimeConfig) -> None:
        if not all([runtime_config.api_key, runtime_config.api_secret]):
            raise StartupConfigurationError("Missing required environment variables. Please check .env")
        try:
            self.orchestrator._validate_ai_configuration(runtime_config)
        except ValueError as exc:
            raise StartupConfigurationError(str(exc)) from exc
