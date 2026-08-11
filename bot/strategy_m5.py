"""M5Engine: port de 'M5_Gold_SmartCut_v13_02' (Grinder_Anterior.mq5) a Python.

Scalper M5 autonomo con dos regimenes conmutados por ADX:
  - Scalper (ADX bajo)  : reversion a la media contra bandas dinamicas.
  - Surfer  (ADX alto)  : continuacion en direccion de la EMA.

Convive con el Sentinel M15 en la MISMA cuenta MT5. Se distinguen por magic
(bot/config.py) y por bot_id en la DB. El reparto de margen lo arbitra
bot/budget.py: este motor es el JUNIOR y cede siempre ante el M15, que no tiene
SL catastrofico y cuyo Hedge Lock es inbloqueable.

Fidelidad al EA original
------------------------
Se conserva lo que hacia bueno al v13.02 y que el grinder embebido en el
Sentinel habia perdido:
  - Decision sobre vela CERRADA (indice [1] de MQL5 == .iloc[-2]). Sin repintado.
  - SL por ATR con piso duro en puntos.
  - Filtro "Gran Hermano" M15: no operar contra la tendencia mayor.
  - Lote dinamico por balance, con tope.
  - Gate de nivel de margen y filtro horario.
  - Trailing de dos velocidades CON histeresis (trail_step) y respeto del
    SYMBOL_TRADE_STOPS_LEVEL del broker.
  - Memoria de racha (lastOpWasWin) para el Surfer.
  - Una sola posicion viva a la vez.

Correcciones sobre el original
------------------------------
  - Filtro de ancho de banda ATR-relativo. El original comparaba el ancho contra
    InpMaxBBWidth=400 PUNTOS FIJOS: como la tolerancia se deriva de
    price * 0.20% * adxFactor, a precio de oro actual el ancho minimo posible en
    la rama de reversion (adx<30 => adxFactor>0.4) supera los 640 puntos, y el
    modo Scalper no disparaba NUNCA. Ver docs/plan-m5-m15-coexistencia.md.
  - Filtro de spread (el original no tenia ninguno).
  - Circuit breaker de volatilidad (VCB), portado del Sentinel.
  - Gobierno de margen (bot/budget.py).

Mejoras v2 (estudio de lateralizacion; el M15 NO se toco)
---------------------------------------------------------
  - Regimen con HISTERESIS: Scalper/Surfer es un estado con banda muerta de
    ADX (entra a Surfer >= m5_trend_adx, vuelve a Scalper < m5_trend_adx_exit),
    no una comparacion seca por vela que alternaba modos con ADX ~30.
  - Estructura de rango M15: el fade del Scalper solo cerca del EXTREMO del
    rango real (fractales M15 confirmados), no en cualquier toque de la banda
    local, que sigue al precio y puede "romperse" en el centro del rango.
  - Veto de ruptura: con breakout M15 confirmado (vela cerrada) no se
    revierte en su contra. Mismo indicador que usa el Sentinel para entrar,
    aqui usado al reves: para abstenerse.
  - Cesion del Surfer: si el M15 ya lleva exposicion neta en esa direccion,
    el Surfer no duplica la apuesta (lee MT5 por magic, igual que el
    gobierno de margen; cero IPC).

Indexacion: igual que bot/indicators.py. MQL5 [0] == .iloc[-1], [1] == .iloc[-2].
"""

import datetime
import math

import MetaTrader5 as mt5
import pandas as pd

from . import budget, config, indicators

