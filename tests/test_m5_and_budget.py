"""Regresion funcional: M5Engine + MarginGovernor + Hedge Lock.

No necesita MT5 conectado ni Supabase: usa un broker falso y series
sinteticas. Cubre lo que se rompio historicamente o lo que es caro de
descubrir en vivo:

  [1][2]  Surfer M5: abre con SL, una sola posicion, una entrada por vela.
  [3]     Gran Hermano M15 vetando una entrada contra la tendencia mayor.
  [3b]    Reversion: dispara con bandas ATR y queda bloqueada con la formula
          original en puntos fijos (el bug que tenia muerta la rama Scalper).
  [4]     Trailing: histeresis (trail_step) y respeto del stops_level.
  [5]     Time stop: cierra la perdedora estancada, deja correr la ganadora.
  [6][7]  MarginGovernor: reserva para el vecino, escalera de margen,
          protectoras inbloqueables.
  [8]     Dedupe del log BUDGET y probe() silencioso.
  [9]     Flatten del M5 por nivel de margen.
  [10]    Hedge Lock: si el broker rechaza, el SL SOBREVIVE y se loguea ERROR
          (antes se limpiaba el SL antes de cubrir y la posicion quedaba
          desnuda en silencio).
  [11]    Sentinel M15: Op1 abre SIN SL ni TP -- su red es el Hedge Lock, no un
          stop -- y el unico SL que llega a tener lo pone el Smart Trail SIEMPRE
          por encima de la entrada. Ademas el VCB sigue bloqueando la entrada en
          velas anomalas y sigue siendo desactivable por config.

Uso:  python -m tests.test_m5_and_budget      (desde la raiz del repo)
"""
import sys

import numpy as np
import pandas as pd
import MetaTrader5 as mt5

from bot import budget, config
from bot.strategy_m5 import M5Engine


class FakePos:
    def __init__(self, ticket, ptype, volume, price_open, ptime, comment, magic, sl=0.0):
        self.ticket, self.type, self.volume = ticket, ptype, volume
        self.price_open = self.price_current = price_open
        self.time, self.comment, self.magic = ptime, comment, magic
        self.sl, self.tp, self.swap, self.profit = sl, 0.0, 0.0, 0.0


class FakeLogger:
    def __init__(self): self.events = []
    def write(self, t, m, price=0.0, lots=0.0, balance=0.0, ticket=0):
        self.events.append((t, m))


class FakeBroker:
    def __init__(self, price=4000.0, magic=100200, balance=10000.0):
        self.magic, self.shadow = magic, False
        self._price, self._balance = price, balance
        self._pos, self._next = [], 1
        self.orders = []
        self.ml = float("inf")
        self._free = 9000.0
    # specs
    def point(self): return 0.01
    def digits(self): return 2
    def tick_value(self): return 1.0
    def volume_step(self): return 0.01
    def volume_min(self): return 0.01
    def volume_max(self): return 50.0
    def spread(self): return 25
    def stops_level(self): return 30
    def bid(self): return self._price
    def ask(self): return self._price + 0.25
    # cuenta
    def account_balance(self): return self._balance
    def account_equity(self): return self._balance
    def margin_free(self): return self._free
    def margin_level(self): return self.ml
    def stop_out_levels(self): return (100.0, 50.0)
    def hedged_margin_rate(self): return (0.0, False)
    # posiciones
    def positions(self): return list(self._pos)
    def positions_all(self): return list(self._pos)
    def positions_of_magic(self, m): return [p for p in self._pos if p.magic == m]
    def order_margin(self, otype, lots): return 0.0 if lots <= 0 else lots * 100 * self._price / 500.0
    def magic_margin(self, m): return sum(self.order_margin(0, p.volume) for p in self.positions_of_magic(m))
    def own_margin(self): return self.magic_margin(self.magic)
    # ordenes
    def market_order(self, otype, lots, comment, sl=0.0, tp=0.0):
        ptype = mt5.POSITION_TYPE_BUY if otype == mt5.ORDER_TYPE_BUY else mt5.POSITION_TYPE_SELL
        p = FakePos(self._next, ptype, lots, self.ask() if otype == mt5.ORDER_TYPE_BUY else self.bid(),
                    1_700_000_000, comment, self.magic, sl)
        self._next += 1
        self._pos.append(p)
        self.orders.append({"type": otype, "lots": lots, "comment": comment, "sl": sl})
        class R: retcode = mt5.TRADE_RETCODE_DONE; order = p.ticket
        return R()
    def close_position(self, p, comment="Close"):
        self._pos.remove(p)
        class R: retcode = mt5.TRADE_RETCODE_DONE
        return R()
    def modify_sl(self, p, sl): p.sl = float(sl); return None
    def history_deals(self, a, b): return []


