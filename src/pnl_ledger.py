"""
Realized P&L ledger — the number the whole system exists to produce.

sync_fills() pulls filled orders from Alpaca into fills.jsonl (deduped by
order_id) so fill history survives Alpaca's own retention limits.
compute_realized_pnl() pairs buys against sells per symbol and rolls the
matched (closed) quantity up per strategy via the router.

Convention: realized P&L per symbol = (avg sell - avg buy) x matched qty
x multiplier (100 for options). Unmatched quantity is still open and does
not contribute — that's unrealized P&L, shown on the Positions page.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

import src.logger as _log_setup  # noqa: F401
from src.positions import parse_occ_symbol

logger = structlog.get_logger(__name__)

FILLS_LOG_PATH = Path("fills.jsonl")


def load_fills(path: str | Path = FILLS_LOG_PATH) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    fills = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            fills.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return fills


def sync_fills(executor, path: str | Path = FILLS_LOG_PATH, days_back: int = 90) -> int:
    """Append new filled orders to the ledger; returns count of new fills."""
    existing_ids = {f.get("order_id") for f in load_fills(path)}
    closed = executor.get_closed_orders(days_back=days_back)

    new = [o for o in closed if o["order_id"] not in existing_ids and o.get("filled_avg_price")]
    if not new:
        logger.info("fills.sync", new=0)
        return 0

    new.sort(key=lambda o: o.get("filled_at") or "")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        for o in new:
            f.write(json.dumps(o, default=str) + "\n")
    logger.info("fills.sync", new=len(new))
    return len(new)


def compute_realized_pnl(
    fills: list[dict[str, Any]],
    router_cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate realized P&L from fill history.

    Returns {"total": float, "by_strategy": {name: float},
             "by_symbol": [{symbol, strategy, realized_pnl, closed_qty, open_qty}]}.
    """
    per_symbol: dict[str, dict[str, float]] = {}
    for f in fills:
        sym = f.get("symbol")
        price = f.get("filled_avg_price")
        qty = float(f.get("filled_qty") or 0)
        if not sym or price is None or qty <= 0:
            continue
        bucket = per_symbol.setdefault(
            sym, {"buy_qty": 0.0, "buy_notional": 0.0, "sell_qty": 0.0, "sell_notional": 0.0}
        )
        if f.get("side") == "buy":
            bucket["buy_qty"] += qty
            bucket["buy_notional"] += qty * float(price)
        else:
            bucket["sell_qty"] += qty
            bucket["sell_notional"] += qty * float(price)

    assignments: dict[str, str] = {}
    default = "wheel"
    if router_cfg:
        assignments = router_cfg.get("router", {}).get("assignments", {})
        default = router_cfg.get("router", {}).get("default_strategy", "wheel")

    total = 0.0
    by_strategy: dict[str, float] = {}
    by_symbol: list[dict[str, Any]] = []

    for sym, b in sorted(per_symbol.items()):
        occ = parse_occ_symbol(sym)
        multiplier = 100 if occ else 1
        underlying = occ["underlying"] if occ else sym
        strategy = assignments.get(underlying, default) if router_cfg else None

        matched = min(b["buy_qty"], b["sell_qty"])
        if matched > 0:
            avg_buy = b["buy_notional"] / b["buy_qty"]
            avg_sell = b["sell_notional"] / b["sell_qty"]
            realized = round((avg_sell - avg_buy) * matched * multiplier, 2)
        else:
            realized = 0.0

        open_qty = abs(b["buy_qty"] - b["sell_qty"])
        total += realized
        if strategy:
            by_strategy[strategy] = round(by_strategy.get(strategy, 0.0) + realized, 2)

        by_symbol.append({
            "symbol": sym,
            "underlying": underlying,
            "strategy": strategy or "—",
            "realized_pnl": realized,
            "closed_qty": matched,
            "open_qty": open_qty,
        })

    return {"total": round(total, 2), "by_strategy": by_strategy, "by_symbol": by_symbol}


def realized_pnl_summary(
    executor=None,
    router_cfg: dict[str, Any] | None = None,
    path: str | Path = FILLS_LOG_PATH,
) -> dict[str, Any]:
    """Convenience: sync fills (when an executor is given) then compute the rollup."""
    if executor is not None:
        try:
            sync_fills(executor, path)
        except Exception as exc:
            logger.error("fills.sync_failed", error=str(exc))
    return compute_realized_pnl(load_fills(path), router_cfg)
