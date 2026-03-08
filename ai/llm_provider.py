import inspect
import os
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel

try:
    import anthropic
except ImportError:  # pragma: no cover - optional dependency
    anthropic = None

try:
    from google import genai
    from google.genai import types
except ImportError:  # pragma: no cover - optional dependency
    genai = None
    types = None

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - optional dependency
    OpenAI = None


PROVIDER_ALIASES = {
    "gpt": "openai",
    "chatgpt": "openai",
    "openai": "openai",
    "deepseek": "deepseek",
    "claude": "anthropic",
    "anthropic": "anthropic",
    "gemini": "gemini",
    "google": "gemini",
}

PROVIDER_API_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

PROVIDER_BASE_URL_ENV = {
    "gemini": "GEMINI_BASE_URL",
    "openai": "OPENAI_BASE_URL",
    "deepseek": "DEEPSEEK_BASE_URL",
    "anthropic": "ANTHROPIC_BASE_URL",
}

PROVIDER_DEFAULT_MODEL_ENV = {
    "gemini": "GEMINI_DEFAULT_MODEL",
    "openai": "OPENAI_DEFAULT_MODEL",
    "deepseek": "DEEPSEEK_DEFAULT_MODEL",
    "anthropic": "ANTHROPIC_DEFAULT_MODEL",
}

PROVIDER_TRIGGER_MODEL_ENV = {
    "gemini": "GEMINI_TRIGGER_MODEL",
    "openai": "OPENAI_TRIGGER_MODEL",
    "deepseek": "DEEPSEEK_TRIGGER_MODEL",
    "anthropic": "ANTHROPIC_TRIGGER_MODEL",
}

PROVIDER_MEMORY_MODEL_ENV = {
    "gemini": "GEMINI_MEMORY_MODEL",
    "openai": "OPENAI_MEMORY_MODEL",
    "deepseek": "DEEPSEEK_MEMORY_MODEL",
    "anthropic": "ANTHROPIC_MEMORY_MODEL",
}


@dataclass
class LLMGenerationResult:
    text: str
    usage_metadata: dict[str, Any]


def normalize_provider_name(provider: Optional[str] = None) -> str:
    raw = (provider or os.getenv("AI_PROVIDER") or "").strip().lower()
    if raw:
        return PROVIDER_ALIASES.get(raw, raw)
    return "gemini"


def resolve_api_key(
    provider: str,
    explicit_api_key: Optional[str] = None,
    *,
    shared_api_key: Optional[str] = None,
) -> str:
    if (explicit_api_key or "").strip():
        return explicit_api_key.strip()
    env_key = PROVIDER_API_KEY_ENV.get(provider)
    if env_key and os.getenv(env_key, "").strip():
        return os.getenv(env_key, "").strip()
    if (shared_api_key or "").strip():
        return shared_api_key.strip()
    if os.getenv("AI_API_KEY", "").strip():
        return os.getenv("AI_API_KEY", "").strip()
    raise ValueError(
        f"Missing API key for provider '{provider}'. Set AI_API_KEY or {env_key}."
    )


def resolve_base_url(
    provider: str,
    explicit_base_url: Optional[str] = None,
    *,
    shared_base_url: Optional[str] = None,
) -> Optional[str]:
    if (explicit_base_url or "").strip():
        return explicit_base_url.strip()
    env_key = PROVIDER_BASE_URL_ENV.get(provider)
    if env_key and os.getenv(env_key, "").strip():
        return os.getenv(env_key, "").strip()
    if (shared_base_url or "").strip():
        return shared_base_url.strip()
    if os.getenv("AI_BASE_URL", "").strip():
        return os.getenv("AI_BASE_URL", "").strip()
    if provider == "deepseek":
        return "https://api.deepseek.com"
    return None


def _require_model_name(provider: str, model_name: Optional[str], purpose: str) -> str:
    if (model_name or "").strip():
        return model_name.strip()
    raise ValueError(
        f"Missing {purpose} model for provider '{provider}'. "
        f"Set AI_{purpose.upper()}_MODEL or the provider-specific model env."
    )


