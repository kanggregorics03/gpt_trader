from dataclasses import dataclass
import os

from ai.llm_provider import normalize_provider_name


@dataclass(frozen=True)
class RuntimeConfig:
    api_key: str | None
    api_secret: str | None
    trading_mode_env: str | None
    trading_testnet_env: str | None
    ai_provider: str | None
    ai_api_key: str | None
    ai_base_url: str | None
    ai_default_model: str | None
    ai_trigger_model: str | None
    db_path: str

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        return cls(
            api_key=os.getenv("BYBIT_API_KEY"),
            api_secret=os.getenv("BYBIT_API_SECRET"),
            trading_mode_env=os.getenv("BYBIT_TRADING_MODE"),
            trading_testnet_env=os.getenv("BYBIT_TESTNET"),
            ai_provider=normalize_provider_name(os.getenv("AI_PROVIDER")),
            ai_api_key=os.getenv("AI_API_KEY"),
            ai_base_url=os.getenv("AI_BASE_URL"),
            ai_default_model=(
                os.getenv("AI_DEFAULT_MODEL")
                or os.getenv("GEMINI_DEFAULT_MODEL")
                or os.getenv("GEMINI_MODEL")
            ),
            ai_trigger_model=os.getenv("AI_TRIGGER_MODEL") or os.getenv("GEMINI_TRIGGER_MODEL"),
            db_path=os.getenv("DB_PATH", "data/gpt_trader.db"),
        )


class StartupConfigurationError(RuntimeError):
    """Raised when the orchestrator cannot start with the current configuration."""
