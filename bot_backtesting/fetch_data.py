"""Descarga barras historicas reales desde MT5 para el backtest.

Requiere MetaTrader 5 instalado y logueado. NO opera: solo descarga historico.

Dos usos:
  - In-memory: `fetch_frames(symbol, start, end)` devuelve {timeframe: DataFrame}
    listo para el Market del backtest. Es lo que usa run_backtest (sin CSV).
  - Export a CSV (opcional, para inspeccion/cache):
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

_REQUIRED_COLS = ["time", "open", "high", "low", "close"]


def _parse(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)


def _normalize(rates):
    """Rates de MT5 -> DataFrame normalizado (mismas columnas/tipos que load_frames)."""
    df = pd.DataFrame(rates)
    df = df[_REQUIRED_COLS].copy()
    df["time"] = df["time"].astype("int64")
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    return df.sort_values("time").drop_duplicates("time").reset_index(drop=True)


def fetch_frames(symbol, start, end, verbose=False):
    """Descarga M15/M5/H4 desde MT5 y devuelve {timeframe: DataFrame} en memoria.

    Inicializa y cierra MT5 internamente. Lanza si no hay conexion o si algun
    timeframe vuelve vacio (el backtest necesita los tres).
    """
    if not mt5.initialize():
        raise RuntimeError(f"No se pudo inicializar MT5: {mt5.last_error()}")
    try:
        mt5.symbol_select(symbol, True)
        frames = {}
        for tf in DATA_FILES:
            rates = mt5.copy_rates_range(symbol, tf, start, end)
            if rates is None or len(rates) == 0:
                raise RuntimeError(
                    f"MT5 no devolvio datos para {symbol} timeframe {tf} "
                    f"en {start:%Y-%m-%d}..{end:%Y-%m-%d}. "
                    f"Verifica que el simbolo exista y tenga historico."
                )
            frames[tf] = _normalize(rates)
            if verbose:
                print(f"  tf {tf}: {len(frames[tf])} barras")
        return frames
    finally:
        mt5.shutdown()


def fetch(symbol, start, end, out_dir):
    """Export a CSV (herramienta auxiliar; el backtest ya no la necesita)."""
    frames = fetch_frames(symbol, start, end, verbose=False)
    os.makedirs(out_dir, exist_ok=True)
    safe = symbol.replace("/", "_")
    for tf, pattern in DATA_FILES.items():
        path = os.path.join(out_dir, pattern.format(symbol=safe))
        frames[tf].to_csv(path, index=False)
        print(f"  {path}: {len(frames[tf])} barras")


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
