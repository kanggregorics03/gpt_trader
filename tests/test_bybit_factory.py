import unittest

from exchange.bybit_factory import (
    create_private_exchange,
    create_public_exchange,
    normalize_trading_mode,
)


class TestBybitFactory(unittest.TestCase):
    def test_normalize_trading_mode_prefers_explicit_mode(self):
        self.assertEqual(normalize_trading_mode("live", "true"), "live")
        self.assertEqual(normalize_trading_mode("demo", "false"), "demo")

    def test_normalize_trading_mode_falls_back_to_legacy_testnet(self):
        self.assertEqual(normalize_trading_mode(None, "true"), "testnet")
        self.assertEqual(normalize_trading_mode(None, "false"), "live")

    def test_normalize_trading_mode_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            normalize_trading_mode("paper-money", None)

    def test_public_exchange_stays_on_mainnet(self):
        exchange = create_public_exchange()

        self.assertFalse(exchange.options.get("enableDemoTrading", False))
        self.assertEqual(exchange.hostname, "bybit.com")
        self.assertIn("api.{hostname}", exchange.urls["api"]["public"])

    def test_private_exchange_can_use_demo_trading(self):
        exchange = create_private_exchange("key", "secret", trading_mode="demo")

        self.assertTrue(exchange.options.get("enableDemoTrading"))
        self.assertEqual(exchange.hostname, "bybit.com")
        self.assertIn("api-demo.{hostname}", exchange.urls["api"]["private"])


if __name__ == "__main__":
    unittest.main()
