"""Exporta barras historicas reales desde MT5 a CSV para el backtest.

Requiere MetaTrader 5 instalado y logueado. NO opera: solo descarga historico.

Uso:
    python -m bot_backtesting.fetch_data --start 2024-01-01 --end 2024-12-31 \
        --out-dir bot_backtesting/data
"""

import argparse
import datetime
import os

import MetaTrader5 as mt5
import pandas as pd

from bot import config
from .config_bt import DATA_FILES


def _parse(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)


def fetch(symbol, start, end, out_dir):
    if not mt5.initialize():
        raise RuntimeError(f"No se pudo inicializar MT5: {mt5.last_error()}")
    try:
        mt5.symbol_select(symbol, True)
        os.makedirs(out_dir, exist_ok=True)
        safe = symbol.replace("/", "_")
        for tf, pattern in DATA_FILES.items():
            rates = mt5.copy_rates_range(symbol, tf, start, end)
            if rates is None or len(rates) == 0:
                print(f"[WARN] sin datos para timeframe {tf}")
                continue
            df = pd.DataFrame(rates)
            path = os.path.join(out_dir, pattern.format(symbol=safe))
            df.to_csv(path, index=False)
            print(f"  {path}: {len(df)} barras")
    finally:
        mt5.shutdown()


def main():
    ap = argparse.ArgumentParser(description="Descarga historico de MT5 a CSV.")
    ap.add_argument("--symbol", default=config.SYMBOL)
    ap.add_argument("--start", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD")
    ap.add_argument("--out-dir", default=os.path.join("bot_backtesting", "data"))
    args = ap.parse_args()
    fetch(args.symbol, _parse(args.start), _parse(args.end), args.out_dir)


if __name__ == "__main__":
    main()
