"""Unit tests for src/order_executor.py — mocks the Alpaca TradingClient."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import pytest

from src.signal_output import SignalCard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_card(
    phase="SELL_PUT",
    ticker="NVDA",
    strike=840.0,
    expiry="2026-06-26",
    option_mid=5.50,
) -> SignalCard:
    return SignalCard(
        ticker=ticker,
        phase=phase,
        underlying_price=884.50,
        recommended_strike=strike,
        recommended_expiry=expiry,
        dte=41,
        delta_estimate=-0.29,
        option_mid=option_mid,
        confidence_tier="HIGH",
    )


def _make_order_response(order_id="ord-123", status="accepted") -> MagicMock:
    order = MagicMock()
    order.id = order_id
    order.status = status
    return order


@pytest.fixture()
def executor():
    with patch.dict("os.environ", {"ALPACA_API_KEY": "fake", "ALPACA_SECRET_KEY": "fake"}):
        with patch("src.order_executor.TradingClient") as MockClient:
            from src.order_executor import AlpacaOrderExecutor
            inst = AlpacaOrderExecutor.__new__(AlpacaOrderExecutor)
            inst._client = MockClient.return_value
            yield inst


# ---------------------------------------------------------------------------
# build_occ_symbol
# ---------------------------------------------------------------------------

class TestBuildOccSymbol:
    def test_put_symbol(self):
        from src.order_executor import build_occ_symbol
        assert build_occ_symbol("NVDA", "2026-06-26", "put", 840.0) == "NVDA260626P00840000"

    def test_call_symbol(self):
        from src.order_executor import build_occ_symbol
        assert build_occ_symbol("NVDA", "2026-06-26", "call", 920.0) == "NVDA260626C00920000"

    def test_fractional_strike(self):
        from src.order_executor import build_occ_symbol
        assert build_occ_symbol("AAPL", "2026-06-20", "put", 175.50) == "AAPL260620P00175500"

    def test_low_strike(self):
        from src.order_executor import build_occ_symbol
        assert build_occ_symbol("MSFT", "2026-07-17", "put", 50.0) == "MSFT260717P00050000"

    def test_case_insensitive_option_type(self):
        from src.order_executor import build_occ_symbol
        assert build_occ_symbol("NVDA", "2026-06-26", "PUT", 840.0) == "NVDA260626P00840000"
        assert build_occ_symbol("NVDA", "2026-06-26", "CALL", 840.0) == "NVDA260626C00840000"


# ---------------------------------------------------------------------------
# place_order_from_card
# ---------------------------------------------------------------------------

class TestPlaceOrderFromCard:
    def test_returns_order_dict_on_success(self, executor):
        executor._client.submit_order.return_value = _make_order_response("ord-abc", "accepted")
        card = _make_card(phase="SELL_PUT", strike=840.0, expiry="2026-06-26", option_mid=5.50)
        result = executor.place_order_from_card(card)
        assert result["order_id"] == "ord-abc"
        assert result["status"] == "accepted"
        assert result["occ_symbol"] == "NVDA260626P00840000"
        assert result["limit_price"] == pytest.approx(5.50)

    def test_builds_correct_occ_for_sell_call(self, executor):
        executor._client.submit_order.return_value = _make_order_response()
        card = _make_card(phase="SELL_CALL", ticker="AAPL", strike=190.0, expiry="2026-06-20", option_mid=3.20)
        result = executor.place_order_from_card(card)
        assert result["occ_symbol"] == "AAPL260620C00190000"

    def test_limit_price_rounded_to_cents(self, executor):
        executor._client.submit_order.return_value = _make_order_response()
        card = _make_card(option_mid=5.1234567)
        result = executor.place_order_from_card(card)
        assert result["limit_price"] == pytest.approx(5.12)

    def test_raises_for_no_signal_phase(self, executor):
        card = _make_card()
        card.phase = "NO_SIGNAL"
        with pytest.raises(ValueError, match="Cannot place order"):
            executor.place_order_from_card(card)

    def test_raises_when_strike_missing(self, executor):
        card = _make_card()
        card.recommended_strike = None
        with pytest.raises(ValueError, match="missing strike"):
            executor.place_order_from_card(card)

    def test_raises_when_expiry_missing(self, executor):
        card = _make_card()
        card.recommended_expiry = None
        with pytest.raises(ValueError, match="missing strike"):
            executor.place_order_from_card(card)

    def test_raises_when_option_mid_is_none(self, executor):
        card = _make_card(option_mid=None)
        with pytest.raises(ValueError, match="No valid mid price"):
            executor.place_order_from_card(card)

    def test_raises_when_option_mid_is_zero(self, executor):
        card = _make_card(option_mid=0.0)
        with pytest.raises(ValueError, match="No valid mid price"):
            executor.place_order_from_card(card)

    def test_raises_runtime_error_on_api_failure(self, executor):
        executor._client.submit_order.side_effect = Exception("connection refused")
        card = _make_card()
        with pytest.raises(RuntimeError, match="Order submission failed"):
            executor.place_order_from_card(card)

    def test_submit_order_called_once(self, executor):
        executor._client.submit_order.return_value = _make_order_response()
        executor.place_order_from_card(_make_card())
        assert executor._client.submit_order.call_count == 1
