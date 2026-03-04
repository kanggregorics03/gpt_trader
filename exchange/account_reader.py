import ccxt
import time
from typing import List, Dict, Any
from core.models import AccountState, PositionState
from loguru import logger

class AccountReader:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.exchange = ccxt.bybit({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'linear' # USDT perpetuals
            }
        })
        if testnet:
            self.exchange.set_sandbox_mode(True)
            logger.info("AccountReader connected to Bybit Testnet")
        else:
            logger.warning("AccountReader connected to Bybit MAINNET")

    def get_account_state(self, symbols: List[str]) -> AccountState:
        try:
            # 1. Fetch Balances
            balance = self.exchange.fetch_balance()
            total_balance = balance.get('USDT', {}).get('total', 0.0)
            available_balance = balance.get('USDT', {}).get('free', 0.0)

            # 2. Fetch Open Positions
            positions_data = self.exchange.fetch_positions(symbols)
            active_positions = []
            for pos in positions_data:
                size = float(pos.get('contracts', 0) or 0)
                if size > 0:
                    side = "LONG" if pos.get('side') == 'long' else "SHORT"
                    entry_price = float(pos.get('entryPrice', 0))
                    unrealized_pnl = float(pos.get('unrealizedPnl', 0))
                    
                    # Estimate PnL % if possible, ccxt returns absolute PnL
                    # We approximate it based on margin or entry value
                    margin = float(pos.get('initialMargin', 0) or (size * entry_price))
                    pnl_pct = (unrealized_pnl / margin * 100) if margin > 0 else 0.0

                    active_positions.append(PositionState(
                        symbol=pos['symbol'],
                        side=side,
                        size=size,
                        entry_price=entry_price,
                        unrealized_pnl_pct=pnl_pct
                    ))

            # 3. Fetch Open Orders
            open_orders_count = 0
            for symbol in symbols:
                orders = self.exchange.fetch_open_orders(symbol)
                open_orders_count += len(orders)

            return AccountState(
                total_balance=total_balance,
                available_balance=available_balance,
                positions=active_positions,
                open_orders_count=open_orders_count
            )

        except Exception as e:
            logger.error(f"Failed to fetch account state: {e}")
            raise