def resolve_decision_models(
    provider: str,
    *,
    default_model: Optional[str] = None,
    trigger_model: Optional[str] = None,
) -> tuple[str, str]:
    legacy_model = os.getenv("GEMINI_MODEL", "").strip() if provider == "gemini" else ""
    resolved_default = (
        (default_model or "").strip()
        or os.getenv("AI_DEFAULT_MODEL", "").strip()
        or os.getenv(PROVIDER_DEFAULT_MODEL_ENV.get(provider, ""), "").strip()
        or legacy_model
        or ("gemini-3.0-pro" if provider == "gemini" else "")
    )
    resolved_default = _require_model_name(provider, resolved_default, "default")
    resolved_trigger = (
        (trigger_model or "").strip()
        or os.getenv("AI_TRIGGER_MODEL", "").strip()
        or os.getenv(PROVIDER_TRIGGER_MODEL_ENV.get(provider, ""), "").strip()
        or (
            os.getenv("GEMINI_TRIGGER_MODEL", "").strip()
            if provider == "gemini"
            else ""
        )
        or resolved_default
    )
    return resolved_default, _require_model_name(provider, resolved_trigger, "trigger")


def resolve_memory_model_name(provider: str, explicit_model: Optional[str] = None) -> str:
    legacy_model = os.getenv("GEMINI_MODEL", "").strip() if provider == "gemini" else ""
    resolved_model = (
        (explicit_model or "").strip()
        or os.getenv("MEMORY_MODEL", "").strip()
        or os.getenv(PROVIDER_MEMORY_MODEL_ENV.get(provider, ""), "").strip()
        or os.getenv("AI_TRIGGER_MODEL", "").strip()
        or os.getenv("AI_DEFAULT_MODEL", "").strip()
        or os.getenv(PROVIDER_TRIGGER_MODEL_ENV.get(provider, ""), "").strip()
        or os.getenv(PROVIDER_DEFAULT_MODEL_ENV.get(provider, ""), "").strip()
        or (
            os.getenv("GEMINI_MEMORY_MODEL", "").strip()
            if provider == "gemini"
            else ""
        )
        or (
            os.getenv("GEMINI_TRIGGER_MODEL", "").strip()
            if provider == "gemini"
            else ""
        )
        or legacy_model
        or ("gemini-3.0-pro" if provider == "gemini" else "")
    )
    return _require_model_name(provider, resolved_model, "memory")


class BaseLLMProvider:
    provider_name = "base"
    JSON_ONLY_SYSTEM_PROMPT = (
        "Return only valid JSON. Do not include markdown fences, explanations, or prose outside the JSON payload."
    )

    def __init__(self):
        self.client = None

    def generate_json(
        self,
        *,
        prompt: str,
        model_name: str,
        schema: Any = None,
        temperature: float = 0.2,
        max_output_tokens: Optional[int] = None,
    ) -> LLMGenerationResult:
        raise NotImplementedError

    def _strip_code_fences(self, text: Any) -> str:
        raw = str(text or "").strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines).strip()
        return raw

    def _build_json_only_prompt(self, prompt: str) -> str:
        return (
            f"{prompt.rstrip()}\n\n"
            "FINAL OUTPUT REQUIREMENT:\n"
            "Return only valid JSON. Do not wrap the JSON in markdown or add commentary."
        )


