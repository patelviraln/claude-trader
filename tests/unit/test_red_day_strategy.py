"""End-to-end tests for RedDayCSPStrategy using the REDDIP/SPY fixtures.

REDDIP fixture: final day -2.76%, SPY -0.55%, beta ~1 → residual ~-2.2% (MIXED).
Chain has a -0.221 delta put at strike 155, expiry 2026-06-22 (DTE 37).
"""
from __future__ import annotations

from datetime import date

from src.adapters.fixture_adapter import FixtureAdapter
from src.attribution import Attribution
from src.strategies.red_day.state import RedDayState
from src.strategies.red_day.strategy import RedDayCSPStrategy, load_red_day_config
from src.strategies.registry import list_strategies

FIXTURE_TODAY = date(2026, 5, 16)
FAR_EARNINGS = date(2026, 9, 1)

CFG = {
    "dte_min": 30,
    "dte_max": 45,
    "delta_target": 0.22,
    "delta_tolerance": 0.03,
    "ivr_floor": 30.0,
    "earnings_buffer_days": 14,
    "block_on_unknown_earnings": True,
    "blackout_dates": [],
    "attribution_enabled": True,
    "thresholds": {"default": -2.0},
    "residual": {"sympathy_max": 1.0, "idiosyncratic_min": 2.5},
    "sector_etfs": {},
}


def _strategy() -> RedDayCSPStrategy:
    s = RedDayCSPStrategy()
    s.load_config(dict(CFG))
    return s


def _context(tmp_path, **overrides):
    ctx = {
        "iv_history_dir": tmp_path / "iv",
        "today": FIXTURE_TODAY,
        "earnings_date": FAR_EARNINGS,
        "attribution": Attribution(category="TRANSIENT", reason="sector sympathy"),
    }
    ctx.update(overrides)
    return ctx


class TestRegistration:
    def test_registered(self):
        assert "red_day_csp" in list_strategies()

    def test_state_schema(self):
        assert _strategy().get_state_schema() is RedDayState


class TestEmitSignalCard:
    def test_red_day_emits_sell_put(self, tmp_path):
        card = _strategy().emit_signal_card(
            "REDDIP", FixtureAdapter(), {}, context=_context(tmp_path))
        assert card.signal_type == "SELL_PUT"
        assert card.strategy_name == "red_day_csp"
        assert len(card.legs) == 1
        leg = card.legs[0]
        assert leg.option_type == "put" and leg.side == "sell"
        assert leg.strike == 155.0
        assert leg.delta_estimate == -0.221
        assert card.payload["residual_tag"] == "MIXED"
        assert card.payload["attribution_category"] == "TRANSIENT"
        assert card.payload["day_move_pct"] == -2.76
        assert card.payload["dte"] == 37

    def test_green_day_blocks_at_dip_trigger(self, tmp_path):
        # WXYZ fixture trends up — dip trigger must stop it before any chain fetch
        card = _strategy().emit_signal_card(
            "WXYZ", FixtureAdapter(), {}, context=_context(tmp_path))
        assert card.signal_type == "NO_SIGNAL"
        assert card.no_signal_reason.startswith("dip_trigger:")

    def test_fundamental_break_blocks(self, tmp_path):
        ctx = _context(tmp_path, attribution=Attribution(
            category="FUNDAMENTAL_BREAK", reason="guidance cut"))
        card = _strategy().emit_signal_card("REDDIP", FixtureAdapter(), {}, context=ctx)
        assert card.signal_type == "NO_SIGNAL"
        assert card.no_signal_reason.startswith("attribution:")
        assert "guidance cut" in card.no_signal_reason

    def test_no_news_on_mixed_residual_blocks(self, tmp_path):
        ctx = _context(tmp_path, attribution=Attribution(
            category="NO_NEWS", reason="nothing found"))
        card = _strategy().emit_signal_card("REDDIP", FixtureAdapter(), {}, context=ctx)
        assert card.signal_type == "NO_SIGNAL"
        assert "Unexplained" in card.no_signal_reason

    def test_open_position_blocks(self, tmp_path):
        state = {"open_put_strike": 155.0, "open_put_expiry": "2026-06-22"}
        card = _strategy().emit_signal_card(
            "REDDIP", FixtureAdapter(), state, context=_context(tmp_path))
        assert card.signal_type == "NO_SIGNAL"
        assert card.no_signal_reason.startswith("open_position_gate:")

    def test_unknown_earnings_blocks(self, tmp_path):
        ctx = _context(tmp_path, earnings_date=None)
        card = _strategy().emit_signal_card("REDDIP", FixtureAdapter(), {}, context=ctx)
        assert card.signal_type == "NO_SIGNAL"
        assert card.no_signal_reason.startswith("earnings_gate:")

    def test_earnings_cap_starves_chain(self, tmp_path):
        # Earnings 2026-06-30, buffer 14 → expiries must end by 2026-06-16,
        # cap 31 DTE. The only fixture expiry is 2026-06-22 (37 DTE) → no chain.
        ctx = _context(tmp_path, earnings_date=date(2026, 6, 30))
        card = _strategy().emit_signal_card("REDDIP", FixtureAdapter(), {}, context=ctx)
        assert card.signal_type == "NO_SIGNAL"
        assert card.no_signal_reason.startswith("options_chain:")

    def test_mixed_residual_carries_risk_flag(self, tmp_path):
        card = _strategy().emit_signal_card(
            "REDDIP", FixtureAdapter(), {}, context=_context(tmp_path))
        assert any("MIXED" in f for f in card.risk_flags)

    def test_iv_history_written(self, tmp_path):
        ctx = _context(tmp_path)
        _strategy().emit_signal_card("REDDIP", FixtureAdapter(), {}, context=ctx)
        assert (tmp_path / "iv" / "REDDIP.jsonl").exists()


class TestState:
    def test_roundtrip(self):
        s = RedDayState(open_put_strike=150.0, open_put_expiry="2026-08-21",
                        entry_date="2026-07-13")
        d = s.model_dump()
        assert RedDayState.from_dict(d) == s

    def test_from_dict_ignores_unknown_keys(self):
        s = RedDayState.from_dict({"open_put_strike": 1.0, "bogus": True})
        assert s.open_put_strike == 1.0

    def test_phase_is_always_a(self):
        assert RedDayState.default().phase == "A"


class TestSimulateTrade:
    def test_no_legs_returns_zero_pnl(self, tmp_path):
        from src.signal_output import SignalCard
        card = SignalCard(strategy_name="red_day_csp", ticker="REDDIP",
                          signal_type="NO_SIGNAL", underlying_price=168.81)
        adapter = FixtureAdapter()
        future = adapter.get_ohlcv("REDDIP", bars=60)
        result = _strategy().simulate_trade(card, future)
        assert result.pnl == 0.0


class TestConfigLoader:
    def test_loads_toml(self, tmp_path):
        p = tmp_path / "red_day_csp.toml"
        p.write_text('[red_day_csp]\ndte_min = 25\n', encoding="utf-8")
        cfg = load_red_day_config(p)
        assert cfg["dte_min"] == 25
