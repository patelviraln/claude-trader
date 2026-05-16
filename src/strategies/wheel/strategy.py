"""
WheelStrategy: two-pass filter chain implementing the Wheel options strategy.

Pass 1 (cheapest first, no I/O): Phase Gate → EMA → RSI → BB
Pass 2 (requires I/O):           Options Chain → Delta → Volume Profile
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel

from src.strategies.base import FilterResult
from src.strategies.registry import register
from src.strategies.wheel.filters import (
    BollingerFilter,
    DeltaTargetFilter,
    EMATrendFilter,
    OptionsChainFilter,
    PhaseGateFilter,
    RSIFilter,
    VolumeProfileFilter,
)
from src.strategies.wheel.state import WheelState


@register("wheel")
class WheelStrategy:
    name = "wheel"

    def __init__(self):
        self._cfg: dict[str, Any] = {}
        # Filters initialised lazily after load_config
        self._phase_gate = PhaseGateFilter()
        self._ema: EMATrendFilter | None = None
        self._rsi: RSIFilter | None = None
        self._bb: BollingerFilter | None = None
        self._options: OptionsChainFilter = OptionsChainFilter()
        self._delta: DeltaTargetFilter | None = None
        self._volume: VolumeProfileFilter | None = None

    # ------------------------------------------------------------------
    # Strategy Protocol methods
    # ------------------------------------------------------------------

    def load_config(self, config: dict[str, Any]) -> None:
        self._cfg = config
        self._ema = EMATrendFilter(ema_period=config.get("ema_period", 50))
        self._rsi = RSIFilter(
            rsi_min=config.get("rsi_min", 35.0),
            rsi_max=config.get("rsi_max", 65.0),
        )
        self._bb = BollingerFilter(
            period=config.get("bb_period", 20),
            std_dev=config.get("bb_std_dev", 2.0),
        )
        self._delta = DeltaTargetFilter(
            delta_target=config.get("delta_target", 0.30),
            delta_tolerance=config.get("delta_tolerance", 0.05),
        )
        self._volume = VolumeProfileFilter(
            lookback=config.get("volume_lookback_bars", 20),
        )

    def get_state_schema(self) -> type[BaseModel]:
        return WheelState

    def evaluate(
        self,
        ticker: str,
        ohlcv: pd.DataFrame,
        options_chain: pd.DataFrame,
        state: dict[str, Any],
    ) -> list[FilterResult]:
        if not self._ema:
            raise RuntimeError("Call load_config() before evaluate()")

        wheel_state = WheelState.from_dict(state) if state else WheelState.default()
        phase: Literal["A", "B"] = wheel_state.phase  # type: ignore[assignment]

        results: list[FilterResult] = []

        # --- Pass 1: cheap local filters ---

        r = self._phase_gate.run(ticker, wheel_state)
        results.append(r)
        if not r.passed and r.hard_stop:
            return results

        r = self._ema.run(ohlcv)
        results.append(r)
        if not r.passed and r.hard_stop:
            return results

        r = self._rsi.run(ohlcv)
        results.append(r)
        if not r.passed and r.hard_stop:
            return results

        r = self._bb.run(ohlcv, phase)
        results.append(r)
        # BB is soft — always continue

        # --- Pass 2: options chain filters (I/O already done by caller) ---

        r = self._options.run(options_chain)
        results.append(r)
        if not r.passed and r.hard_stop:
            return results

        r = self._delta.run(options_chain, phase)
        results.append(r)
        if not r.passed and r.hard_stop:
            return results

        # Extract strike from delta result for volume profile
        recommended_strike: float = r.adjustments.get("recommended_strike", 0.0)

        r = self._volume.run(ohlcv, phase, recommended_strike)
        results.append(r)

        return results


# ------------------------------------------------------------------
# Config loader helper
# ------------------------------------------------------------------

def load_wheel_config(config_path: str | Path = "config/wheel.toml") -> dict[str, Any]:
    path = Path(config_path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return raw.get("wheel", raw)