def series(n, start, drift, noise=0.0, seed=1, pull=None, pn=0):
    """Serie sintetica. `pull`/`pn`: deriva distinta en las ultimas pn barras."""
    rng = np.random.default_rng(seed)
    d = np.full(n, float(drift))
    if pn:
        d[-pn:] = float(pull)
    closes = start + np.cumsum(d + rng.normal(0, noise, n))
    return pd.DataFrame({
        "time": pd.to_datetime(np.arange(n) * 300, unit="s"),
        "open": closes - d, "high": closes + noise,
        "low": closes - noise, "close": closes,
        "tick_volume": np.full(n, 100),
    })


# Regimenes calibrados (ver busqueda en el historial de la sesion):
#   SURFER    -> ADX 58.1, RSI 60.9, close por encima de la EMA50
#   REVERSION -> ADX 24.5, RSI 17.7, ruptura de la banda inferior
SURFER_M5 = dict(n=300, start=3900, drift=0.40, noise=0.80, seed=11)
REVERSION_M5 = dict(n=300, start=4000, drift=0.0, noise=0.60, seed=5, pull=-1.2, pn=4)
UP_M15 = dict(n=300, start=3900, drift=0.9, noise=0.10, seed=3)
DOWN_M15 = dict(n=300, start=4100, drift=-1.2, noise=0.10, seed=4)


class TestEngine(M5Engine):
    def __init__(self, broker, logger, df5, df15):
        super().__init__(broker, logger)
        self._df5, self._df15 = df5, df15
    def _fetch_tick(self):
        class T: time = 1_700_050_000  # jueves ~12:00 UTC
        return T()
    def _rates(self, timeframe, count=200, min_bars=60):
        return self._df15.copy() if timeframe == config.TIMEFRAME_CORE else self._df5.copy()