class GeminiLLMProvider(BaseLLMProvider):
    provider_name = "gemini"

    def __init__(self, api_key: str):
        super().__init__()
        if genai is None or types is None:
            raise RuntimeError("google-genai is not installed.")
        self.client = genai.Client(api_key=api_key)

    def generate_json(
        self,
        *,
        prompt: str,
        model_name: str,
        schema: Any = None,
        temperature: float = 0.2,
        max_output_tokens: Optional[int] = None,
    ) -> LLMGenerationResult:
        config_kwargs = {
            "response_mime_type": "application/json",
            "temperature": temperature,
        }
        if schema is not None:
            config_kwargs["response_json_schema"] = self._build_response_json_schema(schema)
        if max_output_tokens is not None:
            config_kwargs["max_output_tokens"] = int(max_output_tokens)
        response = self.client.models.generate_content(
            model=model_name,
            contents=prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        usage = self._extract_usage_metadata(response)
        return LLMGenerationResult(
            text=self._strip_code_fences(getattr(response, "text", "")),
            usage_metadata=usage,
        )

    def _build_response_json_schema(self, schema: Any) -> Any:
        if inspect.isclass(schema) and issubclass(schema, BaseModel):
            schema_payload = schema.model_json_schema()
        elif isinstance(schema, BaseModel):
            schema_payload = type(schema).model_json_schema()
        elif hasattr(schema, "model_json_schema"):
            schema_payload = schema.model_json_schema()
        else:
            schema_payload = schema
        return self._strip_unsupported_schema_keywords(schema_payload)

    def _strip_unsupported_schema_keywords(self, schema: Any) -> Any:
        if isinstance(schema, dict):
            cleaned = {}
            for key, value in schema.items():
                if key == "additionalProperties":
                    continue
                cleaned[key] = self._strip_unsupported_schema_keywords(value)
            return cleaned
        if isinstance(schema, list):
            return [self._strip_unsupported_schema_keywords(item) for item in schema]
        return schema

    def _extract_usage_metadata(self, response: Any) -> dict[str, Any]:
        usage = getattr(response, "usage_metadata", None) or getattr(response, "usageMetadata", None)
        if usage is None:
            return {}
        if isinstance(usage, dict):
            return dict(usage)
        extracted = {}
        for field in (
            "prompt_token_count",
            "candidates_token_count",
            "total_token_count",
            "cached_content_token_count",
            "thoughts_token_count",
        ):
            value = getattr(usage, field, None)
            if value is not None:
                extracted[field] = value
        return extracted


class OpenAICompatibleLLMProvider(BaseLLMProvider):
    provider_name = "openai"

    def __init__(self, api_key: str, base_url: Optional[str] = None, provider_name: str = "openai"):
        super().__init__()
        if OpenAI is None:
            raise RuntimeError("openai is not installed.")
        self.provider_name = provider_name
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = OpenAI(**client_kwargs)

    def generate_json(
        self,
        *,
        prompt: str,
        model_name: str,
        schema: Any = None,
        temperature: float = 0.2,
        max_output_tokens: Optional[int] = None,
    ) -> LLMGenerationResult:
        prompt = self._build_json_only_prompt(prompt)
        request_kwargs = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": self.JSON_ONLY_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
        }
        if max_output_tokens is not None:
            request_kwargs["max_tokens"] = int(max_output_tokens)
        if self.provider_name in {"openai", "deepseek"}:
            request_kwargs["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(**request_kwargs)
        message = response.choices[0].message
        raw_text = message.content or ""
        usage = {}
        response_usage = getattr(response, "usage", None)
        if response_usage is not None:
            usage = {
                key: value
                for key, value in {
                    "prompt_tokens": getattr(response_usage, "prompt_tokens", None),
                    "completion_tokens": getattr(response_usage, "completion_tokens", None),
                    "total_token_count": getattr(response_usage, "total_tokens", None),
                }.items()
                if value is not None
            }
        return LLMGenerationResult(
            text=self._strip_code_fences(raw_text),
            usage_metadata=usage,
        )


class AnthropicLLMProvider(BaseLLMProvider):
    provider_name = "anthropic"

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        super().__init__()
        if anthropic is None:
            raise RuntimeError("anthropic is not installed.")
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**client_kwargs)

    def generate_json(
        self,
        *,
        prompt: str,
        model_name: str,
        schema: Any = None,
        temperature: float = 0.2,
        max_output_tokens: Optional[int] = None,
    ) -> LLMGenerationResult:
        prompt = self._build_json_only_prompt(prompt)
        response = self.client.messages.create(
            model=model_name,
            system=self.JSON_ONLY_SYSTEM_PROMPT,
            temperature=temperature,
            max_tokens=int(max_output_tokens or 4096),
            messages=[{"role": "user", "content": prompt}],
        )
        text_parts = []
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", ""))
        usage = {}
        response_usage = getattr(response, "usage", None)
        if response_usage is not None:
            usage = {
                key: value
                for key, value in {
                    "prompt_tokens": getattr(response_usage, "input_tokens", None),
                    "completion_tokens": getattr(response_usage, "output_tokens", None),
                }.items()
                if value is not None
            }
            if "prompt_tokens" in usage or "completion_tokens" in usage:
                usage["total_token_count"] = int(usage.get("prompt_tokens", 0)) + int(
                    usage.get("completion_tokens", 0)
                )
        return LLMGenerationResult(
            text=self._strip_code_fences("".join(text_parts)),
            usage_metadata=usage,
        )


def create_llm_provider(
    provider: str,
    *,
    api_key: str,
    base_url: Optional[str] = None,
) -> BaseLLMProvider:
    normalized = normalize_provider_name(provider)
    if normalized == "gemini":
        return GeminiLLMProvider(api_key=api_key)
    if normalized in {"openai", "deepseek"}:
        return OpenAICompatibleLLMProvider(
            api_key=api_key,
            base_url=base_url,
            provider_name=normalized,
        )
    if normalized == "anthropic":
        return AnthropicLLMProvider(api_key=api_key, base_url=base_url)
    raise ValueError(f"Unsupported AI provider '{provider}'.")
