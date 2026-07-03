"""Unit tests for src/pnl_ledger.py — realized P&L from fill history."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.pnl_ledger import compute_realized_pnl, load_fills, sync_fills

ROUTER_CFG = {
    "router": {
        "default_strategy": "wheel",
        "assignments": {"NVDA": "wheel", "TLT": "rsi2"},
    }
}


def _fill(symbol, side, qty, price, order_id="o1", filled_at="2026-07-01T14:00:00Z"):
    return {"order_id": order_id, "symbol": symbol, "side": side,
            "filled_qty": qty, "filled_avg_price": price, "filled_at": filled_at,
            "status": "OrderStatus.FILLED"}


class TestComputeRealizedPnl:
    def test_short_option_round_trip(self):
        # Sold put at 7.50, bought back at 3.00 → +$450 per contract
        fills = [
            _fill("NVDA260626P00840000", "sell", 1, 7.50, "o1"),
            _fill("NVDA260626P00840000", "buy", 1, 3.00, "o2"),
        ]
        result = compute_realized_pnl(fills, ROUTER_CFG)
        assert result["total"] == 450.0
        assert result["by_strategy"] == {"wheel": 450.0}
        assert result["by_symbol"][0]["closed_qty"] == 1

    def test_equity_round_trip(self):
        fills = [
            _fill("TLT", "buy", 12, 103.91, "o1"),
            _fill("TLT", "sell", 12, 106.00, "o2"),
        ]
        result = compute_realized_pnl(fills, ROUTER_CFG)
        assert result["total"] == pytest.approx((106.00 - 103.91) * 12, abs=0.01)
        assert result["by_strategy"] == {"rsi2": pytest.approx(25.08, abs=0.01)}

    def test_open_position_contributes_nothing(self):
        fills = [_fill("NVDA260626P00840000", "sell", 1, 7.50)]
        result = compute_realized_pnl(fills, ROUTER_CFG)
        assert result["total"] == 0.0
        assert result["by_symbol"][0]["open_qty"] == 1

    def test_partial_close_matches_min_qty(self):
        fills = [
            _fill("TLT", "buy", 10, 100.0, "o1"),
            _fill("TLT", "sell", 4, 110.0, "o2"),
        ]
        result = compute_realized_pnl(fills, ROUTER_CFG)
        assert result["total"] == pytest.approx(40.0)   # 4 shares × $10
        assert result["by_symbol"][0]["open_qty"] == 6

    def test_losing_round_trip_negative(self):
        fills = [
            _fill("NVDA260626P00840000", "sell", 1, 7.50, "o1"),
            _fill("NVDA260626P00840000", "buy", 1, 16.00, "o2"),  # stop-loss close
        ]
        result = compute_realized_pnl(fills, ROUTER_CFG)
        assert result["total"] == -850.0

    def test_empty_fills(self):
        result = compute_realized_pnl([], ROUTER_CFG)
        assert result == {"total": 0.0, "by_strategy": {}, "by_symbol": []}


class TestSyncFills:
    def _executor(self, orders):
        ex = MagicMock()
        ex.get_closed_orders.return_value = orders
        return ex

    def test_appends_new_and_dedupes(self, tmp_path):
        path = tmp_path / "fills.jsonl"
        orders = [_fill("TLT", "buy", 12, 103.91, "ord-A")]
        ex = self._executor(orders)

        assert sync_fills(ex, path) == 1
        assert sync_fills(ex, path) == 0          # same order again → deduped
        assert len(load_fills(path)) == 1

        orders.append(_fill("TLT", "sell", 12, 106.0, "ord-B"))
        assert sync_fills(ex, path) == 1
        assert len(load_fills(path)) == 2

    def test_skips_orders_without_fill_price(self, tmp_path):
        path = tmp_path / "fills.jsonl"
        ex = self._executor([_fill("TLT", "buy", 12, None, "ord-X")])
        assert sync_fills(ex, path) == 0

    def test_fills_sorted_by_time(self, tmp_path):
        path = tmp_path / "fills.jsonl"
        ex = self._executor([
            _fill("TLT", "sell", 1, 105.0, "o2", "2026-07-02T14:00:00Z"),
            _fill("TLT", "buy", 1, 100.0, "o1", "2026-07-01T14:00:00Z"),
        ])
        sync_fills(ex, path)
        fills = load_fills(path)
        assert fills[0]["order_id"] == "o1"
