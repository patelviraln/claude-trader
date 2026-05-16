"""Regenerate fixture data with realistic GBM price action."""
import json, math, datetime, random
from pathlib import Path

BASE = Path(__file__).parent
TODAY = datetime.date(2026, 5, 16)


def make_ohlcv(n=60, base_price=180.0, drift=0.002, vol=0.015, seed=42):
    random.seed(seed)
    bars = []
    price = base_price
    start = TODAY - datetime.timedelta(days=n + 25)
    day = start
    count = 0
    while count < n:
        if day.weekday() < 5:
            ret = drift + vol * random.gauss(0, 1)
            close = round(max(1.0, price * (1 + ret)), 2)
            daily_range = price * vol * 1.5
            high = round(max(price, close) + daily_range * random.random() * 0.5, 2)
            low = round(max(0.01, min(price, close) - daily_range * random.random() * 0.5), 2)
            open_ = round(price * (1 + vol * 0.3 * (random.random() - 0.5)), 2)
            vol_shares = int(random.uniform(3_000_000, 8_000_000))
            bars.append({
                "timestamp": day.isoformat() + "T00:00:00+00:00",
                "open": open_, "high": high, "low": low, "close": close, "volume": vol_shares,
            })
            price = close
            count += 1
        day += datetime.timedelta(days=1)
    return bars


def make_options(underlying_price, dte=37, num_strikes=12):
    expiry = TODAY + datetime.timedelta(days=dte)
    step = max(1.0, round(underlying_price * 0.025, 0))
    center = round(underlying_price / step) * step
    strikes = [center + (i - num_strikes // 2) * step for i in range(num_strikes)]
    contracts = []
    for s in strikes:
        mono = (s - underlying_price) / underlying_price
        # Correct put delta: OTM put (strike < underlying, mono < 0) → delta near 0
        # ITM put (strike > underlying, mono > 0) → delta near -1
        put_delta = round(-1 / (1 + math.exp(-8 * mono)), 3)
        call_delta = round(1 + put_delta, 3)
        iv = round(0.22 + abs(mono) * 0.3, 4)
        for opt_type, delta in [("put", put_delta), ("call", call_delta)]:
            contracts.append({
                "symbol": "FAKE_" + opt_type[0].upper() + str(int(s)),
                "strike": s, "expiry": expiry.isoformat(), "option_type": opt_type,
                "delta": delta, "iv": iv,
                "bid": round(max(0.05, abs(delta) * underlying_price * iv * 0.15), 2),
                "ask": round(max(0.10, abs(delta) * underlying_price * iv * 0.17), 2),
                "volume": max(10, int(800 * abs(delta))), "dte": dte,
            })
    return contracts


# 1. WXYZ — clear SELL_PUT (gentle uptrend, neutral RSI)
b1 = make_ohlcv(60, 180.0, drift=0.0005, vol=0.015, seed=9)
(BASE / "WXYZ_ohlcv.json").write_text(json.dumps(b1, indent=2))
(BASE / "WXYZ_options.json").write_text(json.dumps(make_options(b1[-1]["close"], 37), indent=2))
print("WXYZ close=", b1[-1]["close"])

# 2. CALLX — clear SELL_CALL (gentle uptrend, neutral RSI, phase B in state)
b2 = make_ohlcv(60, 220.0, drift=0.0005, vol=0.018, seed=10)
(BASE / "CALLX_ohlcv.json").write_text(json.dumps(b2, indent=2))
(BASE / "CALLX_options.json").write_text(json.dumps(make_options(b2[-1]["close"], 35), indent=2))
print("CALLX close=", b2[-1]["close"])

# 3. EMAFAIL — downtrend (EMA slope negative)
b3 = make_ohlcv(60, 150.0, drift=-0.005, vol=0.018, seed=3)
(BASE / "EMAFAIL_ohlcv.json").write_text(json.dumps(b3, indent=2))
(BASE / "EMAFAIL_options.json").write_text(json.dumps(make_options(b3[-1]["close"], 40), indent=2))
print("EMAFAIL close=", b3[-1]["close"])

# 4. RSIFAIL — flat then strong spike → RSI > 65
b4 = make_ohlcv(60, 100.0, drift=0.0, vol=0.008, seed=4)
for i in range(-14, 0):
    b4[i]["close"] = round(b4[i]["close"] + (15 + i) * 2.5, 2)
    b4[i]["high"] = round(b4[i]["close"] + 1.0, 2)
    b4[i]["low"] = round(b4[i]["close"] - 0.5, 2)
    b4[i]["open"] = round(b4[i]["close"] - 0.8, 2)
(BASE / "RSIFAIL_ohlcv.json").write_text(json.dumps(b4, indent=2))
(BASE / "RSIFAIL_options.json").write_text(json.dumps(make_options(b4[-1]["close"], 38), indent=2))
print("RSIFAIL close=", b4[-1]["close"])

# 5. LOWCONF — gentle uptrend, passes EMA+RSI, may trip soft flags
b5 = make_ohlcv(60, 95.0, drift=0.002, vol=0.016, seed=5)
(BASE / "LOWCONF_ohlcv.json").write_text(json.dumps(b5, indent=2))
(BASE / "LOWCONF_options.json").write_text(json.dumps(make_options(b5[-1]["close"], 43), indent=2))
print("LOWCONF close=", b5[-1]["close"])

print("All fixtures regenerated.")
