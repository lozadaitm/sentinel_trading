"""Indicadores tecnicos en pandas/numpy puro (cero dependencias extra).

Replica los indicadores que el EA MQL5 obtiene de MT5:
  iATR, iMA(EMA), iRSI, iADX, iFractals (+ helpers de estructura H4 y breakout M15).

Convencion de indexacion MQL5 <-> pandas:
  El DataFrame viene cronologico ASCENDENTE (ultima fila = barra actual).
  MQL5 [0] (barra actual)  == .iloc[-1]
  MQL5 [1] (barra previa)  == .iloc[-2]
  MQL5 shift s             == .iloc[-1 - s]

ATR y RSI usan suavizado de Wilder para coincidir con iATR/iRSI de MT5.
"""

import numpy as np
import pandas as pd


# ------------------------------------------------------------------
# Basicos
# ------------------------------------------------------------------
def true_range(df):
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    return pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)


def atr(df, period=14):
    """ATR con suavizado de Wilder (igual que iATR de MT5)."""
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi(df, period=14):
    """RSI con suavizado de Wilder (igual que iRSI de MT5)."""
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(100)


def adx(df, period=14):
    """ADX de Wilder. Devuelve la serie ADX (usar .iloc[-1] para el valor actual)."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    tr = true_range(df)
    atr_w = tr.ewm(alpha=1.0 / period, adjust=False).mean()

    plus_di = 100 * plus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_w
    minus_di = 100 * minus_dm.ewm(alpha=1.0 / period, adjust=False).mean() / atr_w

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / di_sum
    adx_series = dx.ewm(alpha=1.0 / period, adjust=False).mean()
    return adx_series.fillna(0)


# ------------------------------------------------------------------
# Fractales (Bill Williams, 5 barras)
# ------------------------------------------------------------------
def _up_fractals(highs):
    """Array float: high[k] si k es fractal superior, NaN si no.

    Un fractal superior en k requiere 2 barras a cada lado; solo puede
    confirmarse cuando existen las 2 barras posteriores (mas recientes).
    """
    n = len(highs)
    out = np.full(n, np.nan)
    for k in range(2, n - 2):
        h = highs[k]
        if h > highs[k - 1] and h > highs[k - 2] and h > highs[k + 1] and h > highs[k + 2]:
            out[k] = h
    return out


def _down_fractals(lows):
    n = len(lows)
    out = np.full(n, np.nan)
    for k in range(2, n - 2):
        lo = lows[k]
        if lo < lows[k - 1] and lo < lows[k - 2] and lo < lows[k + 1] and lo < lows[k + 2]:
            out[k] = lo
    return out


def _frac_at_shift(frac_arr, shift):
    """Valor de fractal en el 'shift' estilo MQL5, o 0 si no hay/fuera de rango."""
    idx = len(frac_arr) - 1 - shift
    if idx < 0 or idx >= len(frac_arr):
        return 0.0
    v = frac_arr[idx]
    return 0.0 if np.isnan(v) else float(v)


def get_h4_structure(df_h4, use_h4_struct=True):
    """Estructura H4 -> 1 (alcista) / -1 (bajista) / 0. Replica GetH4Structure (MQL5 281-299)."""
    if not use_h4_struct:
        return 0

    highs = df_h4["high"].values
    lows = df_h4["low"].values
    up = _up_fractals(highs)
    dn = _down_fractals(lows)

    h1 = h2 = l1 = l2 = 0.0

    c = 0
    for i in range(2, 100):
        f = _frac_at_shift(up, i)
        if f > 0:
            if c == 0:
                h1 = f
            else:
                h2 = f
                break
            c += 1

    c = 0
    for i in range(2, 100):
        f = _frac_at_shift(dn, i)
        if f > 0:
            if c == 0:
                l1 = f
            else:
                l2 = f
                break
            c += 1

    if h1 > h2 and l1 > l2:
        return 1
    if l1 < l2 and h1 < h2:
        return -1
    if l1 < l2:
        return -1
    if h1 > h2:
        return 1
    return 0


def fractal_range(df, max_shift=60):
    """Techo y piso del rango vigente: ultimo fractal superior e inferior
    CONFIRMADOS (5 barras, estilo Bill Williams). -> (high, low); 0.0 si no hay.

    Lo consume el M5 (estructura de rango del Scalper). Es solo-lectura y
    aditivo: el Sentinel M15 no lo usa.
    """
    highs = df["high"].values
    lows = df["low"].values
    up = _up_fractals(highs)
    dn = _down_fractals(lows)

    hi = lo = 0.0
    for i in range(2, max_shift):
        if hi == 0.0:
            f = _frac_at_shift(up, i)
            if f > 0:
                hi = f
        if lo == 0.0:
            f = _frac_at_shift(dn, i)
            if f > 0:
                lo = f
        if hi > 0 and lo > 0:
            break
    return hi, lo


def check_m15_breakout(df_m15):
    """Breakout M15 -> 1 / -1 / 0. Replica CheckM15Breakout (MQL5 301-314)."""
    highs = df_m15["high"].values
    lows = df_m15["low"].values
    closes = df_m15["close"].values
    up = _up_fractals(highs)
    dn = _down_fractals(lows)

    up_f = lo_f = 0.0
    for i in range(3, 30):
        u = _frac_at_shift(up, i)
        l = _frac_at_shift(dn, i)
        if up_f == 0 and u > 0:
            up_f = u
        if lo_f == 0 and l > 0:
            lo_f = l
        if up_f > 0 and lo_f > 0:
            break

    c1 = closes[-2]  # close[1] en MQL5
    if up_f > 0 and c1 > up_f:
        return 1
    if lo_f > 0 and c1 < lo_f:
        return -1
    return 0
