"""
Alpaca paper trading order executor for the Wheel strategy.

Places limit sell-to-open orders for the recommended option contract
derived from a SignalCard. Requires ALPACA_API_KEY and ALPACA_SECRET_KEY.

Module-level imports so the TradingClient can be patched in tests.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

if TYPE_CHECKING:
    from src.signal_output import SignalCard


def build_occ_symbol(ticker: str, expiry: str, option_type: str, strike: float) -> str:
    """Build an OCC option symbol, e.g. NVDA260626P00840000."""
    date = dt.date.fromisoformat(expiry)
    date_str = date.strftime("%y%m%d")
    type_char = "C" if option_type.lower() == "call" else "P"
    strike_int = round(strike * 1000)
    return f"{ticker}{date_str}{type_char}{strike_int:08d}"


class AlpacaOrderExecutor:
    """
    Submits paper options orders via the Alpaca trading API.
    One executor instance per session — reuse across tickers.
    """

    def __init__(self) -> None:
        api_key = os.environ["ALPACA_API_KEY"]
        secret_key = os.environ["ALPACA_SECRET_KEY"]
        base_url = os.environ.get("ALPACA_BASE_URL")
        self._client = TradingClient(
            api_key,
            secret_key,
            paper=True,
            **({"url_override": base_url} if base_url else {}),
        )

    def place_order_from_card(self, card: "SignalCard") -> dict:
        """
        Place a day-limit sell-to-open order for the recommended contract.

        Returns a dict with order_id, occ_symbol, limit_price, and status.
        Raises ValueError if the card has no actionable signal or no mid price.
        Raises RuntimeError if the Alpaca API call fails.
        """
        if card.phase not in ("SELL_PUT", "SELL_CALL"):
            raise ValueError(f"Cannot place order for phase '{card.phase}'")

        if card.recommended_strike is None or card.recommended_expiry is None:
            raise ValueError(f"Card for {card.ticker} is missing strike/expiry")

        if card.option_mid is None or card.option_mid <= 0:
            raise ValueError(
                f"No valid mid price for {card.ticker} — cannot determine limit price"
            )

        option_type = "put" if card.phase == "SELL_PUT" else "call"
        occ_symbol = build_occ_symbol(
            card.ticker,
            card.recommended_expiry,
            option_type,
            card.recommended_strike,
        )
        limit_price = round(card.option_mid, 2)

        try:
            order_req = LimitOrderRequest(
                symbol=occ_symbol,
                qty=1,
                side=OrderSide.SELL,
                type="limit",
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
            order = self._client.submit_order(order_req)
        except Exception as exc:
            raise RuntimeError(f"Order submission failed for {occ_symbol}: {exc}") from exc

        return {
            "order_id": str(order.id),
            "occ_symbol": occ_symbol,
            "limit_price": limit_price,
            "status": str(order.status),
        }
