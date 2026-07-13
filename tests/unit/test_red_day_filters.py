"""Unit tests for src/strategies/red_day/filters.py."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from src.attribution import Attribution
from src.strategies.red_day.filters import (
    AttributionFilter,
    DipTriggerFilter,
    EarningsGateFilter,
    IVBehaviorFilter,
    OpenPositionGateFilter,
    ResidualFilter,
    compute_beta,
)
from src.strategies.red_day.state import RedDayState

TODAY = date(2026, 7, 13)


def _ohlcv(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.bdate_range(end="2026-07-13", periods=n, tz="UTC")
    return pd.DataFrame({
        "open": closes, "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes], "close": closes,
        "volume": [1_000_000] * n,
    }, index=idx)


def _md(closes, **extra):
    md = {"ticker": "AMD", "ohlcv": _ohlcv(closes), "today": TODAY}
    md.update(extra)
    return md


STATE = RedDayState.default()


class TestOpenPositionGate:
    def test_clear_passes(self):
        r = OpenPositionGateFilter().run(_md([100, 98]), STATE, {})
        assert r.passed

    def test_open_put_hard_stops(self):
        state = RedDayState(open_put_strike=95.0, open_put_expiry="2026-08-21")
        r = OpenPositionGateFilter().run(_md([100, 98]), state, {})
        assert not r.passed and r.hard_stop
        assert "95.0" in r.reason


class TestDipTrigger:
    def test_deep_dip_passes(self):
        r = DipTriggerFilter().run(_md([100, 97.0]), STATE, {"thresholds": {"default": -2.0}})
        assert r.passed
        assert r.adjustments["day_move_pct"] == -3.0

    def test_shallow_dip_blocks(self):
        r = DipTriggerFilter().run(_md([100, 99.0]), STATE, {"thresholds": {"default": -2.0}})
        assert not r.passed and r.hard_stop

    def test_green_day_blocks(self):
        r = DipTriggerFilter().run(_md([100, 101.5]), STATE, {})
        assert not r.passed and r.hard_stop

    def test_per_ticker_override(self):
        cfg = {"thresholds": {"default": -2.0, "AMD": -3.5}}
        r = DipTriggerFilter().run(_md([100, 97.0]), STATE, cfg)  # -3% not enough for AMD
        assert not r.passed
        r2 = DipTriggerFilter().run(_md([100, 96.0]), STATE, cfg)  # -4% is
        assert r2.passed

    def test_too_few_bars_blocks(self):
        r = DipTriggerFilter().run(_md([100]), STATE, {})
        assert not r.passed and r.hard_stop


class TestComputeBeta:
    def test_known_beta_recovered(self):
        rng = np.random.default_rng(7)
        b_ret = rng.normal(0, 0.01, 200)
        s_ret = 1.5 * b_ret  # no noise: exact beta 1.5
        bench = _ohlcv(list(100 * np.exp(np.cumsum(b_ret))))
        stock = _ohlcv(list(100 * np.exp(np.cumsum(s_ret))))
        assert compute_beta(stock, bench) == pytest.approx(1.5, abs=0.05)

    def test_insufficient_data_defaults_to_one(self):
        assert compute_beta(_ohlcv([100, 99]), _ohlcv([500, 499])) == 1.0


class TestResidualFilter:
    def _run(self, stock_moves, spy_moves=None, sector_moves=None, cfg=None):
        # Build series long enough for beta; engineered final-day moves
        rng = np.random.default_rng(3)
        base = rng.normal(0.0004, 0.008, 150)
        stock_ret, spy_ret = base * 1.0, base * 1.0  # beta ~1
        stock_ret = np.append(stock_ret, stock_moves / 100)
        spy_ret = np.append(spy_ret, (spy_moves or 0.0) / 100)
        md = _md(list(100 * np.exp(np.cumsum(stock_ret))))
        if spy_moves is not None:
            md["spy_ohlcv"] = _ohlcv(list(500 * np.exp(np.cumsum(spy_ret))))
        if sector_moves is not None:
            sec_ret = np.append(base, sector_moves / 100)
            md["sector_ohlcv"] = _ohlcv(list(200 * np.exp(np.cumsum(sec_ret))))
        return ResidualFilter().run(md, STATE, cfg or {})

    def test_market_drag_is_sympathy(self):
        r = self._run(stock_moves=-2.5, spy_moves=-2.3)
        assert r.passed
        assert r.adjustments["residual_tag"] == "SYMPATHY"

    def test_idiosyncratic_flagged(self):
        r = self._run(stock_moves=-4.0, spy_moves=0.1)
        assert r.passed  # never a hard stop — attribution decides
        assert r.adjustments["residual_tag"] == "IDIOSYNCRATIC"
        assert r.adjustments.get("soft_flag") == "idiosyncratic_move"

    def test_mixed_band(self):
        r = self._run(stock_moves=-2.0, spy_moves=-0.5)
        assert r.adjustments["residual_tag"] == "MIXED"

    def test_sector_explains_move_forgiving_rule(self):
        # Market residual large, but sector fell as much — sympathy wins
        r = self._run(stock_moves=-4.0, spy_moves=0.0, sector_moves=-3.8)
        assert r.adjustments["residual_tag"] == "SYMPATHY"

    def test_no_benchmarks_treated_idiosyncratic(self):
        r = self._run(stock_moves=-3.0)
        assert r.passed
        assert r.adjustments["residual_tag"] == "IDIOSYNCRATIC"
        assert r.adjustments.get("soft_flag") == "residual_unavailable"


class TestAttributionFilter:
    def _run(self, category, residual_tag="MIXED", cfg=None, reason="r"):
        md = _md([100, 97], residual_tag=residual_tag, day_move_pct=-3.0,
                 attribution=Attribution(category=category, reason=reason))
        return AttributionFilter().run(md, STATE, cfg if cfg is not None else {})

    def test_transient_passes(self):
        r = self._run("TRANSIENT")
        assert r.passed
        assert r.adjustments["attribution_category"] == "TRANSIENT"

    def test_fundamental_break_blocks(self):
        r = self._run("FUNDAMENTAL_BREAK", residual_tag="SYMPATHY")
        assert not r.passed and r.hard_stop

    def test_event_pending_blocks(self):
        r = self._run("EVENT_PENDING", residual_tag="SYMPATHY")
        assert not r.passed and r.hard_stop

    def test_no_news_sympathy_passes(self):
        r = self._run("NO_NEWS", residual_tag="SYMPATHY")
        assert r.passed

    def test_no_news_idiosyncratic_blocks(self):
        r = self._run("NO_NEWS", residual_tag="IDIOSYNCRATIC")
        assert not r.passed and r.hard_stop
        assert "Unexplained" in r.reason

    def test_no_news_mixed_blocks(self):
        r = self._run("NO_NEWS", residual_tag="MIXED")
        assert not r.passed and r.hard_stop

    def test_unavailable_sympathy_passes_with_flag(self):
        r = self._run("UNAVAILABLE", residual_tag="SYMPATHY")
        assert r.passed
        assert r.adjustments.get("soft_flag") == "attribution_unavailable"

    def test_unavailable_mixed_blocks(self):
        r = self._run("UNAVAILABLE", residual_tag="MIXED")
        assert not r.passed and r.hard_stop

    def test_disabled_skips(self):
        md = _md([100, 97], residual_tag="MIXED")
        r = AttributionFilter().run(md, STATE, {"attribution_enabled": False})
        assert r.passed
        assert r.adjustments["attribution_category"] == "SKIPPED"


class TestIVBehaviorFilter:
    def test_no_iv_passes_with_flag(self):
        r = IVBehaviorFilter().run(_md([100, 97], atm_iv=None, iv_series=[]), STATE, {})
        assert r.passed
        assert r.adjustments.get("soft_flag") == "iv_unavailable"

    def test_low_rank_blocks(self):
        # series 0.2..0.6, current 0.25 → rank ~12.5 < 30
        md = _md([100, 97], atm_iv=0.25, iv_series=[0.2, 0.4, 0.6, 0.25])
        r = IVBehaviorFilter().run(md, STATE, {"ivr_floor": 30.0})
        assert not r.passed and r.hard_stop

    def test_rising_iv_passes_clean(self):
        md = _md([100, 97], atm_iv=0.55, iv_series=[0.2, 0.4, 0.55])
        r = IVBehaviorFilter().run(md, STATE, {"ivr_floor": 30.0})
        assert r.passed
        assert "soft_flag" not in r.adjustments

    def test_flat_iv_soft_flags(self):
        md = _md([100, 97], atm_iv=0.4, iv_series=[0.2, 0.6, 0.4])
        r = IVBehaviorFilter().run(md, STATE, {"ivr_floor": 30.0})
        assert r.passed
        assert r.adjustments.get("soft_flag") == "iv_not_expanding"

    def test_no_history_soft_flags(self):
        md = _md([100, 97], atm_iv=0.4, iv_series=[0.4])
        r = IVBehaviorFilter().run(md, STATE, {})
        assert r.passed
        assert r.adjustments.get("soft_flag") == "iv_rank_unavailable"


class TestEarningsGate:
    CFG = {"earnings_buffer_days": 14, "dte_min": 30, "dte_max": 45,
           "block_on_unknown_earnings": True}

    def test_far_earnings_passes_uncapped(self):
        md = _md([100, 97], earnings_date=date(2026, 10, 1))
        r = EarningsGateFilter().run(md, STATE, self.CFG)
        assert r.passed
        assert r.adjustments["dte_max_effective"] == 45

    def test_mid_earnings_caps_expiry_window(self):
        # earnings in 50d, buffer 14 → cap 36 (>= dte_min 30) → pass, capped
        md = _md([100, 97], earnings_date=TODAY.replace(month=9, day=1))  # 50d out
        r = EarningsGateFilter().run(md, STATE, self.CFG)
        assert r.passed
        assert r.adjustments["dte_max_effective"] == 36

    def test_near_earnings_blocks(self):
        md = _md([100, 97], earnings_date=date(2026, 7, 30))  # 17d out, cap 3 < 30
        r = EarningsGateFilter().run(md, STATE, self.CFG)
        assert not r.passed and r.hard_stop

    def test_unknown_blocks_by_default(self):
        md = _md([100, 97], earnings_date=None)
        r = EarningsGateFilter().run(md, STATE, self.CFG)
        assert not r.passed and r.hard_stop
        assert "unknown" in r.reason.lower()

    def test_unknown_allowed_when_configured(self):
        cfg = {**self.CFG, "block_on_unknown_earnings": False}
        md = _md([100, 97], earnings_date=None)
        r = EarningsGateFilter().run(md, STATE, cfg)
        assert r.passed
        assert r.adjustments.get("soft_flag") == "earnings_unverified"

    def test_blackout_date_blocks(self):
        cfg = {**self.CFG, "blackout_dates": ["2026-07-13"]}
        md = _md([100, 97], earnings_date=date(2026, 10, 1))
        r = EarningsGateFilter().run(md, STATE, cfg)
        assert not r.passed and r.hard_stop
        assert "blackout" in r.reason.lower()