# Defaults = valores input del MQL5 original (fallback si falta en bot_config).
DEFAULTS = {
    # Gestion
    "m5_lots_per_1000": 0.02,
    "m5_max_lot_cap": 0.50,
    "m5_min_margin_level": 150.0,
    # Gran Hermano (M15)
    "m5_use_bigbro": True,
    "m5_bigbro_period": 50,
    # Riesgo
    "m5_sl_atr": 2.0,
    "m5_max_trade_min": 45,
    "m5_hard_sl_points": 1000.0,
    # Scalper (reversion)
    "m5_use_reversion": True,
    "m5_ma_period": 50,
    "m5_adx_period": 14,
    "m5_rsi_period": 14,
    "m5_base_tolerance": 0.20,
    "m5_rsi_os": 30,
    "m5_rsi_ob": 70,
    # Banda: 'atr' (recomendado) o 'legacy' (formula original en % de precio)
    "m5_band_mode": "atr",
    "m5_band_atr_mult": 1.0,    # tolerancia = ATR * mult * adxFactor
    "m5_max_band_atr": 2.5,     # ancho maximo de banda permitido, en ATRs
    # Surfer (tendencia)
    "m5_use_surfer": True,
    "m5_trend_adx": 30,
    "m5_surfer_rsi_max": 85,
    "m5_force_rsi_buy": 55,
    "m5_force_rsi_sell": 45,
    # Tiempo
    "m5_use_time_filter": True,
    "m5_start_hour": 1,
    "m5_end_hour": 23,
    # Trailing
    "m5_use_trailing": True,
    "m5_trail_start": 100,
    "m5_trail_dist": 50,
    "m5_trail_step": 10,
    "m5_turbo_trigger": 300,
    "m5_turbo_dist": 150,
    # Añadidos
    "m5_max_spread": 350,
    "m5_use_vcb": True,
    "m5_vcb_atr_mult": 2.8,
    # v2: regimen + estructura de rango (estudio de lateralizacion)
    "m5_trend_adx_exit": 24,      # histeresis: Surfer entra >= m5_trend_adx, vuelve a Scalper < este
    "m5_use_range_struct": True,  # fade solo cerca del extremo del rango M15 (fractales)
    "m5_range_edge_frac": 0.35,   # fraccion del alto del rango que cuenta como 'extremo'
    "m5_use_breakout_veto": True, # no revertir contra una ruptura M15 confirmada
    "m5_surfer_yield": True,      # el Surfer cede si el M15 ya esta expuesto en esa direccion
    # Gobierno de margen (rol JUNIOR)
    "equity_weight": 0.20,
    "margin_cap_pct": 10.0,
    "peer_reserve_mult": 1.5,
    "ml_no_add": 400.0,
    "ml_flatten": 250.0,
}


