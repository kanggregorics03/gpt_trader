from typing import Optional

import ccxt

from exchange.private_feed import BybitPrivateAccountFeed
from exchange.public_feed import BybitPublicMarketFeed


SUPPORTED_TRADING_MODES = {"live", "demo", "testnet"}


def normalize_trading_mode(
    trading_mode: Optional[str] = None,
    legacy_testnet: Optional[str] = None,
) -> str:
    mode = (trading_mode or "").strip().lower()
    if mode in SUPPORTED_TRADING_MODES:
        return mode
    if mode:
        raise ValueError(
            f"Unsupported BYBIT_TRADING_MODE '{trading_mode}'. "
            f"Expected one of: {', '.join(sorted(SUPPORTED_TRADING_MODES))}."
        )

    if legacy_testnet is not None and str(legacy_testnet).strip().lower() == "true":
        return "testnet"

    return "live"


def create_public_exchange() -> ccxt.bybit:
    return ccxt.bybit(
        {
            "enableRateLimit": True,
            "options": {
                "defaultType": "linear",
            },
        }
    )


def create_private_exchange(api_key: str, api_secret: str, trading_mode: str = "live") -> ccxt.bybit:
    exchange = ccxt.bybit(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "linear",
            },
        }
    )

    if trading_mode == "demo":
        exchange.enable_demo_trading(True)
    elif trading_mode == "testnet":
        exchange.set_sandbox_mode(True)

    return exchange


def create_public_market_feed(symbols: list[str]) -> Optional[BybitPublicMarketFeed]:
    feed = BybitPublicMarketFeed(symbols)
    if not feed.start():
        return None
    return feed


def create_private_account_feed(
    api_key: str,
    api_secret: str,
    trading_mode: str = "live",
) -> Optional[BybitPrivateAccountFeed]:
    feed = BybitPrivateAccountFeed(
        api_key=api_key,
        api_secret=api_secret,
        trading_mode=trading_mode,
    )
    if not feed.start():
        return None
    return feed
