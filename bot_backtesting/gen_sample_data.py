"""Genera datos sinteticos (random-walk) M15/M5/H4 consistentes para smoke-test.

NO sirve para evaluar rentabilidad real (precio inventado); sirve para verificar
que el arnes corre de punta a punta sin MT5. Para resultados reales usa fetch_data.

Uso:
    python -m bot_backtesting.gen_sample_data --days 180 --out-dir bot_backtesting/data
"""

import argparse
import os

import numpy as np
import pandas as pd

from bot import config

_TF = [(config.TIMEFRAME_CORE, 15 * 60, "M15"),
       (config.TIMEFRAME_M5, 5 * 60, "M5"),
       (config.TIMEFRAME_STRUCT, 4 * 60 * 60, "H4")]


def generate(out_dir, symbol, days=180, start="2024-01-01", seed=7, base=2000.0, vol=0.0006):
    os.makedirs(out_dir, exist_ok=True)
    minutes = days * 24 * 60
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0, vol, minutes)
    price = base * np.exp(np.cumsum(rets))
    t0 = int(pd.Timestamp(start, tz="UTC").timestamp())
    times = t0 + np.arange(minutes) * 60
    s = pd.DataFrame({"time": times, "price": price})

    safe = symbol.replace("/", "_")
    for _tf, secs, name in _TF:
        bucket = (s["time"] // secs) * secs
        g = s.groupby(bucket)["price"]
        df = pd.DataFrame({
            "time": g.first().index.astype("int64").to_numpy(),
            "open": g.first().to_numpy(),
            "high": g.max().to_numpy(),
            "low": g.min().to_numpy(),
            "close": g.last().to_numpy(),
        })
        path = os.path.join(out_dir, f"{safe}_{name}.csv")
        df.to_csv(path, index=False)
        print(f"  {path}: {len(df)} barras")


def main():
    ap = argparse.ArgumentParser(description="Genera datos sinteticos para smoke-test.")
    ap.add_argument("--symbol", default=config.SYMBOL)
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out-dir", default=os.path.join("bot_backtesting", "data"))
    args = ap.parse_args()
    generate(args.out_dir, args.symbol, days=args.days, start=args.start, seed=args.seed)


if __name__ == "__main__":
    main()
