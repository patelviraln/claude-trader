"""
Thin orchestrator: wires adapter → strategy → signal output for a list of tickers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import structlog

import src.logger as _log_setup  # noqa: F401
from src.adapters.base import DataAdapter
from src.indicator_engine import compute_iv_rank
from src.iv_history import append_iv_sample, load_iv_series

logger = structlog.get_logger(__name__)

from src.signal_output import (
    IndicatorReadings,
    SignalCard,
    append_signal_jsonl,
    compute_confidence_tier,
    print_signal_card,
)
from src.state_store import get_ticker_state
from src.strategies.wheel.strategy import WheelStrategy, load_wheel_config


def _get_atm_iv(options_chain: pd.DataFrame, underlying_price: float) -> float | None:
    """Return the IV of the put strike nearest to current underlying price."""
    if options_chain.empty or "iv" not in options_chain.columns:
        return None
    puts = options_chain[options_chain["option_type"] == "put"].copy()
    if puts.empty:
        return None
    puts["strike_dist"] = (puts["strike"] - underlying_price).abs()
    atm = puts.loc[puts["strike_dist"].idxmin()]
    iv = atm.get("iv")
    return float(iv) if pd.notna(iv) else None


def _collect_adjustments(filter_results) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for r in filter_results:
        merged.update(r.adjustments)
    return merged


def _collect_soft_flags(filter_results) -> list[str]:
    return [
        r.adjustments["soft_flag"]
        for r in filter_results
        if r.adjustments.get("soft_flag")
    ]


def _get_contract_mid(
    options_chain: pd.DataFrame,
    strike: float | None,
    expiry: str | None,
    option_type: str,
) -> float | None:
    """Return (bid+ask)/2 for the recommended contract, or None if not found."""
    if options_chain.empty or strike is None or expiry is None:
        return None
    try:
        expiry_date = pd.Timestamp(expiry).date()
    except Exception:
        return None
    mask = (
        (options_chain["strike"] == strike)
        & (options_chain["expiry"].apply(lambda d: d if isinstance(d, type(expiry_date)) else pd.Timestamp(d).date()) == expiry_date)
        & (options_chain["option_type"] == option_type)
    )
    matches = options_chain[mask]
    if matches.empty:
        return None
    row = matches.iloc[0]
    bid = float(row.get("bid") or 0.0)
    ask = float(row.get("ask") or 0.0)
    mid = (bid + ask) / 2
    return round(mid, 4) if mid > 0 else None


def run_signal(
    ticker: str,
    adapter: DataAdapter,
    strategy: WheelStrategy,
    state_path: str | Path = "wheel_state.json",
    signals_path: str | Path = "signals.jsonl",
    iv_history_dir: Path = Path("iv_history"),
    dte_min: int = 30,
    dte_max: int = 45,
    print_output: bool = True,
    generate_thesis: bool = False,
    execute: bool = False,
    executor=None,
) -> SignalCard:
    """Run the full signal pipeline for a single ticker and return the SignalCard."""
    state = get_ticker_state(state_path, ticker)
    phase = state.get("phase", "A")

    # Fetch data
    ohlcv = adapter.get_ohlcv(ticker, bars=60)
    options_chain = adapter.get_options_chain(ticker, dte_min=dte_min, dte_max=dte_max)

    underlying_price = float(ohlcv["close"].iloc[-1])

    # IV rank: persist ATM IV then compute rank from rolling history
    atm_iv = _get_atm_iv(options_chain, underlying_price)
    iv_series = load_iv_series(ticker, Path(iv_history_dir))
    if atm_iv is not None:
        append_iv_sample(ticker, atm_iv, Path(iv_history_dir))
        iv_series = iv_series + [atm_iv]
    iv_rank = compute_iv_rank(atm_iv, iv_series) if atm_iv is not None else None

    # Run filter chain
    filter_results = strategy.evaluate(ticker, ohlcv, options_chain, state)

    # Check for hard stop
    hard_stopped = any(not r.passed and r.hard_stop for r in filter_results)

    if hard_stopped:
        stop_result = next(r for r in filter_results if not r.passed and r.hard_stop)
        logger.info("signal_blocked", ticker=ticker, filter=stop_result.filter_name, reason=stop_result.reason)
        card = SignalCard(
            ticker=ticker,
            phase="NO_SIGNAL",
            underlying_price=underlying_price,
            no_signal_reason=f"{stop_result.filter_name}: {stop_result.reason}",
        )
    else:
        adj = _collect_adjustments(filter_results)
        soft_flags = _collect_soft_flags(filter_results)
        delta_estimate: float | None = adj.get("delta_estimate")
        if delta_estimate is None:
            raise RuntimeError(
                "delta_estimate missing from combined filter adjustments; "
                "verify DeltaTargetFilter ran and passed before signal assembly."
            )
        delta_distance = abs(delta_estimate) - 0.30
        tier, rationale = compute_confidence_tier(soft_flags, abs(delta_distance), iv_rank)

        signal_phase = "SELL_PUT" if phase == "A" else "SELL_CALL"
        logger.info("signal_emitted", ticker=ticker, phase=signal_phase, tier=tier)

        readings: IndicatorReadings | None = None
        if all(k in adj for k in ("ema50", "rsi14", "bb_upper", "bb_lower", "bb_percent_b")):
            readings = IndicatorReadings(
                ema50=adj["ema50"],
                ema50_slope=adj.get("ema50_slope", 0.0),
                rsi14=adj["rsi14"],
                bb_upper=adj["bb_upper"],
                bb_lower=adj["bb_lower"],
                bb_percent_b=adj["bb_percent_b"],
                volume_node_nearest=adj.get("volume_node_nearest", underlying_price),
                volume_node_type=adj.get("volume_node_type", "support"),
            )

        risk_flags = []
        if "bb_unfavorable" in soft_flags:
            risk_flags.append("BB position unfavorable for entry")
        if "volume_node_divergence" in soft_flags:
            pct = adj.get("hvn_divergence_pct", 0)
            risk_flags.append(f"Strike diverges {pct:.1f}% from volume node")

        rec_strike = adj.get("recommended_strike")
        rec_expiry = adj.get("recommended_expiry")
        opt_type = "put" if signal_phase == "SELL_PUT" else "call"
        option_mid = _get_contract_mid(options_chain, rec_strike, rec_expiry, opt_type)

        card = SignalCard(
            ticker=ticker,
            phase=signal_phase,
            underlying_price=underlying_price,
            recommended_strike=rec_strike,
            recommended_expiry=rec_expiry,
            dte=adj.get("dte"),
            delta_estimate=adj.get("delta_estimate"),
            iv_rank=iv_rank,
            indicator_readings=readings,
            option_mid=option_mid,
            confidence_tier=tier,
            confidence_rationale=rationale,
            risk_flags=risk_flags,
        )

    if generate_thesis and card.phase != "NO_SIGNAL":
        from src.thesis_generator import generate_thesis as _gen
        card.thesis_text = _gen(card, iv_history_dir=Path(iv_history_dir))

    if execute and executor is not None and card.phase != "NO_SIGNAL":
        try:
            order = executor.place_order_from_card(card)
            card.order_id = order["order_id"]
            card.order_status = order["status"]
            logger.info(
                "executor.order_placed",
                ticker=ticker,
                occ_symbol=order["occ_symbol"],
                limit_price=order["limit_price"],
                order_id=order["order_id"],
                status=order["status"],
            )
        except Exception as exc:
            logger.error("executor.order_failed", ticker=ticker, error=str(exc))

    if print_output:
        print_signal_card(card)
    append_signal_jsonl(card, signals_path)
    return card


def run_signals_batch(
    tickers: list[str],
    adapter: DataAdapter,
    config_path: str | Path = "config/wheel.toml",
    state_path: str | Path = "wheel_state.json",
    signals_path: str | Path = "signals.jsonl",
    iv_history_dir: Path = Path("iv_history"),
    print_output: bool = True,
    generate_thesis: bool = False,
    execute: bool = False,
    executor=None,
) -> list[SignalCard]:
    cfg = load_wheel_config(config_path)
    strategy = WheelStrategy()
    strategy.load_config(cfg)

    cards = []
    for ticker in tickers:
        card = run_signal(
            ticker,
            adapter,
            strategy,
            state_path=state_path,
            signals_path=signals_path,
            iv_history_dir=iv_history_dir,
            dte_min=cfg.get("dte_min", 30),
            dte_max=cfg.get("dte_max", 45),
            print_output=print_output,
            generate_thesis=generate_thesis,
            execute=execute,
            executor=executor,
        )
        cards.append(card)
    return cards
