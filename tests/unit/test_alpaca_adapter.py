"""Unit tests for the hardened AlpacaPaperAdapter using mocks."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import datetime as dt

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers to build mock Alpaca SDK objects
# ---------------------------------------------------------------------------

def _make_mock_snapshot(symbol: str, delta: float | None, iv: float | None,
                         bid: float = 1.0, ask: float = 1.1):
    snap = MagicMock()
    snap.greeks = MagicMock()
    snap.greeks.delta = delta
    snap.implied_volatility = iv
    snap.latest_quote = MagicMock()
    snap.latest_quote.bid_price = bid
    snap.latest_quote.ask_price = ask
    return snap


def _make_bars_df(ticker: str, n: int = 60) -> pd.DataFrame:
    prices = [100.0 + i * 0.5 for i in range(n)]
    idx = pd.MultiIndex.from_tuples([(ticker, f"2026-01-{i+1:02d}") for i in range(n)])
    return pd.DataFrame(
        {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1_000_000] * n},
        index=idx,
    )


# ---------------------------------------------------------------------------
# Fixture: adapter with mocked Alpaca clients
# ---------------------------------------------------------------------------

@pytest.fixture()
def adapter():
    with patch.dict("os.environ", {"ALPACA_API_KEY": "fake", "ALPACA_SECRET_KEY": "fake"}):
        with patch("src.adapters.alpaca_adapter.StockHistoricalDataClient") as MockStock, \
             patch("src.adapters.alpaca_adapter.OptionHistoricalDataClient") as MockOption, \
             patch("src.adapters.alpaca_adapter.StockBarsRequest"), \
             patch("src.adapters.alpaca_adapter.OptionChainRequest"), \
             patch("src.adapters.alpaca_adapter.TimeFrame"):
            from src.adapters.alpaca_adapter import AlpacaPaperAdapter
            inst = AlpacaPaperAdapter.__new__(AlpacaPaperAdapter)
            inst._stock_client = MockStock.return_value
            inst._option_client = MockOption.return_value
            yield inst


# ---------------------------------------------------------------------------
# get_ohlcv tests
# ---------------------------------------------------------------------------

class TestGetOhlcv:
    def test_returns_dataframe_with_ohlcv_columns(self, adapter):
        ticker = "NVDA"
        mock_df = _make_bars_df(ticker, n=60)
        adapter._stock_client.get_stock_bars.return_value.df = mock_df
        df = adapter.get_ohlcv(ticker, bars=60)
        assert set(["open", "high", "low", "close", "volume"]).issubset(df.columns)

    def test_raises_if_ticker_not_in_response(self, adapter):
        mock_df = _make_bars_df("OTHER", n=60)
        adapter._stock_client.get_stock_bars.return_value.df = mock_df
        with pytest.raises(RuntimeError, match="No OHLCV data returned"):
            adapter.get_ohlcv("NVDA", bars=60)

    def test_raises_if_insufficient_bars(self, adapter):
        ticker = "NVDA"
        mock_df = _make_bars_df(ticker, n=5)
        adapter._stock_client.get_stock_bars.return_value.df = mock_df
        with pytest.raises(RuntimeError, match="Insufficient OHLCV bars"):
            adapter.get_ohlcv(ticker, bars=60)

    def test_returns_at_most_bars_rows(self, adapter):
        ticker = "NVDA"
        mock_df = _make_bars_df(ticker, n=120)
        adapter._stock_client.get_stock_bars.return_value.df = mock_df
        df = adapter.get_ohlcv(ticker, bars=60)
        assert len(df) == 60


# ---------------------------------------------------------------------------
# get_options_chain tests
# ---------------------------------------------------------------------------

class TestGetOptionsChain:
    def _make_chain(self, symbols_and_greeks: list[tuple]) -> dict:
        chain = {}
        for symbol, delta, iv in symbols_and_greeks:
            chain[symbol] = _make_mock_snapshot(symbol, delta, iv)
        return chain

    def test_returns_dataframe_with_required_columns(self, adapter):
        chain = self._make_chain([
            ("NVDA260626P00210000", -0.30, 0.45),
            ("NVDA260626C00240000", 0.30, 0.42),
        ])
        adapter._option_client.get_option_chain.return_value = chain
        df = adapter.get_options_chain("NVDA", dte_min=30, dte_max=45)
        assert {"strike", "expiry", "option_type", "delta", "iv", "bid", "ask"}.issubset(df.columns)

    def test_skips_contracts_with_no_delta(self, adapter):
        chain = self._make_chain([
            ("NVDA260626P00210000", None, 0.45),
            ("NVDA260626P00200000", -0.25, 0.44),
        ])
        adapter._option_client.get_option_chain.return_value = chain
        df = adapter.get_options_chain("NVDA", dte_min=30, dte_max=45)
        assert len(df) == 1
        assert df.iloc[0]["delta"] == pytest.approx(-0.25)

    def test_skips_contracts_with_no_iv(self, adapter):
        chain = self._make_chain([
            ("NVDA260626P00210000", -0.30, None),
            ("NVDA260626P00200000", -0.25, 0.44),
        ])
        adapter._option_client.get_option_chain.return_value = chain
        df = adapter.get_options_chain("NVDA", dte_min=30, dte_max=45)
        assert len(df) == 1

    def test_returns_empty_df_when_chain_is_empty(self, adapter):
        adapter._option_client.get_option_chain.return_value = {}
        df = adapter.get_options_chain("NVDA", dte_min=30, dte_max=45)
        assert df.empty

    def test_handles_none_bid_ask_gracefully(self, adapter):
        snap = _make_mock_snapshot("NVDA260626P00210000", -0.30, 0.45, bid=None, ask=None)
        snap.latest_quote = None
        adapter._option_client.get_option_chain.return_value = {"NVDA260626P00210000": snap}
        df = adapter.get_options_chain("NVDA", dte_min=30, dte_max=45)
        assert df.iloc[0]["bid"] == 0.0
        assert df.iloc[0]["ask"] == 0.0

    def test_raises_on_api_error(self, adapter):
        adapter._option_client.get_option_chain.side_effect = Exception("API timeout")
        with pytest.raises(RuntimeError, match="Options chain fetch failed"):
            adapter.get_options_chain("NVDA", dte_min=30, dte_max=45)

    def test_skips_malformed_occ_symbols(self, adapter):
        snap_bad = _make_mock_snapshot("BAD_SYMBOL", -0.30, 0.45)
        snap_good = _make_mock_snapshot("NVDA260626P00200000", -0.25, 0.44)
        adapter._option_client.get_option_chain.return_value = {
            "BAD_SYMBOL": snap_bad,
            "NVDA260626P00200000": snap_good,
        }
        df = adapter.get_options_chain("NVDA", dte_min=30, dte_max=45)
        assert len(df) == 1


# ---------------------------------------------------------------------------
# Phase A4 — new adapter methods
# ---------------------------------------------------------------------------

class TestGetMultiOhlcv:
    def test_returns_dict_keyed_by_ticker(self, adapter):
        tickers = ["AAPL", "SPY"]
        import pandas as pd
        # Build multi-index df with both tickers
        idx = pd.MultiIndex.from_tuples(
            [("AAPL", f"2026-01-{i+1:02d}") for i in range(60)]
            + [("SPY", f"2026-01-{i+1:02d}") for i in range(60)]
        )
        prices = [150.0 + i for i in range(60)] + [400.0 + i for i in range(60)]
        mock_df = pd.DataFrame(
            {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1_000_000] * 120},
            index=idx,
        )
        adapter._stock_client.get_stock_bars.return_value.df = mock_df
        result = adapter.get_multi_ohlcv(tickers, bars=60)
        assert set(result.keys()) == {"AAPL", "SPY"}
        assert len(result["AAPL"]) == 60
        assert len(result["SPY"]) == 60

    def test_returns_empty_df_for_missing_ticker(self, adapter):
        ticker_in_response = "AAPL"
        import pandas as pd
        idx = pd.MultiIndex.from_tuples(
            [(ticker_in_response, f"2026-01-{i+1:02d}") for i in range(60)]
        )
        prices = [150.0 + i for i in range(60)]
        mock_df = pd.DataFrame(
            {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1_000_000] * 60},
            index=idx,
        )
        adapter._stock_client.get_stock_bars.return_value.df = mock_df
        result = adapter.get_multi_ohlcv(["AAPL", "MISSING"], bars=60)
        assert "AAPL" in result
        assert result["MISSING"].empty


class TestFixtureAdapterA4:
    def test_get_quote_returns_bid_ask_last(self, tmp_path):
        import json
        from src.adapters.fixture_adapter import FixtureAdapter

        ohlcv = [{"timestamp": "2026-05-16T00:00:00Z", "open": 100.0, "high": 101.0,
                  "low": 99.0, "close": 100.0, "volume": 1_000_000}]
        (tmp_path / "WXYZ_ohlcv.json").write_text(json.dumps(ohlcv))
        adapter = FixtureAdapter(tmp_path)
        q = adapter.get_quote("WXYZ")
        assert "bid" in q and "ask" in q and "last" in q
        assert q["ask"] >= q["bid"]
        assert q["last"] == pytest.approx(100.0)

    def test_get_multi_ohlcv_returns_dict(self, tmp_path):
        import json
        from src.adapters.fixture_adapter import FixtureAdapter

        for ticker in ["AAPL", "SPY"]:
            ohlcv = [{"timestamp": "2026-05-16T00:00:00Z", "open": 100.0, "high": 101.0,
                      "low": 99.0, "close": 100.0, "volume": 1_000_000}]
            (tmp_path / f"{ticker}_ohlcv.json").write_text(json.dumps(ohlcv))
        adapter = FixtureAdapter(tmp_path)
        result = adapter.get_multi_ohlcv(["AAPL", "SPY"], bars=1)
        assert set(result.keys()) == {"AAPL", "SPY"}
        assert not result["AAPL"].empty

    def test_get_option_quote_returns_stub(self, tmp_path):
        from src.adapters.fixture_adapter import FixtureAdapter
        adapter = FixtureAdapter(tmp_path)
        q = adapter.get_option_quote("NVDA260626P00840000")
        assert "bid" in q and "ask" in q
