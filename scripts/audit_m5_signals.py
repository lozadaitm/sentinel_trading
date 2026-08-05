"""Fase 0.2: frecuencia de señal del scalper M5 bajo distintas formulas de banda.

READ-ONLY: descarga historico M5/M15 de MT5, replica la logica de entrada de
M5Engine barra a barra y cuenta cuantas señales habria producido cada
formulacion del filtro de ancho de banda.

Por que existe
--------------
El EA original (Grinder_Anterior.mq5) filtraba con `bbWidth < InpMaxBBWidth`,
400 PUNTOS FIJOS. La tolerancia se deriva de price * 0.20% * adxFactor, y la
rama de reversion exige adx < 30, luego adxFactor > 0.4. A precio de oro
actual el ancho minimo posible supera los 640 puntos: la rama Scalper no
dispara NUNCA. Ni siquiera en su epoca era holgada (solo pasaba con adx 27-30).

Este script cuantifica el efecto y permite calibrar m5_max_band_atr con datos
en vez de a ojo.

Uso:  python -m scripts.audit_m5_signals [--barras 20000]
"""

import argparse
import os
import sys
from collections import Counter

import MetaTrader5 as mt5
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import config, indicators  # noqa: E402
from bot.strategy_m5 import DEFAULTS  # noqa: E402


def _init():
    kw = {}
    if config.MT5_PATH:
        kw["path"] = config.MT5_PATH
    if config.MT5_LOGIN:
        kw["login"] = int(config.MT5_LOGIN)
        kw["server"] = config.MT5_SERVER
        kw["password"] = config.MT5_PASSWORD
    if not mt5.initialize(**kw):
        raise SystemExit(f"No se pudo inicializar MT5: {mt5.last_error()}")
    mt5.symbol_select(config.SYMBOL, True)


def _rates(timeframe, count):
    r = mt5.copy_rates_from_pos(config.SYMBOL, timeframe, 0, count)
    if r is None or len(r) == 0:
        raise SystemExit(f"MT5 no devolvio velas para {config.SYMBOL} tf={timeframe}")
    df = pd.DataFrame(r)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    return df


