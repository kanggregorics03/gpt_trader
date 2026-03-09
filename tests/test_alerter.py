import unittest
import sys
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
