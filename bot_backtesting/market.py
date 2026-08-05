"""Feed de datos historicos multi-timeframe (M15/M5/H4) para el backtest.

Modelo de simulacion: se itera barra a barra sobre M15 y se decide en el CIERRE
de cada barra (sin lookahead). El "tick" actual = cierre de la barra M15 en curso;
el motor ve solo barras cuyo open_time < cierre actual.
"""

import bisect
import os

import pandas as pd

from bot import config

M15 = config.TIMEFRAME_CORE
M5 = config.TIMEFRAME_M5
H4 = config.TIMEFRAME_STRUCT

_BAR_SECONDS = {M15: 15 * 60, M5: 5 * 60, H4: 4 * 60 * 60}
_REQUIRED_COLS = ["time", "open", "high", "low", "close"]


def load_frames(file_map):
    """file_map: {timeframe: ruta_csv}. CSV con columnas time(epoch),open,high,low,close."""
    frames = {}
    for tf, path in file_map.items():
        if not os.path.exists(path):
            raise FileNotFoundError(f"Falta el CSV de datos: {path}")
        df = pd.read_csv(path)
        missing = [c for c in _REQUIRED_COLS if c not in df.columns]
        if missing:
            raise ValueError(f"{path}: faltan columnas {missing}")
        df = df[_REQUIRED_COLS].copy()
        df["time"] = df["time"].astype("int64")
        for c in ("open", "high", "low", "close"):
            df[c] = df[c].astype(float)
        df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
        frames[tf] = df
    return frames


class Market:
    """Reloj del backtest + acceso a barras sin lookahead."""

    def __init__(self, frames):
        self.frames = frames
        self.m15 = frames[M15]
        self._times = {tf: df["time"].to_numpy() for tf, df in frames.items()}
        self.i = 0
        self.current_time = 0      # epoch del cierre de la barra M15 en curso
        self.current_price = 0.0   # close de la barra M15 en curso
        self.bar_high = 0.0
        self.bar_low = 0.0

    def __len__(self):
        return len(self.m15)

    def step_to(self, i):
        bar = self.m15.iloc[i]
        self.i = i
        self.current_time = int(bar["time"]) + _BAR_SECONDS[M15]
        self.current_price = float(bar["close"])
        self.bar_high = float(bar["high"])
        self.bar_low = float(bar["low"])

    def start_index_for(self, start_time):
        """Primer indice M15 cuyo cierre >= start_time (epoch)."""
        if not start_time:
            return 0
        closes = self._times[M15] + _BAR_SECONDS[M15]
        return int(bisect.bisect_left(closes, start_time))

    def rates(self, tf, count):
        """Ultimas `count` barras del timeframe con open_time < cierre actual."""
        times = self._times[tf]
        hi = bisect.bisect_left(times, self.current_time)  # barras estrictamente anteriores
        if hi <= 0:
            return None
        lo = max(0, hi - count)
        return self.frames[tf].iloc[lo:hi]