def _adx_factor(adx):
    f = (50.0 - adx) / 50.0
    return 0.2 if f < 0.2 else f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--barras", type=int, default=20000, help="velas M5 a analizar")
    args = ap.parse_args()

    _init()
    sym = mt5.symbol_info(config.SYMBOL)
    point = sym.point

    df5 = _rates(config.TIMEFRAME_M5, args.barras)
    df15 = _rates(config.TIMEFRAME_CORE, max(2000, args.barras // 3))

    ema = indicators.ema(df5["close"], DEFAULTS["m5_ma_period"])
    adx = indicators.adx(df5, DEFAULTS["m5_adx_period"])
    rsi = indicators.rsi(df5, DEFAULTS["m5_rsi_period"])
    atr = indicators.atr(df5, 14)

    # Gran Hermano M15 remuestreado al indice M5 (merge_asof: ultima M15 cerrada).
    ema15 = indicators.ema(df15["close"], DEFAULTS["m5_bigbro_period"])
    bias15 = pd.Series(0, index=df15.index, dtype=int)
    rising = (ema15 > ema15.shift(1)) & (ema15.shift(1) > ema15.shift(2))
    falling = (ema15 < ema15.shift(1)) & (ema15.shift(1) < ema15.shift(2))
    bias15[rising] = 1
    bias15[falling] = -1
    m15 = pd.DataFrame({"time": df15["time"], "bias": bias15.values})
    merged = pd.merge_asof(df5[["time"]].sort_values("time"), m15.sort_values("time"),
                           on="time", direction="backward")
    bias = merged["bias"].fillna(0).astype(int).values

    trend_adx = DEFAULTS["m5_trend_adx"]
    rsi_os, rsi_ob = DEFAULTS["m5_rsi_os"], DEFAULTS["m5_rsi_ob"]
    surfer_max = DEFAULTS["m5_surfer_rsi_max"]
    force_buy, force_sell = DEFAULTS["m5_force_rsi_buy"], DEFAULTS["m5_force_rsi_sell"]

    # Formulas de banda a comparar. Cada una: (nombre, semi_ancho, techo)
    def formulas(i, a, price, at):
        f = _adx_factor(a)
        legacy_tol = price * (DEFAULTS["m5_base_tolerance"] / 100.0) * f
        atr_tol = at * DEFAULTS["m5_band_atr_mult"] * f
        return {
            "legacy (400 pt fijos)": (legacy_tol, 400.0 * point),
            "legacy + techo % precio": (legacy_tol, price * 0.0024),
            "ATR (implementada)": (atr_tol, at * DEFAULTS["m5_max_band_atr"]),
        }

    nombres = list(formulas(0, 20.0, 4000.0, 2.0).keys())
    rev = {n: Counter() for n in nombres}
    surf = Counter()
    bloqueos = {n: Counter() for n in nombres}
    n_eval = 0

    # i = indice de la vela CERRADA sobre la que se decide (como .iloc[-2]).
    start = max(60, DEFAULTS["m5_ma_period"] + 20)
    for i in range(start, len(df5) - 1):
        a, r, e, at = adx.iloc[i], rsi.iloc[i], ema.iloc[i], atr.iloc[i]
        if pd.isna(a) or pd.isna(r) or pd.isna(e) or pd.isna(at) or at <= 0:
            continue
        n_eval += 1
        c1, h1, l1 = df5["close"].iloc[i], df5["high"].iloc[i], df5["low"].iloc[i]
        b = bias[i]
        mes = df5["time"].iloc[i].strftime("%Y-%m")

        if a < trend_adx:
            for nombre, (tol, techo) in formulas(i, a, c1, at).items():
                ancho = 2 * tol
                if ancho > techo:
                    bloqueos[nombre]["ancho"] += 1
                    continue
                up, lo = e + tol, e - tol
                if l1 < lo and r < rsi_os and b != -1:
                    rev[nombre][mes] += 1
                elif h1 > up and r > rsi_ob and b != 1:
                    rev[nombre][mes] += 1
        else:
            if c1 > e and r < surfer_max and r > force_buy and b != -1:
                surf[mes] += 1
            elif c1 < e and r > (100 - surfer_max) and r < force_sell and b != 1:
                surf[mes] += 1

    desde = df5["time"].iloc[start].strftime("%Y-%m-%d")
    hasta = df5["time"].iloc[-1].strftime("%Y-%m-%d")
    meses = sorted(set(df5["time"].dt.strftime("%Y-%m")))
    n_meses = max(1, len(meses))

    print("=" * 78)
    print(f"FRECUENCIA DE SEÑAL M5  |  {config.SYMBOL}  |  {desde} .. {hasta}")
    print(f"velas evaluadas: {n_eval}   meses: {n_meses}   precio final: {df5['close'].iloc[-1]:.2f}")
    print("=" * 78)

    print("\nRAMA SURFER (ADX >= %d) - no depende del filtro de banda" % trend_adx)
    print(f"  total: {sum(surf.values())}   media/mes: {sum(surf.values())/n_meses:.1f}")

    print("\nRAMA SCALPER / REVERSION (ADX < %d) - por formula de banda" % trend_adx)
    print(f"  {'formula':<26} | {'señales':>8} | {'media/mes':>10} | {'bloqueos por ancho':>19}")
    print("  " + "-" * 72)
    for n in nombres:
        tot = sum(rev[n].values())
        print(f"  {n:<26} | {tot:>8} | {tot/n_meses:>10.1f} | {bloqueos[n]['ancho']:>19}")

    legacy = sum(rev[nombres[0]].values())
    actual = sum(rev[nombres[-1]].values())
    print()
    if legacy == 0 and actual > 0:
        print("  => CONFIRMADO: la formula original del EA no produce NINGUNA señal de")
        print("     reversion a este precio. Las bandas ATR recuperan la rama.")
    elif legacy == 0 and actual == 0:
        print("  => Ninguna formula dispara en esta muestra: subir m5_max_band_atr")
        print("     o revisar el resto de gates (RSI, Gran Hermano).")
    else:
        print(f"  => La formula original si dispara ({legacy} señales). Comparar calidad")
        print("     antes de decidir; el cambio a ATR no es obligatorio.")

    print("\nDETALLE MENSUAL (formula ATR implementada)")
    ref = rev[nombres[-1]]
    print(f"  {'mes':<10} | {'reversion':>10} | {'surfer':>8} | {'total':>7}")
    print("  " + "-" * 42)
    for m in meses:
        r_, s_ = ref.get(m, 0), surf.get(m, 0)
        if r_ or s_:
            print(f"  {m:<10} | {r_:>10} | {s_:>8} | {r_+s_:>7}")

    print("\nCALIBRACION")
    print("  m5_max_band_atr actual = %.2f" % DEFAULTS["m5_max_band_atr"])
    print("  Subirlo admite mas reversion (mas señales, peor calidad);")
    print("  bajarlo la restringe a rangos estrechos. Buscar 1-4 señales/dia")
    print("  combinando ambas ramas: por encima de eso es churn.")

    mt5.shutdown()


if __name__ == "__main__":
    main()
