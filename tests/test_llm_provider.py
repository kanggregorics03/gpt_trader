import json
import unittest
from unittest.mock import patch

from ai.llm_provider import (
    AnthropicLLMProvider,
    GeminiLLMProvider,
    OpenAICompatibleLLMProvider,
    normalize_provider_name,
    resolve_api_key,
)
from core.models import TradeIntent


class TestLLMProvider(unittest.TestCase):
    def test_normalize_provider_name_defaults_to_gemini_even_when_other_keys_exist(self):
        with patch.dict(
            "os.environ",
            {
                "AI_PROVIDER": "",
                "OPENAI_API_KEY": "openai-key",
                "DEEPSEEK_API_KEY": "deepseek-key",
                "ANTHROPIC_API_KEY": "anthropic-key",
            },
            clear=False,
        ):
            self.assertEqual(normalize_provider_name(None), "gemini")

    def test_resolve_api_key_prefers_provider_specific_key_before_shared_key(self):
        with patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "anthropic-key",
                "AI_API_KEY": "shared-key",
            },
            clear=False,
        ):
            self.assertEqual(
                resolve_api_key("anthropic", shared_api_key="shared-key"),
                "anthropic-key",
            )

    def test_openai_compatible_provider_enables_json_mode_for_openai(self):
        captured = {}

        class DummyCompletions:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                return type(
                    "Response",
                    (),
                    {
                        "choices": [
                            type(
                                "Choice",
                                (),
                                {"message": type("Message", (), {"content": '{"decision":"HOLD"}'})()},
                            )()
                        ],
                        "usage": type(
                            "Usage",
                            (),
                            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                        )(),
                    },
                )()

        provider = OpenAICompatibleLLMProvider.__new__(OpenAICompatibleLLMProvider)
        provider.provider_name = "openai"
        provider.client = type(
            "Client",
            (),
            {"chat": type("Chat", (), {"completions": DummyCompletions()})()},
        )()

        result = provider.generate_json(prompt="{}", model_name="gpt-4.1-mini")

        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["messages"][0]["role"], "system")
        self.assertIn("Return only valid JSON", captured["messages"][0]["content"])
        self.assertEqual(result.text, '{"decision":"HOLD"}')
        self.assertEqual(result.usage_metadata["total_token_count"], 15)

    def test_openai_compatible_provider_enables_json_mode_for_deepseek(self):
        captured = {}

        class DummyCompletions:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                return type(
                    "Response",
                    (),
                    {
                        "choices": [
                            type(
                                "Choice",
                                (),
                                {"message": type("Message", (), {"content": '{"decision":"HOLD"}'})()},
                            )()
                        ],
                        "usage": None,
                    },
                )()

        provider = OpenAICompatibleLLMProvider.__new__(OpenAICompatibleLLMProvider)
        provider.provider_name = "deepseek"
        provider.client = type(
            "Client",
            (),
            {"chat": type("Chat", (), {"completions": DummyCompletions()})()},
        )()

        provider.generate_json(prompt="{}", model_name="deepseek-chat")

        self.assertEqual(captured["response_format"], {"type": "json_object"})
        self.assertEqual(captured["messages"][0]["role"], "system")
        self.assertEqual(captured["messages"][1]["role"], "user")

    def test_anthropic_provider_includes_json_only_system_prompt(self):
        captured = {}

        class DummyMessages:
            @staticmethod
            def create(**kwargs):
                captured.update(kwargs)
                return type(
                    "Response",
                    (),
                    {
                        "content": [type("Block", (), {"type": "text", "text": '{"decision":"HOLD"}'})()],
                        "usage": type("Usage", (), {"input_tokens": 12, "output_tokens": 7})(),
                    },
                )()

        provider = AnthropicLLMProvider.__new__(AnthropicLLMProvider)
        provider.client = type("Client", (), {"messages": DummyMessages()})()

        result = provider.generate_json(prompt="{}", model_name="claude-3-7-sonnet")

        self.assertIn("Return only valid JSON", captured["system"])
        self.assertEqual(captured["messages"][0]["role"], "user")
        self.assertEqual(result.text, '{"decision":"HOLD"}')
        self.assertEqual(result.usage_metadata["total_token_count"], 19)

    def test_gemini_provider_uses_json_schema_without_additional_properties(self):
        captured = {}

        class DummyModels:
            @staticmethod
            def generate_content(**kwargs):
                captured.update(kwargs)
                return type(
                    "Response",
                    (),
                    {
                        "text": (
                            '{"decision":"HOLD","orders":[],"rationale":["wait"],'
                            '"risk_notes":[],"confidence":0.5}'
                        ),
                        "usage_metadata": None,
                    },
                )()

        provider = GeminiLLMProvider.__new__(GeminiLLMProvider)
        provider.client = type("Client", (), {"models": DummyModels()})()

        result = provider.generate_json(
            prompt="{}",
            model_name="gemini-2.5-pro",
            schema=TradeIntent,
        )

        config = captured["config"]
        self.assertIsNone(getattr(config, "response_schema", None))
        self.assertIsNotNone(getattr(config, "response_json_schema", None))
        self.assertNotIn(
            "additionalProperties",
            json.dumps(config.response_json_schema),
        )
        self.assertEqual(result.text, '{"decision":"HOLD","orders":[],"rationale":["wait"],"risk_notes":[],"confidence":0.5}')


if __name__ == "__main__":
    unittest.main()
