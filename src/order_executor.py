"""
Alpaca paper trading order executor.

Phase A6 adds OrderIntent / OrderResult / execute() for multi-leg and equity orders.
The legacy place_order_from_card() is kept as a thin shim over execute().
"""
from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

if TYPE_CHECKING:
    from src.signal_output import Leg, SignalCard

# Lazy-import multi-leg request class (requires alpaca-py >= 0.18)
try:
    from alpaca.trading.requests import OptionLegRequest  # type: ignore[import]
    _HAS_MLEG = True
except ImportError:
    _HAS_MLEG = False


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

class OrderResult(BaseModel):
    leg_index: int
    order_id: str
    status: str  # "accepted" | "filled" | "rejected" | ...
    occ_symbol: str | None = None
    filled_price: float | None = None
    metadata: dict[str, Any] = {}


class OrderIntent(BaseModel):
    """Describes a trade to place: one or more legs, strategy context for logging."""
    strategy_name: str
    ticker: str  # underlying symbol (for logging / audit)
    legs: list[Any]  # list[Leg] — typed as Any to avoid circular import at runtime
    notes: str = ""


# ---------------------------------------------------------------------------
# OCC symbol helper
# ---------------------------------------------------------------------------

def build_occ_symbol(ticker: str, expiry: str, option_type: str, strike: float) -> str:
    """Build an OCC option symbol, e.g. NVDA260626P00840000."""
    date = dt.date.fromisoformat(expiry)
    date_str = date.strftime("%y%m%d")
    type_char = "C" if option_type.lower() == "call" else "P"
    strike_int = round(strike * 1000)
    return f"{ticker}{date_str}{type_char}{strike_int:08d}"


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

