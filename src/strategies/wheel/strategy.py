"""
WheelStrategy: two-pass filter chain implementing the Wheel options strategy.

Pass 1 (cheapest first, no I/O): Phase Gate → EMA → RSI → BB
Pass 2 (requires I/O):           Options Chain → Delta → Volume Profile
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pandas as pd
import structlog
from pydantic import BaseModel

import src.logger as _log_setup  # noqa: F401
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

logger = structlog.get_logger(__name__)


@register("wheel")
class WheelStrategy:
    name = "wheel"

    def __init__(self):
        self._cfg: dict[str, Any] = {}
        self._phase_gate = PhaseGateFilter()
        self._ema: EMATrendFilter | None = None
        self._rsi: RSIFilter | None = None
        self._bb: BollingerFilter | None = None
        self._options: OptionsChainFilter = OptionsChainFilter()
        self._delta: DeltaTargetFilter | None = None
        self._volume: VolumeProfileFilter | None = None
        self._filters: list = []

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
        self._filters = [
            self._phase_gate,
            self._ema,
            self._rsi,
            self._bb,
            self._options,
            self._delta,
            self._volume,
        ]

    def get_state_schema(self) -> type[BaseModel]:
        return WheelState

    def evaluate(
        self,
        ticker: str,
        ohlcv: pd.DataFrame,
        options_chain: pd.DataFrame,
        state: dict[str, Any],
    ) -> list[FilterResult]:
        if not self._filters:
            raise RuntimeError("Call load_config() before evaluate()")

        wheel_state = WheelState.from_dict(state) if state else WheelState.default()
        logger.debug("evaluate_start", ticker=ticker, phase=wheel_state.phase)

        market_data: dict[str, Any] = {
            "ticker": ticker,
            "ohlcv": ohlcv,
            "options_chain": options_chain,
        }

        results: list[FilterResult] = []
        for f in self._filters:
            r = f.run(market_data, wheel_state, self._cfg)
            results.append(r)
            market_data.update(r.adjustments)
            if not r.passed and r.hard_stop:
                logger.debug("evaluate_hard_stop", ticker=ticker, filter=f.name, reason=r.reason)
                return results

        logger.debug("evaluate_complete", ticker=ticker, filters_run=len(results))
        return results


# ------------------------------------------------------------------
# Config loader helper
# ------------------------------------------------------------------

def load_wheel_config(config_path: str | Path = "config/wheel.toml") -> dict[str, Any]:
    path = Path(config_path)
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    return raw.get("wheel", raw)
