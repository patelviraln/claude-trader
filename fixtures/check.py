import json, sys
sys.path.insert(0, ".")
import pandas as pd
from src.indicator_engine import compute_ema, compute_rsi
from pathlib import Path

for ticker in ["WXYZ", "CALLX", "EMAFAIL", "RSIFAIL", "LOWCONF"]:
    bars = json.loads((Path("fixtures") / (ticker + "_ohlcv.json")).read_text())
    df = pd.DataFrame(bars)
    df["close"] = df["close"].astype(float)
    ema = compute_ema(df, 50)
    rsi = compute_rsi(df, 14)
    close = df["close"].iloc[-1]
    slope_ok = "OK" if ema["slope"] > 0 else "FAIL"
    price_ok = "OK" if close > ema["ema"] else "FAIL"
    rsi_ok = "OK" if 35 <= rsi["rsi"] <= 65 else "FAIL"
    print(f"{ticker}: close={close:.2f} ema={ema['ema']:.2f}({price_ok}) slope={ema['slope']:+.4f}({slope_ok}) rsi={rsi['rsi']:.1f}({rsi_ok})")