class M5Engine:
    """Motor del scalper M5. Misma forma que SentinelEngine: on_tick() por tick."""

    def __init__(self, broker, logger):
        self.b = broker
        self.log = logger
        self.cfg = {}
        self.hud = {}

        # Rol JUNIOR: reserva margen para las protectoras del M15 y se retira
        # primero cuando la cuenta aprieta.
        self.gov = budget.MarginGovernor(
            broker, lambda: self.cfg, budget.ROLE_JUNIOR,
            peer_magics=config.PEER_MAGICS, logger=logger)

        # Estado
        self.last_candle_time = 0     # lastCandleTime del MQL5: 1 entrada por vela
        self.last_op_was_win = False  # CheckLastTradeResult()
        self.close_only = False       # espejo de bot_instances.is_active
        self.is_active = False
        self.user_email = None
        self.initial_balance = None
        self.vol_breaker = False
        self.vol_breaker_prev = False
        self.spread_high = False

        # Buffers del tick (vela CERRADA salvo donde se indique)
        self.now = 0
        self.bar_time = 0
        self.ema1 = self.adx1 = self.rsi1 = self.atr1 = 0.0
        self.close1 = self.high1 = self.low1 = 0.0
        self.bigbro_bias = 0
        self.df_m5 = None

        # v2: regimen con histeresis + contexto de rango M15
        self.regime = None       # 'scalper' | 'surfer' (estado, no comparacion por vela)
        self.range_high = 0.0    # techo del rango M15 (ultimo fractal sup. confirmado)
        self.range_low = 0.0     # piso del rango M15 (ultimo fractal inf. confirmado)
        self.brk15 = 0           # ruptura M15 confirmada (+1/-1/0) para el veto del fade

    # ==============================================================
    # Parametros
    # ==============================================================
    def _p(self, key):
        return self.cfg.get(key, DEFAULTS[key])

    def init_history_cursor(self):
        """Compat con main.setup(). El M5 no lleva cursor de Healer."""
        self._refresh_last_trade_result()

    # ==============================================================
    # UTILS (equivalentes del MQL5)
    # ==============================================================
    def _my_positions(self):
        """Posiciones vivas de ESTE bot (Broker ya filtra por su magic)."""
        return self.b.positions()

    def _calculate_lots(self):
        """CalculateDynamicLots(): lote por balance, redondeado al step y topado.

        Se dimensiona sobre el equity PONDERADO por el peso de este bot, no
        sobre el balance completo: con dos motores en la cuenta, escalar ambos
        contra el mismo capital seria doble conteo. Ver bot/budget.py.
        """
        capital = self.gov.sizing_equity()
        lots = (capital / 1000.0) * float(self._p("m5_lots_per_1000"))

        step = self.b.volume_step()
        lots = math.floor(lots / step) * step

        vol_min = self.b.volume_min()
        vol_max = self.b.volume_max()
        if lots < vol_min:
            lots = vol_min
        if lots > vol_max:
            lots = vol_max
        cap = float(self._p("m5_max_lot_cap"))
        if lots > cap:
            lots = cap
        return lots

    def _dynamic_tolerance(self, adx_value, current_price, atr_value):
        """CalculateDynamicTolerance(): semi-ancho de la banda alrededor de la EMA.

        El factor ADX es identico al original: cuanto mas fuerte la tendencia,
        mas estrecha la banda (menos reversion). Lo que cambia es la BASE.

        - 'atr'    : base = ATR * m5_band_atr_mult. Escala con la volatilidad
                     real, que es lo que la banda pretende medir. Recomendado.
        - 'legacy' : base = precio * (m5_base_tolerance / 100). Formula literal
                     del EA; se conserva para poder comparar con el original.
        """
        adx_factor = (50.0 - adx_value) / 50.0
        if adx_factor < 0.2:
            adx_factor = 0.2

        if str(self._p("m5_band_mode")).lower() == "legacy":
            base = current_price * (float(self._p("m5_base_tolerance")) / 100.0)
        else:
            base = atr_value * float(self._p("m5_band_atr_mult"))
        return base * adx_factor

    def _refresh_last_trade_result(self):
        """CheckLastTradeResult(): ¿el ultimo cierre de este bot fue ganador?

        Alimenta el Surfer: tras una ganadora se permite entrar sin exigir el
        umbral de RSI de confirmacion.
        """
        to_dt = datetime.datetime.utcnow() + datetime.timedelta(minutes=5)
        from_dt = to_dt - datetime.timedelta(days=7)
        try:
            deals = self.b.history_deals(from_dt, to_dt)
        except Exception:  # noqa: BLE001  (no tumbar el tick por el historial)
            return
        if not deals:
            return
        best = None
        for d in deals:
            if d.magic != self.b.magic or d.entry != mt5.DEAL_ENTRY_OUT:
                continue
            if best is None or d.time > best.time:
                best = d
        if best is not None:
            self.last_op_was_win = best.profit > 0

    def _big_brother_bias(self, df15=None):
        """GetBigBrotherBias(): 1 alcista / -1 bajista / 0 neutro, sobre EMA M15.

        Veta operar contra la tendencia mayor: no comprar mientras la EMA M15
        cae de forma consistente, no vender mientras sube. Es la mejora estrella
        del v13.02 sobre el v12.30 y la que el grinder del Sentinel no tenia.
        """
        if not self._p("m5_use_bigbro"):
            return 0
        if df15 is None:
            df15 = self._rates(config.TIMEFRAME_CORE)
        if df15 is None:
            return 0
        ema15 = indicators.ema(df15["close"], int(self._p("m5_bigbro_period")))
        if len(ema15) < 3:
            return 0
        e0, e1, e2 = float(ema15.iloc[-1]), float(ema15.iloc[-2]), float(ema15.iloc[-3])
        if e0 > e1 > e2:
            return 1
        if e0 < e1 < e2:
            return -1
        return 0

    def _update_regime(self):
        """Regimen Scalper/Surfer conmutado por ADX, con HISTERESIS.

        El original comparaba adx contra un umbral seco en cada vela: con el
        ADX oscilando alrededor de m5_trend_adx el bot alternaba de modo vela
        a vela (podia vender reversion en una y comprar tendencia en la
        siguiente). Ahora el regimen es un ESTADO: se entra a Surfer al
        superar m5_trend_adx y solo se vuelve a Scalper al caer por debajo de
        m5_trend_adx_exit (banda muerta). Igualar ambos umbrales en bot_config
        recupera el comportamiento clasico.
        """
        trend = float(self._p("m5_trend_adx"))
        exit_th = min(float(self._p("m5_trend_adx_exit")), trend)
        prev = self.regime
        if self.regime is None:
            self.regime = "surfer" if self.adx1 >= trend else "scalper"
        elif self.regime == "scalper" and self.adx1 >= trend:
            self.regime = "surfer"
        elif self.regime == "surfer" and self.adx1 < exit_th:
            self.regime = "scalper"
        if prev is not None and self.regime != prev:
            self.log.write("GRINDER",
                           f"Cambio de regimen: {prev} -> {self.regime} (ADX {self.adx1:.1f}).",
                           balance=self.b.account_balance())

    def _fade_allowed(self, is_buy):
        """Gates ESTRUCTURALES del fade del Scalper, sobre el rango M15.

        1. Veto de ruptura: con una ruptura de fractal M15 CONFIRMADA (vela
           cerrada) no se revierte en su contra. La banda EMA+-tolerancia no
           distingue "extremo del rango" de "arranque de tendencia"; el
           breakout M15 si.
        2. Estructura de rango: el fade solo cerca del EXTREMO del rango real
           (fractales M15), no en cualquier toque de la banda local. La banda
           sigue al precio: en un rango amplio puede romperse en el CENTRO
           del rango, el peor lugar para revertir.

        Sin fractales confirmados (tendencia limpia, warmup) se degrada al
        comportamiento clasico: deciden banda + RSI + Gran Hermano.
        """
        if self._p("m5_use_breakout_veto"):
            if is_buy and self.brk15 == -1:
                return False
            if not is_buy and self.brk15 == 1:
                return False
        if not self._p("m5_use_range_struct"):
            return True
        hi, lo = self.range_high, self.range_low
        if not (hi > lo > 0.0):
            return True  # sin rango M15 utilizable: fallback al filtro clasico
        edge = (hi - lo) * float(self._p("m5_range_edge_frac"))
        if is_buy:
            return self.close1 <= lo + edge
        return self.close1 >= hi - edge

    def _peer_net(self):
        """Exposicion neta (lotes) de los bots vecinos (el Sentinel M15).

        Se lee directo de MT5 por magic, el mismo canal sin-DB que usa el
        gobierno de margen: latencia cero y sobrevive si un proceso muere.
        """
        net = 0.0
        for magic in self.gov.peer_magics:
            if magic == self.b.magic:
                continue
            for p in self.b.positions_of_magic(magic):
                net += p.volume if p.type == mt5.POSITION_TYPE_BUY else -p.volume
        return net

    def _vol_breaker_active(self):
        """VCB: vela M5 en curso anomala (rango >= ATR * mult). Bloquea aperturas."""
        if not self._p("m5_use_vcb") or self.df_m5 is None:
            return False
        bar_range = float(self.df_m5["high"].iloc[-1]) - float(self.df_m5["low"].iloc[-1])
        if self.atr1 <= 0:
            return False
        return bar_range >= self.atr1 * float(self._p("m5_vcb_atr_mult"))

    # ==============================================================
    # GESTION DE SALIDAS (Time Stop + Trailing 2 velocidades)
    # ==============================================================
    def _manage_exits(self):
        time_stop = int(self._p("m5_max_trade_min")) * 60
        point = self.b.point()
        digits = self.b.digits()

        use_trail = bool(self._p("m5_use_trailing"))
        trail_start = int(self._p("m5_trail_start"))
        trail_dist = int(self._p("m5_trail_dist"))
        trail_step = int(self._p("m5_trail_step"))
        turbo_trig = int(self._p("m5_turbo_trigger"))
        turbo_dist = int(self._p("m5_turbo_dist"))
        stops_level = self.b.stops_level()

        for p in self._my_positions():
            # 1. TIME STOP: solo cierra si esta PERDIENDO tras X minutos. Si va
            #    ganando se deja correr (lo gestiona el trailing).
            money = p.profit + p.swap
            if self.now - p.time > time_stop and money < 0:
                tkt = p.ticket
                self.b.close_position(p, "M5 TimeStop")
                self.log.write("GRINDER", f"TimeStop: cierre tactico por estancamiento. PnL: {money:.2f}",
                               p.price_current, p.volume, self.b.account_balance(), ticket=tkt)
                continue

            # 2. TRAILING de dos velocidades.
            if not use_trail:
                continue
            if p.type == mt5.POSITION_TYPE_BUY:
                points = (p.price_current - p.price_open) / point
            else:
                points = (p.price_open - p.price_current) / point
            if points < trail_start:
                continue

            active_dist = turbo_dist if points >= turbo_trig else trail_dist
            # El broker rechaza un SL demasiado pegado al precio: se respeta su
            # SYMBOL_TRADE_STOPS_LEVEL con un colchon, como en el EA original.
            min_safe = max(float(active_dist), float(stops_level) + 20.0)

            update = False
            if p.type == mt5.POSITION_TYPE_BUY:
                new_sl = round(p.price_current - min_safe * point, digits)
                # trail_step = histeresis: sin ella se manda un modify por cada
                # tick que mueva el SL un centimo (spam de ordenes al broker).
                if new_sl > p.price_open and (p.sl == 0 or new_sl > p.sl + trail_step * point):
                    update = True
            else:
                new_sl = round(p.price_current + min_safe * point, digits)
                if new_sl < p.price_open and (p.sl == 0 or new_sl < p.sl - trail_step * point):
                    update = True
            if update:
                self.b.modify_sl(p, new_sl)

    def _flatten(self):
        """Cierra todo lo propio: el M5 se retira cuando la cuenta aprieta."""
        for p in self._my_positions():
            tkt = p.ticket
            money = p.profit + p.swap
            self.b.close_position(p, "M5 Flatten (margen)")
            self.log.write("BUDGET", f"Flatten por nivel de margen. PnL: {money:.2f}",
                           p.price_current, p.volume, self.b.account_balance(), ticket=tkt)

    # ==============================================================
    # BUFFERS
    # ==============================================================
    def _rates(self, timeframe, count=200, min_bars=60):
        rates = mt5.copy_rates_from_pos(config.SYMBOL, timeframe, 0, count)
        if rates is None or len(rates) < min_bars:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df

    def _fetch_tick(self):
        """Seam de datos (igual que SentinelEngine): el backtest lo sobrescribe."""
        return mt5.symbol_info_tick(config.SYMBOL)

    def _compute_buffers(self):
        tick = self._fetch_tick()
        if tick is None:
            return False
        self.now = tick.time

        df5 = self._rates(config.TIMEFRAME_M5)
        if df5 is None:
            return False
        self.df_m5 = df5

        # VELA CERRADA (.iloc[-2] == indice [1] del MQL5). Es la diferencia
        # clave con el grinder del Sentinel, que decidia sobre la vela viva y
        # por tanto repintaba dentro de la barra.
        self.ema1 = float(indicators.ema(df5["close"], int(self._p("m5_ma_period"))).iloc[-2])
        self.adx1 = float(indicators.adx(df5, int(self._p("m5_adx_period"))).iloc[-2])
        self.rsi1 = float(indicators.rsi(df5, int(self._p("m5_rsi_period"))).iloc[-2])
        self.atr1 = float(indicators.atr(df5, 14).iloc[-2])
        self.close1 = float(df5["close"].iloc[-2])
        self.high1 = float(df5["high"].iloc[-2])
        self.low1 = float(df5["low"].iloc[-2])
        self.bar_time = int(df5["time"].iloc[-1].timestamp())

        if any(pd.isna(v) for v in (self.ema1, self.adx1, self.rsi1, self.atr1)):
            return False
        return True

    def _server_dt(self):
        return datetime.datetime.utcfromtimestamp(self.now)

    # ==============================================================
    # HUD (snapshot read-only para la TUI; NO afecta el trading)
    # ==============================================================
    def _build_hud(self, upper, lower, band_ok, in_hours):
        point = self.b.point()
        spread = self.b.spread()
        lots = self._calculate_lots()
        my_pos = len(self._my_positions())
        adx = self.adx1
        trend_adx = int(self._p("m5_trend_adx"))
        exit_adx = min(int(self._p("m5_trend_adx_exit")), trend_adx)
        surfing = self.regime == "surfer"
        mode = "Surfer (tendencia)" if surfing else "Scalper (reversion)"

        if not surfing:
            side = "BUY" if self.rsi1 < int(self._p("m5_rsi_os")) else (
                "SELL" if self.rsi1 > int(self._p("m5_rsi_ob")) else "-")
        else:
            side = "BUY" if self.close1 > self.ema1 else "SELL"

        gates = []
        max_sp = int(self._p("m5_max_spread"))
        gates.append(("Spread", str(spread), f"<= {max_sp}", spread <= max_sp))
        gates.append(("Modo (ADX)", f"{adx:.1f} -> {mode}",
                      f"Surfer >= {trend_adx} / Scalper < {exit_adx}", True))
        gates.append(("Posiciones", str(my_pos), "0", my_pos == 0))

        bias_txt = {1: "Alcista (+1)", -1: "Bajista (-1)"}.get(self.bigbro_bias, "Neutro (0)")
        if side == "BUY":
            gates.append(("Gran Hermano M15", bias_txt, "!= bajista", self.bigbro_bias != -1))
        elif side == "SELL":
            gates.append(("Gran Hermano M15", bias_txt, "!= alcista", self.bigbro_bias != 1))
        else:
            gates.append(("Gran Hermano M15", bias_txt, "-", True))

        if not surfing:
            width_atr = ((upper - lower) / self.atr1) if self.atr1 else 0.0
            gates.append(("Ancho de banda", f"{width_atr:.2f} ATR",
                          f"<= {float(self._p('m5_max_band_atr')):.2f} ATR", band_ok))
            gates.append(("Ruptura de banda",
                          f"low {self.low1:.2f} / high {self.high1:.2f}",
                          f"< {lower:.2f} o > {upper:.2f}",
                          self.low1 < lower or self.high1 > upper))
            gates.append(("RSI (M5)", f"{self.rsi1:.1f}",
                          f"< {int(self._p('m5_rsi_os'))} o > {int(self._p('m5_rsi_ob'))}",
                          self.rsi1 < int(self._p("m5_rsi_os")) or self.rsi1 > int(self._p("m5_rsi_ob"))))
            if self._p("m5_use_range_struct"):
                hi, lo = self.range_high, self.range_low
                if hi > lo > 0:
                    edge = (hi - lo) * float(self._p("m5_range_edge_frac"))
                    if side == "BUY":
                        ok_rng = self.close1 <= lo + edge
                        req = f"cerca del piso (<= {lo + edge:.2f})"
                    elif side == "SELL":
                        ok_rng = self.close1 >= hi - edge
                        req = f"cerca del techo (>= {hi - edge:.2f})"
                    else:
                        ok_rng, req = True, "en extremo"
                    gates.append(("Rango M15", f"{lo:.2f} .. {hi:.2f}", req, ok_rng))
                else:
                    gates.append(("Rango M15", "sin fractales", "fallback banda", True))
            if self._p("m5_use_breakout_veto"):
                brk_txt = {1: "alza (+1)", -1: "baja (-1)"}.get(self.brk15, "no (0)")
                veto = ((side == "BUY" and self.brk15 == -1)
                        or (side == "SELL" and self.brk15 == 1))
                gates.append(("Ruptura M15", brk_txt, "sin ruptura en contra", not veto))
        else:
            rmax = int(self._p("m5_surfer_rsi_max"))
            gates.append(("Precio vs EMA", f"{self.close1:.2f} vs {self.ema1:.2f}",
                          "encima o debajo", True))
            gates.append(("RSI (M5)", f"{self.rsi1:.1f}", f"{100 - rmax} .. {rmax}",
                          (100 - rmax) < self.rsi1 < rmax))
            gates.append(("Racha / RSI forzado",
                          "ganadora" if self.last_op_was_win else f"{self.rsi1:.1f}",
                          "ultima ganadora o RSI de confirmacion", True))
            if self._p("m5_surfer_yield"):
                net = self._peer_net()
                yield_block = ((side == "BUY" and net > 1e-9)
                               or (side == "SELL" and net < -1e-9))
                gates.append(("Cede al M15", f"net vecino {net:+.2f}",
                              "sin exposicion M15 del mismo lado", not yield_block))

        gates.append(("Horario", self._server_dt().strftime("%H:%M"),
                      f"{int(self._p('m5_start_hour'))}h - {int(self._p('m5_end_hour'))}h", in_hours))
        ml = self.b.margin_level()
        min_ml = float(self._p("m5_min_margin_level"))
        gates.append(("Nivel de margen", "inf" if ml == float("inf") else f"{ml:.0f}%",
                      f">= {min_ml:.0f}%", ml >= min_ml))
        budget_ok = self.gov.probe(lots, mt5.ORDER_TYPE_BUY, budget.KIND_ADDITIVE)
        gates.append(("Presupuesto", self.gov.last_block or f"{self.b.margin_free():.0f} libre",
                      f"req {lots:.2f} lot", budget_ok))
        if self.vol_breaker:
            gates.append(("VCB", "ACTIVADO", "liberado", False))

        self.hud = {
            "status": None,
            "symbol": config.SYMBOL,
            "bid": self.b.bid(), "ask": self.b.ask(), "spread": spread,
            "rsi": self.rsi1, "ema": self.ema1, "atr": self.atr1,
            "adx": adx, "mode": mode,
            "struct_h4": self.bigbro_bias, "signal_m15": 0,
            "balance": self.b.account_balance(),
            "equity": self.b.account_equity(),
            "positions": my_pos,
            "side": side,
            "lots": lots,
            "gates": gates,
            "ready": my_pos == 0 and side in ("BUY", "SELL") and all(g[3] for g in gates),
            "blockers": sum(1 for g in gates if not g[3]),
            "server_time": self._server_dt().strftime("%H:%M:%S"),
            "point": point,
        }

    # ==============================================================
    # ON TICK
    # ==============================================================
    def on_tick(self):
        if not self._compute_buffers():
            self.hud = {"status": "Esperando datos de MT5 (warmup de indicadores)..."}
            return

        # 1. Gestion de lo abierto SIEMPRE primero, y nunca condicionada por
        #    spread, VCB ni presupuesto: cerrar y proteger no añade riesgo.
        self._manage_exits()

        # 2. Retirada por margen: el M5 es el que cede. Cierra lo suyo y sale.
        if self.gov.should_flatten():
            if self._my_positions():
                self._flatten()
            self.hud = {"status": "Retirado por nivel de margen (FLATTEN)."}
            return

        # Contexto M15 (una sola lectura por tick): Gran Hermano + estructura
        # de rango por fractales + ruptura confirmada para el veto del fade.
        df15 = self._rates(config.TIMEFRAME_CORE)
        self.bigbro_bias = self._big_brother_bias(df15)
        if df15 is not None:
            self.range_high, self.range_low = indicators.fractal_range(df15)
            self.brk15 = indicators.check_m15_breakout(df15)
        else:
            self.range_high = self.range_low = 0.0
            self.brk15 = 0

        self._update_regime()
        self.spread_high = self.b.spread() > int(self._p("m5_max_spread"))

        self.vol_breaker = self._vol_breaker_active()
        if self.vol_breaker != self.vol_breaker_prev:
            estado = "ACTIVADO" if self.vol_breaker else "liberado"
            self.log.write("VCB", f"Circuit breaker de volatilidad {estado}.",
                           balance=self.b.account_balance())
            self.vol_breaker_prev = self.vol_breaker

        # --- Bandas y regimen (sobre la vela cerrada) ---
        tolerance = self._dynamic_tolerance(self.adx1, self.close1, self.atr1)
        upper = self.ema1 + tolerance
        lower = self.ema1 - tolerance
        band_width = upper - lower
        max_band = self.atr1 * float(self._p("m5_max_band_atr"))
        band_ok = band_width <= max_band if self.atr1 > 0 else False

        dt = self._server_dt()
        in_hours = True
        if self._p("m5_use_time_filter"):
            in_hours = int(self._p("m5_start_hour")) <= dt.hour <= int(self._p("m5_end_hour"))

        self._build_hud(upper, lower, band_ok, in_hours)

        # --- Gates de apertura ---
        if self.close_only or self.spread_high or self.vol_breaker:
            return
        if not in_hours:
            return
        if self.b.margin_level() < float(self._p("m5_min_margin_level")):
            return
        if self.bar_time == self.last_candle_time:
            return  # ya se opero en esta vela
        if self._my_positions():
            return  # una sola posicion viva a la vez

        self._refresh_last_trade_result()

        signal_buy = False
        signal_sell = False
        comment = ""

        # =========================================================
        # 1. SCALPER (reversion) - regimen de rango (ADX con histeresis)
        # =========================================================
        if self._p("m5_use_reversion") and self.regime == "scalper":
            # Bandas demasiado abiertas = mercado explotando. Revertir ahi es
            # atrapar el cuchillo: se abstiene.
            if band_ok:
                if self.low1 < lower and self.rsi1 < int(self._p("m5_rsi_os")):
                    # No comprar si el M15 cae con fuerza, ni lejos del piso
                    # del rango M15, ni contra una ruptura bajista confirmada.
                    if self.bigbro_bias != -1 and self._fade_allowed(True):
                        signal_buy = True
                        comment = "M5 Reversion Buy"
                elif self.high1 > upper and self.rsi1 > int(self._p("m5_rsi_ob")):
                    # No vender si el M15 sube con fuerza, ni lejos del techo
                    # del rango M15, ni contra una ruptura alcista confirmada.
                    if self.bigbro_bias != 1 and self._fade_allowed(False):
                        signal_sell = True
                        comment = "M5 Reversion Sell"

        # =========================================================
        # 2. SURFER (tendencia) - regimen de tendencia (ADX con histeresis)
        # =========================================================
        elif self._p("m5_use_surfer") and self.regime == "surfer":
            rsi_max = int(self._p("m5_surfer_rsi_max"))
            if self.close1 > self.ema1 and self.rsi1 < rsi_max:
                if self.last_op_was_win or self.rsi1 > int(self._p("m5_force_rsi_buy")):
                    if self.bigbro_bias != -1:
                        signal_buy = True
                        comment = "M5 Surfer Buy"
            elif self.close1 < self.ema1 and self.rsi1 > (100 - rsi_max):
                if self.last_op_was_win or self.rsi1 < int(self._p("m5_force_rsi_sell")):
                    if self.bigbro_bias != 1:
                        signal_sell = True
                        comment = "M5 Surfer Sell"

            # Cesion al senior: en tendencia, el Sentinel M15 ya es el
            # especialista (ruptura + ciclo). Si el vecino lleva exposicion
            # neta del MISMO lado, este scalp solo duplicaria la apuesta
            # direccional de la cuenta; se cede el turno y el margen.
            if (signal_buy or signal_sell) and self._p("m5_surfer_yield"):
                net = self._peer_net()
                if (signal_buy and net > 1e-9) or (signal_sell and net < -1e-9):
                    signal_buy = signal_sell = False
                    comment = ""

        if not (signal_buy or signal_sell):
            return

        # --- EJECUCION con SL dinamico por ATR ---
        lots = self._calculate_lots()
        order_type = mt5.ORDER_TYPE_BUY if signal_buy else mt5.ORDER_TYPE_SELL
        if not self.gov.can_open(lots, order_type, budget.KIND_ADDITIVE):
            return

        point = self.b.point()
        digits = self.b.digits()
        sl_dist = self.atr1 * float(self._p("m5_sl_atr"))
        hard_floor = float(self._p("m5_hard_sl_points")) * point
        if sl_dist < hard_floor:
            sl_dist = hard_floor

        if signal_buy:
            sl = round(self.b.ask() - sl_dist, digits)
        else:
            sl = round(self.b.bid() + sl_dist, digits)

        res = self.b.market_order(order_type, lots, comment, sl=sl)
        ok = self.b.shadow or (
            res is not None and getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE)
        if ok:
            # Solo se marca la vela como usada si la orden ENTRO: un rechazo no
            # debe consumir el turno de la vela (mismo criterio que el EA).
            self.last_candle_time = self.bar_time
            self.log.write("GRINDER",
                           f"Entrada M5 | {comment} | ADX {self.adx1:.1f} RSI {self.rsi1:.1f} "
                           f"| GranHermano {self.bigbro_bias} | SL {sl:.2f}",
                           self.b.ask() if signal_buy else self.b.bid(), lots,
                           self.b.account_balance())