class AlpacaOrderExecutor:
    """
    Submits paper orders via the Alpaca trading API.
    Supports single-leg equity, single-leg option, and multi-leg option combos.
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

    # ------------------------------------------------------------------
    # Public API — Phase A6
    # ------------------------------------------------------------------

    def execute(self, intent: OrderIntent) -> list[OrderResult]:
        """Route an OrderIntent to the correct Alpaca order type."""
        legs = intent.legs
        if not legs:
            raise ValueError("OrderIntent has no legs")

        if len(legs) == 1:
            leg = legs[0]
            if leg.asset_class == "equity":
                return [self._submit_equity(leg, 0)]
            elif leg.asset_class == "option":
                return [self._submit_single_option(leg, 0)]
            else:
                raise ValueError(f"Unknown asset_class: {leg.asset_class}")

        if all(l.asset_class == "option" for l in legs) and 2 <= len(legs) <= 4:
            return self._submit_mleg(intent)

        raise ValueError(
            f"Unsupported intent shape: {len(legs)} legs, "
            f"asset classes {[l.asset_class for l in legs]}"
        )

    # ------------------------------------------------------------------
    # Account state — Phase 8 (positions & mark-to-market)
    # ------------------------------------------------------------------

    def get_positions(self) -> list[dict[str, Any]]:
        """Return all open positions as plain dicts (see src.positions for parsing)."""
        positions = self._client.get_all_positions()
        out: list[dict[str, Any]] = []
        for p in positions:
            out.append({
                "symbol": p.symbol,
                "asset_class": str(getattr(p, "asset_class", "") or ""),
                "qty": str(p.qty),
                "side": str(p.side),
                "avg_entry_price": str(p.avg_entry_price),
                "current_price": str(p.current_price) if p.current_price is not None else None,
                "market_value": str(p.market_value) if p.market_value is not None else None,
                "unrealized_pl": str(p.unrealized_pl) if p.unrealized_pl is not None else None,
                "unrealized_plpc": str(p.unrealized_plpc) if p.unrealized_plpc is not None else None,
            })
        return out

    def get_account(self) -> dict[str, Any]:
        """Return account equity / buying power snapshot."""
        acct = self._client.get_account()
        return {
            "equity": float(acct.equity) if acct.equity is not None else None,
            "cash": float(acct.cash) if acct.cash is not None else None,
            "buying_power": float(acct.buying_power) if acct.buying_power is not None else None,
            "options_buying_power": (
                float(acct.options_buying_power)
                if getattr(acct, "options_buying_power", None) is not None else None
            ),
            "portfolio_value": (
                float(acct.portfolio_value) if acct.portfolio_value is not None else None
            ),
        }

    def get_closed_orders(self, days_back: int = 90) -> list[dict[str, Any]]:
        """Return filled orders (any fill qty > 0) from the last N days as plain dicts."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        after = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_back)
        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=after, limit=500)
        orders = self._client.get_orders(req)

        out: list[dict[str, Any]] = []
        for o in orders:
            filled_qty = float(o.filled_qty or 0)
            if filled_qty <= 0:
                continue
            out.append({
                "order_id": str(o.id),
                "symbol": o.symbol,
                "side": str(o.side).replace("OrderSide.", "").lower(),
                "filled_qty": filled_qty,
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else None,
                "filled_at": o.filled_at.isoformat() if o.filled_at else None,
                "status": str(o.status),
            })
        return out

    def close_position(self, symbol: str, qty: float | None = None) -> dict[str, Any]:
        """Close an open position (market order on the opposite side).

        For short options this submits a buy-to-close; for equity, a sell.
        qty=None closes the entire position.
        """
        try:
            if qty is not None:
                from alpaca.trading.requests import ClosePositionRequest
                order = self._client.close_position(
                    symbol, ClosePositionRequest(qty=str(qty))
                )
            else:
                order = self._client.close_position(symbol)
        except Exception as exc:
            raise RuntimeError(f"Close failed for {symbol}: {exc}") from exc

        return {
            "order_id": str(order.id),
            "symbol": symbol,
            "status": str(order.status),
        }

    # ------------------------------------------------------------------
    # Legacy shim — kept for backward compat with signal_engine
    # ------------------------------------------------------------------

    def place_order_from_card(self, card: "SignalCard") -> dict:
        """
        Place a day-limit sell-to-open order for the first leg of a SignalCard.
        Returns a dict with order_id, occ_symbol, limit_price, and status.
        """
        if card.signal_type not in ("SELL_PUT", "SELL_CALL"):
            raise ValueError(f"Cannot place order for signal type '{card.signal_type}'")

        if not card.legs:
            raise ValueError(f"Card for {card.ticker} has no legs")

        leg = card.legs[0]
        if leg.strike is None or leg.expiry is None:
            raise ValueError(f"Card for {card.ticker} is missing strike/expiry")

        if leg.limit_price is None or leg.limit_price <= 0:
            raise ValueError(
                f"No valid mid price for {card.ticker} — cannot determine limit price"
            )

        option_type = leg.option_type or ("put" if card.signal_type == "SELL_PUT" else "call")
        occ_symbol = build_occ_symbol(card.ticker, leg.expiry, option_type, leg.strike)
        limit_price = round(leg.limit_price, 2)

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

    # ------------------------------------------------------------------
    # Private routing helpers
    # ------------------------------------------------------------------

    def _submit_equity(self, leg: "Leg", idx: int) -> OrderResult:
        side = OrderSide.BUY if leg.side == "buy" else OrderSide.SELL
        try:
            if leg.limit_price is not None:
                req = LimitOrderRequest(
                    symbol=leg.symbol,
                    qty=leg.qty,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(leg.limit_price, 2),
                )
            else:
                req = MarketOrderRequest(
                    symbol=leg.symbol,
                    qty=leg.qty,
                    side=side,
                    time_in_force=TimeInForce.DAY,
                )
            order = self._client.submit_order(req)
        except Exception as exc:
            raise RuntimeError(f"Equity order failed for {leg.symbol}: {exc}") from exc

        return OrderResult(
            leg_index=idx,
            order_id=str(order.id),
            status=str(order.status),
        )

    def _submit_single_option(self, leg: "Leg", idx: int) -> OrderResult:
        if leg.strike is None or leg.expiry is None or leg.option_type is None:
            raise ValueError(f"Option leg missing strike/expiry/option_type: {leg}")
        occ = build_occ_symbol(
            leg.symbol, leg.expiry, leg.option_type, leg.strike
        )
        limit_price = round(leg.limit_price, 2) if leg.limit_price else None
        if limit_price is None or limit_price <= 0:
            raise ValueError(f"No valid limit_price for option leg {occ}")

        side = OrderSide.SELL if leg.side == "sell" else OrderSide.BUY
        try:
            req = LimitOrderRequest(
                symbol=occ,
                qty=leg.qty,
                side=side,
                type="limit",
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
            order = self._client.submit_order(req)
        except Exception as exc:
            raise RuntimeError(f"Option order failed for {occ}: {exc}") from exc

        return OrderResult(
            leg_index=idx,
            order_id=str(order.id),
            status=str(order.status),
            occ_symbol=occ,
            filled_price=limit_price,
        )

    def _submit_mleg(self, intent: OrderIntent) -> list[OrderResult]:
        """Submit a 2–4 leg option combo order via Alpaca's mleg API."""
        if not _HAS_MLEG:
            raise RuntimeError(
                "Multi-leg orders require alpaca-py >= 0.18 with OptionLegRequest. "
                "Run: pip install 'alpaca-py>=0.18'"
            )
        option_legs = []
        occ_symbols = []
        for leg in intent.legs:
            if leg.strike is None or leg.expiry is None or leg.option_type is None:
                raise ValueError(f"Option leg missing required fields: {leg}")
            occ = build_occ_symbol(leg.symbol, leg.expiry, leg.option_type, leg.strike)
            occ_symbols.append(occ)
            side = OrderSide.SELL if leg.side == "sell" else OrderSide.BUY
            option_legs.append(
                OptionLegRequest(symbol=occ, side=side, ratio_qty=leg.qty)
            )

        # Net credit = sum(sell limit_prices) - sum(buy limit_prices)
        net_credit = sum(
            (leg.limit_price or 0.0) * (1 if leg.side == "sell" else -1)
            for leg in intent.legs
        )
        limit_price = round(abs(net_credit), 2)
        order_side = OrderSide.SELL if net_credit >= 0 else OrderSide.BUY

        try:
            from alpaca.trading.enums import OrderClass
            req = LimitOrderRequest(
                symbol=intent.ticker,
                qty=1,
                side=order_side,
                order_class=OrderClass.MLEG,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                legs=option_legs,  # type: ignore[arg-type]
            )
            order = self._client.submit_order(req)
        except Exception as exc:
            raise RuntimeError(
                f"Multi-leg order failed for {intent.ticker} "
                f"({', '.join(occ_symbols)}): {exc}"
            ) from exc

        results = []
        for i, occ in enumerate(occ_symbols):
            results.append(
                OrderResult(
                    leg_index=i,
                    order_id=str(order.id),
                    status=str(order.status),
                    occ_symbol=occ,
                )
            )
        return results
