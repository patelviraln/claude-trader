"""
Thin orchestrator: wires adapter → strategy → signal output for a list of tickers.
"""
from __future__ import annotations

from pathlib import Path

import structlog

import src.logger as _log_setup  # noqa: F401
from src.signal_output import SignalCard, append_signal_jsonl, print_signal_card
from src.state_store import get_ticker_state
from src.strategies.wheel.strategy import WheelStrategy, load_wheel_config

logger = structlog.get_logger(__name__)


def run_signal(
    ticker: str,
    strategy: WheelStrategy,
    adapter,
    state_path: str | Path = "wheel_state.json",
    signals_path: str | Path = "signals.jsonl",
    iv_history_dir: Path = Path("iv_history"),
    print_output: bool = True,
    generate_thesis: bool = False,
    execute: bool = False,
    executor=None,
) -> SignalCard:
    """Run the full signal pipeline for a single ticker and return the SignalCard."""
    state = get_ticker_state(state_path, ticker)
    card = strategy.emit_signal_card(
        ticker,
        adapter,
        state,
        context={"iv_history_dir": Path(iv_history_dir)},
    )

    if generate_thesis and card.signal_type != "NO_SIGNAL":
        from src.thesis_generator import generate_thesis as _gen
        card.thesis_text = _gen(card, iv_history_dir=Path(iv_history_dir))

    if execute and executor is not None and card.legs:
        try:
            order = executor.place_order_from_card(card)
            card.order_ids = [order["order_id"]]
            card.order_statuses = [order["status"]]
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
    adapter,
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
            strategy,
            adapter,
            state_path=state_path,
            signals_path=signals_path,
            iv_history_dir=iv_history_dir,
            print_output=print_output,
            generate_thesis=generate_thesis,
            execute=execute,
            executor=executor,
        )
        cards.append(card)
    return cards
