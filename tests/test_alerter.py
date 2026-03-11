import unittest
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("loguru", SimpleNamespace(logger=SimpleNamespace(log=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None)))

from utils.alerter import AlertManager


class TestAlertManager(unittest.TestCase):
    def test_build_payload_defaults_to_generic_json(self):
        manager = AlertManager(webhook_url="https://example.com/hooks")

        payload = manager._build_payload("Execution Timeout", "Order unresolved", "error")

        self.assertEqual(
            payload,
            {
                "title": "Execution Timeout",
                "message": "Order unresolved",
                "level": "error",
            },
        )

    def test_auto_detects_slack_webhook(self):
        manager = AlertManager(webhook_url="https://hooks.slack.com/services/T000/B000/XXXX")

        payload = manager._build_payload("Risk Engine Blocked Trade", "Cooldown active", "warning")

        self.assertEqual(
            payload,
            {
                "text": "*Risk Engine Blocked Trade*\nCooldown active\n_Level: WARNING_",
            },
        )

    def test_auto_detects_discord_webhook(self):
        manager = AlertManager(webhook_url="https://discord.com/api/webhooks/123/abc")

        payload = manager._build_payload("Trading Loop Error", "5m cycle failed", "error")

        self.assertEqual(
            payload,
            {
                "content": "**Trading Loop Error**\n5m cycle failed\n`level: ERROR`",
            },
        )

    def test_explicit_format_overrides_url_detection(self):
        manager = AlertManager(
            webhook_url="https://example.com/custom-webhook",
            webhook_format="slack",
        )

        payload = manager._build_payload("Recovery Sweep", "Recovered 2 orders", "info")

        self.assertEqual(
            payload,
            {
                "text": "*Recovery Sweep*\nRecovered 2 orders\n_Level: INFO_",
            },
        )

    def test_normalizes_legacy_discord_host(self):
        manager = AlertManager(webhook_url="https://discordapp.com/api/webhooks/123/abc")

        self.assertEqual(
            manager._resolved_webhook_url(),
            "https://discord.com/api/webhooks/123/abc",
        )

    def test_non_discord_webhook_url_is_preserved(self):
        manager = AlertManager(webhook_url="https://example.com/hooks")

        self.assertEqual(
            manager._resolved_webhook_url(),
            "https://example.com/hooks",
        )

    def test_send_uses_normalized_discord_url_and_user_agent(self):
        manager = AlertManager(webhook_url="https://discordapp.com/api/webhooks/123/abc")
        captured = {}

        class DummyResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["timeout"] = timeout
            return DummyResponse()

        with patch("utils.alerter.request.urlopen", side_effect=fake_urlopen):
            manager.send("Cycle Decision", "orders=none", level="info")

        self.assertEqual(captured["url"], "https://discord.com/api/webhooks/123/abc")
        self.assertEqual(captured["timeout"], 5)
        self.assertIn("User-agent", captured["headers"])
        self.assertEqual(captured["headers"]["User-agent"], AlertManager.DEFAULT_USER_AGENT)


if __name__ == "__main__":
    unittest.main()