def run():
    fails = []

    def check(name, cond, extra=""):
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))
        if not cond: fails.append(name)

    # ---------- 1. Surfer alcista: debe abrir BUY con SL ----------
    b, lg = FakeBroker(), FakeLogger()
    df5 = series(**SURFER_M5)
    b._price = float(df5["close"].iloc[-1])
    eng = TestEngine(b, lg, df5, series(**UP_M15))
    eng.cfg = {}
    eng.on_tick()
    print("\n[1] Surfer alcista")
    print("     ADX=%.1f RSI=%.1f close1=%.2f ema1=%.2f bias=%d" %
          (eng.adx1, eng.rsi1, eng.close1, eng.ema1, eng.bigbro_bias))
    check("abre exactamente 1 orden", len(b.orders) == 1, str(b.orders))
    if b.orders:
        o = b.orders[0]
        check("la orden es BUY", o["type"] == mt5.ORDER_TYPE_BUY)
        check("lleva SL != 0 (fix vs grinder actual)", o["sl"] > 0, f"sl={o['sl']:.2f}")
        check("SL por debajo del precio en un BUY", o["sl"] < b.ask(), f"{o['sl']:.2f} < {b.ask():.2f}")
        check("SL respeta el piso duro de puntos",
              (b.ask() - o["sl"]) >= 1000 * b.point() - 1e-6,
              f"dist={(b.ask()-o['sl'])/b.point():.0f} pt")
        check("comment identifica al M5", o["comment"].startswith("M5 "), o["comment"])

    # ---------- 2. Una sola posicion viva + gate de vela ----------
    n_before = len(b.orders)
    eng.on_tick()
    check("no abre una segunda con posicion viva", len(b.orders) == n_before)
    b._pos.clear()
    eng.on_tick()
    check("no reabre en la misma vela (last_candle_time)", len(b.orders) == n_before)

    # ---------- 3. Gran Hermano veta contra la tendencia M15 ----------
    b2, lg2 = FakeBroker(), FakeLogger()
    df5b = series(**SURFER_M5)
    b2._price = float(df5b["close"].iloc[-1])
    eng2 = TestEngine(b2, lg2, df5b, series(**DOWN_M15))  # M15 cayendo
    eng2.cfg = {}
    eng2.on_tick()
    print("\n[3] Gran Hermano bajista con senal de compra M5")
    print("     bias=%d  ordenes=%d" % (eng2.bigbro_bias, len(b2.orders)))
    check("bias M15 = -1", eng2.bigbro_bias == -1)
    check("veta la compra contra el M15", len(b2.orders) == 0)

    # ---------- 3b. Scalper (reversion): la rama que el EA original tenia muerta ----
    print("\n[3b] Scalper / reversion - bandas ATR vs formula original")
    dfrev = series(**REVERSION_M5)
    b9, lg9 = FakeBroker(), FakeLogger()
    b9._price = float(dfrev["close"].iloc[-1])
    eng9 = TestEngine(b9, lg9, dfrev, series(**UP_M15))
    eng9.cfg = {}                       # m5_band_mode = 'atr' (default)
    eng9.on_tick()
    print("     ADX=%.1f RSI=%.1f  ordenes=%d" % (eng9.adx1, eng9.rsi1, len(b9.orders)))
    check("regimen de rango (ADX < 30)", eng9.adx1 < 30, f"{eng9.adx1:.1f}")
    check("con bandas ATR, la reversion DISPARA", len(b9.orders) == 1, str(b9.orders))
    if b9.orders:
        check("la reversion compra en sobreventa", b9.orders[0]["type"] == mt5.ORDER_TYPE_BUY)
        check("la reversion tambien lleva SL", b9.orders[0]["sl"] > 0)

    # Misma vela, formula legacy del EA (ancho = precio * 0.20% * factorADX).
    # A precio de oro actual el ancho resultante desborda cualquier techo
    # razonable => la rama queda bloqueada, que es el bug que se corrigio.
    b10, lg10 = FakeBroker(), FakeLogger()
    b10._price = float(dfrev["close"].iloc[-1])
    eng10 = TestEngine(b10, lg10, dfrev, series(**UP_M15))
    eng10.cfg = {"m5_band_mode": "legacy"}
    eng10.on_tick()
    tol_legacy = eng10._dynamic_tolerance(eng10.adx1, eng10.close1, eng10.atr1)
    print("     legacy: ancho=%.2f  (%.2f ATR)  techo=%.2f ATR"
          % (2 * tol_legacy, 2 * tol_legacy / eng10.atr1, 2.5))
    check("la formula original bloquea la misma senal", len(b10.orders) == 0,
          f"ancho={2*tol_legacy/eng10.atr1:.1f} ATR")

    # ---------- 4. Trailing con histeresis y stops_level ----------
    b3, lg3 = FakeBroker(), FakeLogger()
    eng3 = TestEngine(b3, lg3, df5, series(**UP_M15))
    eng3.cfg = {}
    eng3.now = 1_700_050_000
    p = FakePos(99, mt5.POSITION_TYPE_BUY, 0.1, 4000.0, 1_700_049_000, "M5 Surfer Buy", b3.magic)
    p.price_current = 4002.0   # +200 pt: supera trail_start=100
    p.profit = 20.0
    b3._pos.append(p)
    eng3._manage_exits()
    print("\n[4] Trailing")
    print("     sl=%.2f (esperado ~%.2f)" % (p.sl, 4002.0 - 50 * 0.01))
    check("arma el trailing con +200 pt", p.sl > 0)
    check("SL por encima del open (asegura verde)", p.sl > p.price_open)
    prev = p.sl
    p.price_current = 4002.02  # +2 pt: por debajo del trail_step=10
    eng3._manage_exits()
    check("histeresis: no mueve el SL por 2 pt", p.sl == prev, f"{prev:.2f} -> {p.sl:.2f}")
    p.price_current = 4002.50  # +50 pt: supera el step
    eng3._manage_exits()
    check("si mueve el SL al superar el step", p.sl > prev, f"{prev:.2f} -> {p.sl:.2f}")

    # ---------- 5. Time stop ----------
    b4, lg4 = FakeBroker(), FakeLogger()
    eng4 = TestEngine(b4, lg4, df5, series(**UP_M15))
    eng4.cfg = {}
    eng4.now = 1_700_050_000
    old = FakePos(7, mt5.POSITION_TYPE_BUY, 0.1, 4000.0, 1_700_050_000 - 46 * 60, "M5 Surfer Buy", b4.magic)
    old.profit = -12.0
    b4._pos.append(old)
    eng4._manage_exits()
    print("\n[5] Time stop")
    check("cierra la perdedora estancada a los 45 min", len(b4._pos) == 0)
    b4._pos.clear()
    win = FakePos(8, mt5.POSITION_TYPE_BUY, 0.1, 4000.0, 1_700_050_000 - 46 * 60, "M5 Surfer Buy", b4.magic)
    win.profit = 15.0
    b4._pos.append(win)
    eng4._manage_exits()
    check("deja correr la ganadora estancada", len(b4._pos) == 1)

    # ---------- 6. MarginGovernor: reserva para el vecino M15 ----------
    print("\n[6] MarginGovernor")
    b5, lg5 = FakeBroker(magic=config.MAGIC_M5), FakeLogger()
    gov = budget.MarginGovernor(b5, lambda: {"margin_cap_pct": 0.0}, budget.ROLE_JUNIOR,
                                peer_magics=(config.MAGIC_M15,), logger=lg5)
    check("sin vecino, reserva 0", gov.peer_reserve() == 0.0)
    b5._pos.append(FakePos(1, mt5.POSITION_TYPE_BUY, 1.0, 4000.0, 0, "SMC Buy Entry", config.MAGIC_M15))
    res = gov.peer_reserve()
    check("con M15 descubierto, reserva > 0", res > 0, f"{res:.2f}")
    b5._free = res * 0.5
    check("bloquea al M5 si solo queda la reserva",
          not gov.can_open(0.05, mt5.ORDER_TYPE_BUY, budget.KIND_ADDITIVE), gov.last_block or "")
    b5._free = res + 5000.0
    check("permite al M5 si sobra margen",
          gov.can_open(0.05, mt5.ORDER_TYPE_BUY, budget.KIND_ADDITIVE), gov.last_block or "")
    # hedge cubre: reserva vuelve a 0
    b5._pos.append(FakePos(2, mt5.POSITION_TYPE_SELL, 1.0, 4000.0, 0, "Hedge Lock", config.MAGIC_M15))
    check("con el M15 ya cubierto, reserva vuelve a 0", gov.peer_reserve() == 0.0)

    # senior no reserva
    gov_s = budget.MarginGovernor(b5, lambda: {}, budget.ROLE_SENIOR,
                                  peer_magics=(config.MAGIC_M5,), logger=lg5)
    check("el senior no reserva para nadie", gov_s.peer_reserve() == 0.0)
    check("protectora nunca se bloquea",
          gov_s.can_open(99.0, mt5.ORDER_TYPE_SELL, budget.KIND_PROTECTIVE))

    # ---------- 7. Escalera de margen ----------
    b6 = FakeBroker(magic=config.MAGIC_M5)
    cfg_m5 = {"ml_no_add": 400.0, "ml_flatten": 250.0, "margin_cap_pct": 0.0}
    gov6 = budget.MarginGovernor(b6, lambda: cfg_m5, budget.ROLE_JUNIOR, logger=FakeLogger())
    print("\n[7] Escalera de margen")
    b6.ml = 800.0; check("ML 800% -> NORMAL", gov6.posture() == budget.POSTURE_NORMAL)
    b6.ml = 350.0; check("ML 350% -> NO_ADD", gov6.posture() == budget.POSTURE_NO_ADD)
    b6.ml = 200.0; check("ML 200% -> FLATTEN", gov6.posture() == budget.POSTURE_FLATTEN)
    check("should_flatten a 200%", gov6.should_flatten())
    b6.ml = 350.0
    check("NO_ADD bloquea aditivas",
          not gov6.can_open(0.05, mt5.ORDER_TYPE_BUY, budget.KIND_ADDITIVE))
    check("NO_ADD NO bloquea protectoras",
          gov6.can_open(0.05, mt5.ORDER_TYPE_BUY, budget.KIND_PROTECTIVE))

    # ---------- 8. Dedupe del log BUDGET ----------
    lg7 = FakeLogger()
    b7 = FakeBroker(magic=config.MAGIC_M5); b7.ml = 350.0
    gov7 = budget.MarginGovernor(b7, lambda: cfg_m5, budget.ROLE_JUNIOR, logger=lg7)
    for _ in range(20):
        gov7.can_open(0.05, mt5.ORDER_TYPE_BUY, budget.KIND_ADDITIVE)
    n_log = sum(1 for t, _ in lg7.events if t == "BUDGET")
    print("\n[8] Dedupe de log")
    check("20 bloqueos con el mismo motivo -> 1 log", n_log == 1, f"n={n_log}")
    n_before8 = len(lg7.events)
    for _ in range(5):
        gov7.probe(0.05, mt5.ORDER_TYPE_BUY, budget.KIND_ADDITIVE)
    check("probe() no loguea nada", len(lg7.events) == n_before8)

    # ---------- 9. Flatten del M5 por margen ----------
    b8, lg8 = FakeBroker(), FakeLogger()
    eng8 = TestEngine(b8, lg8, df5, series(**UP_M15))
    eng8.cfg = {"ml_flatten": 250.0}
    b8.ml = 200.0
    b8._pos.append(FakePos(11, mt5.POSITION_TYPE_BUY, 0.05, 4000.0, 0, "M5 Surfer Buy", b8.magic))
    eng8.on_tick()
    print("\n[9] Flatten por margen")
    check("cierra lo suyo al entrar en FLATTEN", len(b8._pos) == 0)
    check("no abre nada en FLATTEN", len(b8.orders) == 0)

    # ---------- 10. Fase 1: Hedge Lock nunca deja la posicion desnuda ----------
    print("\n[10] Hedge Lock (Sentinel M15)")
    from bot.strategy import SentinelEngine

    class HedgeBroker(FakeBroker):
        """Broker que puede rechazar la orden, para probar el camino de fallo."""
        def __init__(self, accept=True, **kw):
            super().__init__(**kw)
            self.accept = accept
            self.sl_calls = []
        def market_order(self, otype, lots, comment, sl=0.0, tp=0.0):
            if not self.accept:
                self.orders.append({"type": otype, "lots": lots, "comment": comment, "rejected": True})
                class R: retcode = 10019  # TRADE_RETCODE_NO_MONEY
                return R()
            return super().market_order(otype, lots, comment, sl, tp)
        def modify_sl(self, p, sl):
            self.sl_calls.append(float(sl))
            p.sl = float(sl)

    # 10a. El broker RECHAZA el hedge -> el SL debe sobrevivir y avisar
    bh = HedgeBroker(accept=False, magic=config.MAGIC_M15)
    lgh = FakeLogger()
    op1 = FakePos(1, mt5.POSITION_TYPE_BUY, 0.5, 4000.0, 0, "SMC Buy Entry", config.MAGIC_M15, sl=3980.0)
    bh._pos.append(op1)
    eh = SentinelEngine(bh, lgh)
    eh.cfg = {}
    ok = eh._open_hedge(op1)
    errs = [m for t, m in lgh.events if t == "ERROR"]
    check("hedge rechazado -> devuelve False", ok is False)
    check("hedge rechazado -> el SL NO se toca", op1.sl == 3980.0, f"sl={op1.sl}")
    check("hedge rechazado -> no se llamo a modify_sl", bh.sl_calls == [], str(bh.sl_calls))
    check("hedge rechazado -> loguea ERROR", len(errs) == 1, str(errs)[:90])

    # 10b. El broker ACEPTA -> se abre la cobertura y recien ahi se suelta el SL
    bh2 = HedgeBroker(accept=True, magic=config.MAGIC_M15)
    lgh2 = FakeLogger()
    op1b = FakePos(1, mt5.POSITION_TYPE_BUY, 0.5, 4000.0, 0, "SMC Buy Entry", config.MAGIC_M15, sl=3980.0)
    bh2._pos.append(op1b)
    eh2 = SentinelEngine(bh2, lgh2)
    eh2.cfg = {}
    ok2 = eh2._open_hedge(op1b)
    check("hedge aceptado -> devuelve True", ok2 is True)
    check("hedge aceptado -> abre al lado contrario",
          len(bh2.orders) == 1 and bh2.orders[0]["type"] == mt5.ORDER_TYPE_SELL, str(bh2.orders))
    check("hedge aceptado -> mismo volumen que Op1", bh2.orders[0]["lots"] == 0.5)
    check("hedge aceptado -> AHORA si limpia el SL", bh2.sl_calls == [0.0], str(bh2.sl_calls))
    check("hedge aceptado -> sin ERROR",
          not [m for t, m in lgh2.events if t == "ERROR"])

    # 10c. La protectora ignora el presupuesto aunque no haya margen
    bh3 = HedgeBroker(accept=True, magic=config.MAGIC_M15)
    bh3._free = 0.0
    bh3.ml = 120.0     # por debajo de ml_no_add del M15 (200)
    lgh3 = FakeLogger()
    op1c = FakePos(1, mt5.POSITION_TYPE_SELL, 0.5, 4000.0, 0, "SMC Sell Entry", config.MAGIC_M15, sl=4020.0)
    bh3._pos.append(op1c)
    eh3 = SentinelEngine(bh3, lgh3)
    eh3.cfg = {}
    check("con margen 0 y ML 120%, el M15 NO abre aditivas",
          not eh3.gov.can_open(0.5, mt5.ORDER_TYPE_BUY, budget.KIND_ADDITIVE))
    check("...pero el Hedge Lock si entra", eh3._open_hedge(op1c) is True)

    # ---------- 11. M15: Op1 sin SL + VCB intacto ----------
    print("\n[11] Sentinel M15: Op1 sin SL, gestionada por Smart Trail")
    from bot import indicators as _ind

    class M15Engine(SentinelEngine):
        """SentinelEngine con el feed sustituido (mismos seams que el backtest)."""
        def __init__(self, broker, logger, df15, df5, dfh4):
            super().__init__(broker, logger)
            self._d15, self._d5, self._dh4 = df15, df5, dfh4
        def _fetch_tick(self):
            class T: time = 1_700_050_000
            return T()
        def _rates(self, timeframe, count=150, min_bars=60):
            if timeframe == config.TIMEFRAME_STRUCT: return self._dh4.copy()
            if timeframe == config.TIMEFRAME_M5: return self._d5.copy()
            return self._d15.copy()

    def m15_engine(broker, logger, df15=None):
        d15 = df15 if df15 is not None else series(n=300, start=3900, drift=0.20, noise=1.2, seed=7)
        # El precio del broker debe ser coherente con las velas servidas, o el
        # gate de pullback (distancia a la EMA) bloquea la entrada.
        broker._price = float(d15["close"].iloc[-1])
        return M15Engine(broker, logger, d15,
                         series(n=300, start=3900, drift=0.07, noise=0.4, seed=8),
                         series(n=300, start=3900, drift=1.5, noise=2.0, seed=9))

    # La señal de entrada depende de fractales H4/M15; aqui se fuerza para
    # aislar lo que se quiere probar: como se CONSTRUYE la orden de Op1.
    _orig_h4, _orig_brk = _ind.get_h4_structure, _ind.check_m15_breakout
    _ind.get_h4_structure = lambda df, use=True: 1
    _ind.check_m15_breakout = lambda df: 1
    try:
        bm, lgm = FakeBroker(magic=config.MAGIC_M15), FakeLogger()
        em = m15_engine(bm, lgm)
        em.cfg = {}
        em.on_tick()
        check("Op1 abre", len(bm.orders) == 1, str(bm.orders)[:110])
        if bm.orders:
            check("Op1 abre SIN SL (la red es el Hedge Lock, no un stop)",
                  bm.orders[0]["sl"] == 0.0, f"sl={bm.orders[0]['sl']}")
            check("Op1 abre SIN TP", bm._pos[0].tp == 0.0)

        # VCB: vela M15 en curso anomala -> bloquea la entrada, misma señal.
        dfv = series(n=300, start=3900, drift=0.20, noise=1.2, seed=7)
        i = dfv.index[-1]
        atr_ref = float(_ind.atr(dfv, 14).iloc[-1])
        dfv.loc[i, "high"] = dfv.loc[i, "close"] + atr_ref * 2.0
        dfv.loc[i, "low"] = dfv.loc[i, "close"] - atr_ref * 2.0   # rango = 4.0 ATR > 2.8
        bv, lgv = FakeBroker(magic=config.MAGIC_M15), FakeLogger()
        ev = m15_engine(bv, lgv, dfv)
        ev.cfg = {}
        ev.on_tick()
        check("VCB detecta la vela anomala", ev.vol_breaker is True)
        check("VCB bloquea la entrada de Op1", len(bv.orders) == 0, str(bv.orders)[:110])
        check("VCB loguea la transicion", any(t == "VCB" for t, _ in lgv.events))

        # VCB desactivable por config, como antes.
        bv2 = FakeBroker(magic=config.MAGIC_M15)
        ev2 = m15_engine(bv2, FakeLogger(), dfv)
        ev2.cfg = {"use_vol_breaker": False}
        ev2.on_tick()
        check("use_vol_breaker=False desactiva el VCB", ev2.vol_breaker is False)
    finally:
        _ind.get_h4_structure, _ind.check_m15_breakout = _orig_h4, _orig_brk

    # Smart Trail: unica via por la que Op1 recibe un SL, y siempre en verde.
    bt, lgt = FakeBroker(magic=config.MAGIC_M15), FakeLogger()
    et = m15_engine(bt, lgt)
    et.cfg = {}
    et._compute_buffers()
    atr_m15 = et._effective_atr()
    op = FakePos(1, mt5.POSITION_TYPE_BUY, 0.2, 3900.0, 1_700_000_000, "SMC Buy Entry", config.MAGIC_M15)
    op.price_open = bt._price
    op.price_current = bt._price + atr_m15 * 1.6   # supera trail_activate = 1.0 ATR
    bt._pos.append(op)
    bt._price = op.price_current
    et.on_tick()
    print("     ATR M15=%.2f  precio=%.2f  SL=%.2f" % (atr_m15, op.price_current, op.sl))
    check("Smart Trail arma el SL de Op1 en beneficio", op.sl > 0)
    check("el SL del Smart Trail queda POR ENCIMA de la entrada (nunca es un stop de perdida)",
          op.sl > op.price_open, f"{op.sl:.2f} > {op.price_open:.2f}")

    # ---------- 12. Guard de identidad: BOT_ID vs MAGIC_NUMBER ----------
    print("\n[12] Guard de identidad de la instancia")
    import importlib
    import os as _os

    def reload_config(bot_id, magic, m15=None, m5=None):
        prev = {k: _os.environ.get(k)
                for k in ("BOT_ID", "MAGIC_NUMBER", "MAGIC_M15", "MAGIC_M5")}
        _os.environ["BOT_ID"] = bot_id
        _os.environ["MAGIC_NUMBER"] = str(magic)
        if m15: _os.environ["MAGIC_M15"] = str(m15)
        if m5: _os.environ["MAGIC_M5"] = str(m5)
        try:
            import bot.config as c
            return importlib.reload(c)
        finally:
            for k, v in prev.items():
                if v is None: _os.environ.pop(k, None)
                else: _os.environ[k] = v

    def aborta(bot_id, magic, **kw):
        try:
            reload_config(bot_id, magic, **kw).validate_identity()
            return False
        except SystemExit:
            return True

    check("m15 con su magic arranca", not aborta("m15", 100100))
    check("m5 con su magic arranca", not aborta("m5", 100200))
    check("m5 con el magic del M15 ABORTA (el .env copiado sin tocar)", aborta("m5", 100100))
    check("m15 con el magic del M5 ABORTA", aborta("m15", 100200))
    check("MAGIC_M15 == MAGIC_M5 ABORTA", aborta("m15", 100100, m15=100100, m5=100100))
    check("BOT_ID desconocido avisa pero no aborta",
          len(reload_config("m30", 100300).validate_identity()) == 1)
    reload_config("m15", 100100)  # restaura el modulo para el resto de la sesion

    print("\n" + "=" * 60)
    if fails:
        print("FALLOS (%d): %s" % (len(fails), fails))
        return 1
    print("TODO OK")
    return 0


if __name__ == "__main__":
    sys.exit(run())
