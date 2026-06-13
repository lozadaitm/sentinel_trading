"""SentinelEngine: port 1:1 del EA MQL5 'M15 Gold Sentinel HyperGrinder v20.0'.

Maquina de estados por numero de posiciones (0/1/2/3+) + subsistemas
transversales (Healer, Profit Banking, Grinder trailing). on_tick() replica
OnTick del MQL5. Los parametros se reciben en self.cfg (cargados de bot_config).

Indexacion: ver bot/indicators.py. MQL5 [0]==.iloc[-1], [1]==.iloc[-2].
Tiempos: se usa el epoch del servidor (tick.time) para todas las comparaciones
de tiempo, consistente con position.time.
"""

import datetime

import MetaTrader5 as mt5
import pandas as pd

from . import config, indicators

# Defaults = valores input del MQL5 (fallback si falta la columna en bot_config)
DEFAULTS = {
    "inp_max_spread": 350,
    "base_risk": 5000.0, "base_lots": 0.12, "max_entry_lots": 1.0, "max_recovery_lots": 2.0,
    "use_basket_close": True, "basket_percent": 0.15, "commission_per_lot": 6.0,
    "use_dynamic_retrace": True, "retrace_atr_mult": 0.1, "fixed_retrace": 2.0,
    "use_dynamic_hedge": True, "hedge_dist": 350, "hedge_atr_mult": 2.0,
    "use_healer": True, "healer_balance_bias": True, "use_unwind_mode": True,
    "unwind_atr_mult": 2.0, "unwind_money_floor": 30.0,
    "use_grinder": True, "grinder_lots": 0.05, "grinder_time_stop": 45,
    "grinder_adx_trend": 30, "grinder_rsi_ob": 70, "grinder_rsi_os": 30,
    "grinder_use_trail": True, "grinder_trail_start": 50, "grinder_trail_dist": 20,
    "grinder_turbo_trig": 150, "grinder_turbo_dist": 50,
    "use_pullback": True, "atr_entry_distance": 2.5, "use_h4_struct": True,
    "entry_rsi_max": 75, "entry_rsi_min": 25,
    "use_smart_trail": True, "trail_activate": 1.0, "trail_dist_atr": 1.5,
    "use_op3_trail": True, "op3_start_atr": 0.60, "op3_base_dist_atr": 0.40,
    "op3_turbo_trigger_atr": 3.0, "op3_turbo_dist_atr": 1.2,
    "use_m5_confirm": True, "min_tech_wait": 60,
    "use_rescue_mode": True, "rescue_atr_mult": 4.0, "rescue_rsi": 70,
    "rescue_target_pct": 0.10, "dd_percent_l2": 3.0, "dd_percent_l3": 8.0,
    "fast_ma": 9, "atr_period": 14, "close_friday": True, "friday_hour": 20,
    "monday_start_hour": 10, "cooldown_seconds": 10,
    # --- Nuevos (optimizaciones de esta revision) ---
    "recovery_min_spacing_atr": 1.0,  # OP3 anti-espera v2: separacion minima por ATR del ultimo leg
    "rescue_cooldown": 30,            # OP4: segundos minimos entre rescates (anti-spam de L3)
    "grinder_cooldown": 120,          # OP11: segundos minimos entre aperturas de grinder (anti-churn)
    "grinder_min_atr_points": 80,     # OP11: ATR minimo (pts) para permitir scalpeo
}

_LOT_EPS = 1e-8


def _struct_txt(v):
    return {1: "Alcista (+1)", -1: "Bajista (-1)"}.get(v, "Neutral (0)")


def _sig_txt(v):
    return {1: "Ruptura alza (+1)", -1: "Ruptura baja (-1)"}.get(v, "Sin ruptura (0)")


class SentinelEngine:
    def __init__(self, broker, logger):
        self.b = broker
        self.log = logger
        self.cfg = {}
        self.hud = {}  # snapshot read-only publicado cada tick para la TUI

        # Estado (globals del MQL5)
        self.last_recovery_close_time = 0
        self.last_healed_ticket = 0
        self.max_cycle_peak = 0.0
        self.cycle_armed = False      # trailing de cesta armado (OP8/OP9)
        self.spread_high = False      # cache del filtro de spread del tick actual
        self.last_rescue_time = 0     # cooldown de rescate (OP4)
        self.last_grinder_open = 0    # cooldown de apertura de grinder (OP11)

        # Buffers del tick actual (rellenados por _compute_buffers)
        self.now = 0
        self.atr0 = self.atr1 = 0.0
        self.ema0 = self.rsi0 = 0.0
        self.g_ema0 = self.g_adx0 = self.g_rsi0 = 0.0
        self.m5_close0 = self.m5_open1 = self.m5_close1 = 0.0
        self.df_h4 = None
        self.df_m15 = None

    # ==============================================================
    # Helpers de parametros
    # ==============================================================
    def _p(self, key):
        return self.cfg.get(key, DEFAULTS[key])

    def init_history_cursor(self):
        """Init lastHealedTicket al mayor ticket existente (baseline del healer).

        El cursor es por numero de ticket (independiente de zona horaria), por lo
        que basta una ventana amplia. Se usa max(ticket) en vez de deals[-1] para
        no depender del orden de retorno del broker.
        """
        to_dt = datetime.datetime.now() + datetime.timedelta(minutes=5)
        from_dt = to_dt - datetime.timedelta(days=7)
        deals = self.b.history_deals(from_dt, to_dt)
        if deals:
            self.last_healed_ticket = max(d.ticket for d in deals)

    # ==============================================================
    # Utils numericas (UTILS MQL5)
    # ==============================================================
    def _dynamic_money(self, percent):
        return (self.b.account_balance() * percent) / 100.0

    def _effective_atr(self):
        return max(self.atr0, self.b.point() * 150)

    def _is_grinder(self, p):
        """RF-E: identifica al grinder por COMMENT (robusto), con fallback a lote.

        El lote exacto chocaba con cualquier posicion que casualmente valiera
        grinder_lots. El comment de apertura ("Grinder ...") es fiable; si el
        broker lo recorta, cae al criterio de lote como respaldo.
        """
        comment = getattr(p, "comment", "") or ""
        if "Grinder" in comment:
            return True
        return abs(p.volume - float(self._p("grinder_lots"))) < _LOT_EPS

    # ==============================================================
    # Lotaje y cierre total
    # ==============================================================
    def _calculate_lots(self, is_recovery):
        equity = self.b.account_equity()
        base_risk = float(self._p("base_risk"))
        ratio = equity / base_risk if base_risk else 0.0
        lots = ratio * float(self._p("base_lots"))

        vol_step = self.b.volume_step()
        import math
        lots = math.floor(lots / vol_step) * vol_step
        min_lot = self.b.volume_min()
        max_lot = self.b.volume_max()

        if lots < min_lot:
            lots = min_lot
        if lots > max_lot:
            lots = max_lot

        cap = float(self._p("max_recovery_lots")) if is_recovery else float(self._p("max_entry_lots"))
        if lots > cap:
            lots = cap
        return lots

    def _close_all(self, reason):
        total_profit = 0.0
        for p in self.b.positions():
            total_profit += p.profit + p.swap
            self.b.close_position(p, f"Close: {reason}")
        self.log.write("EXITO", f"Cierre CICLO ({reason}). PnL: {total_profit:.2f}",
                       balance=self.b.account_balance())
        self.last_recovery_close_time = self.now
        self.max_cycle_peak = 0.0
        self.cycle_armed = False

    # ==============================================================
    # Filtros de entrada
    # ==============================================================
    def _is_price_good_entry(self, order_type):
        if not self._p("use_pullback"):
            return True
        price = self.b.ask() if order_type == mt5.ORDER_TYPE_BUY else self.b.bid()
        ema = self.ema0
        current_atr = self._effective_atr()
        if abs(price - ema) > (current_atr * float(self._p("atr_entry_distance"))):
            return False
        return True

    def _m5_momentum(self, order_type):
        if not self._p("use_m5_confirm"):
            return True
        if order_type == mt5.ORDER_TYPE_BUY:
            return self.m5_close1 > self.m5_open1
        if order_type == mt5.ORDER_TYPE_SELL:
            return self.m5_close1 < self.m5_open1
        return True

    # ==============================================================
    # HUD (snapshot read-only para la TUI; NO afecta el trading)
    # ==============================================================
    def _build_hud(self, struct_h4, signal_m15, is_friday_mode, is_weekly_start_wait):
        """Construye el panel 'actual vs requerido' de las condiciones de Op1.

        Espejo de lectura de los gates del ESTADO 0. Si la maquina de estados
        cambia, este metodo debe seguirla (no comparte codigo a proposito,
        para no arriesgar la logica de trading).
        """
        point = self.b.point()
        bid = self.b.bid()
        ask = self.b.ask()
        spread = self.b.spread()
        rsi = self.rsi0
        ema = self.ema0
        catr = self._effective_atr()
        my_positions = len(self.b.positions())

        if signal_m15 == 1:
            side = "BUY"
        elif signal_m15 == -1:
            side = "SELL"
        elif struct_h4 > 0:
            side = "BUY?"
        elif struct_h4 < 0:
            side = "SELL?"
        else:
            side = "-"

        gates = []
        max_sp = int(self._p("inp_max_spread"))
        gates.append(("Spread", str(spread), f"<= {max_sp}", spread <= max_sp))

        if side.startswith("BUY"):
            gates.append(("Estructura H4", _struct_txt(struct_h4), ">= 0 (alcista)", struct_h4 >= 0))
            gates.append(("Senal M15", _sig_txt(signal_m15), "ruptura alza (+1)", signal_m15 == 1))
            dist = abs(ask - ema)
            maxd = catr * float(self._p("atr_entry_distance"))
            ok_pb = (not self._p("use_pullback")) or dist <= maxd
            gates.append(("Pullback EMA", f"{dist / point:.0f} pt", f"<= {maxd / point:.0f} pt", ok_pb))
            rmax = int(self._p("entry_rsi_max"))
            gates.append(("RSI (M15)", f"{rsi:.1f}", f"< {rmax}", rsi < rmax))
            otype = mt5.ORDER_TYPE_BUY
        elif side.startswith("SELL"):
            gates.append(("Estructura H4", _struct_txt(struct_h4), "<= 0 (bajista)", struct_h4 <= 0))
            gates.append(("Senal M15", _sig_txt(signal_m15), "ruptura baja (-1)", signal_m15 == -1))
            dist = abs(bid - ema)
            maxd = catr * float(self._p("atr_entry_distance"))
            ok_pb = (not self._p("use_pullback")) or dist <= maxd
            gates.append(("Pullback EMA", f"{dist / point:.0f} pt", f"<= {maxd / point:.0f} pt", ok_pb))
            rmin = int(self._p("entry_rsi_min"))
            gates.append(("RSI (M15)", f"{rsi:.1f}", f"> {rmin}", rsi > rmin))
            otype = mt5.ORDER_TYPE_SELL
        else:
            gates.append(("Senal M15", _sig_txt(signal_m15), "ruptura +-1", False))
            gates.append(("Estructura H4", _struct_txt(struct_h4), "definida", struct_h4 != 0))
            gates.append(("RSI (M15)", f"{rsi:.1f}", "25 .. 75", 25 < rsi < 75))
            otype = mt5.ORDER_TYPE_BUY

        lots = self._calculate_lots(False)
        margin_ok = self.b.check_free_margin(lots, otype)
        gates.append(("Margen libre", f"{self.b.margin_free():.0f}", f"req {lots:.2f} lot", margin_ok))

        if is_friday_mode:
            gates.append(("Gate viernes", "ON", "OFF", False))
        if is_weekly_start_wait:
            gates.append(("Apertura semanal", "en espera", "abierto", False))
        if self.last_recovery_close_time > 0:
            elapsed = self.now - self.last_recovery_close_time
            cd = int(self._p("cooldown_seconds"))
            if elapsed < cd:
                gates.append(("Cooldown", f"{cd - elapsed}s", "0s", False))

        ready = (my_positions == 0 and side in ("BUY", "SELL") and all(g[3] for g in gates))
        blockers = sum(1 for g in gates if not g[3])

        self.hud = {
            "status": None,
            "symbol": config.SYMBOL,
            "bid": bid, "ask": ask, "spread": spread,
            "rsi": rsi, "ema": ema, "atr": catr,
            "struct_h4": struct_h4, "signal_m15": signal_m15,
            "balance": self.b.account_balance(),
            "equity": self.b.account_equity(),
            "positions": my_positions,
            "side": side,
            "lots": lots,
            "gates": gates,
            "ready": ready,
            "blockers": blockers,
            "server_time": self._server_dt().strftime("%H:%M:%S"),
        }

    # ==============================================================
    # Calculos de cesta
    # ==============================================================
    def _round_trip_commission(self, volume):
        """RF-F: comision ida+vuelta. commission_per_lot se interpreta por lado."""
        return volume * float(self._p("commission_per_lot")) * 2.0

    def _basket_net_profit(self):
        net = 0.0
        for p in self.b.positions():
            net += p.profit + p.swap
            net -= self._round_trip_commission(p.volume)
        return net

    def _vol_risk_money(self):
        total_lots = sum(p.volume for p in self.b.positions())
        if total_lots <= 0:
            return 1.0
        atr_points = self._effective_atr() / self.b.point()
        return atr_points * total_lots * self.b.tick_value()

    # ==============================================================
    # SMART HEALER
    # ==============================================================
    def _check_healing(self):
        """Healer: ACUMULA el profit de TODOS los deals ganadores nuevos desde el
        cursor (no solo el mas reciente) y aplica una amputacion con ese 90%.

        Cambios vs version previa:
          - RF/bug: ya no se descartan los ganadores intermedios. El cursor avanza
            al mayor ticket visto, pero el presupuesto suma TODOS los ganadores
            nuevos (no salta solo al ultimo).
          - Tiempo: se usa una ventana amplia y se filtra por ticket (el cursor),
            evitando el desfase de zona horaria de datetime.now() vs server time.
          - No se exige que un solo ganador cubra toda la perdida: se va curando
            parcialmente con lo disponible en cada pasada.
        """
        if not self._p("use_healer"):
            return
        to_dt = datetime.datetime.utcfromtimestamp(self.now) + datetime.timedelta(minutes=5)
        from_dt = to_dt - datetime.timedelta(days=2)
        deals = self.b.history_deals(from_dt, to_dt)
        if not deals:
            return

        budget = 0.0
        max_ticket = self.last_healed_ticket
        for d in deals:
            if d.ticket <= self.last_healed_ticket:
                continue
            if d.ticket > max_ticket:
                max_ticket = d.ticket
            if d.magic == self.b.magic and d.entry == mt5.DEAL_ENTRY_OUT and d.profit > 0:
                budget += d.profit  # acumula TODOS los ganadores nuevos

        if max_ticket > self.last_healed_ticket:
            self.last_healed_ticket = max_ticket
        if budget > 0:
            self._apply_healing(budget)

    def _apply_healing(self, profit_available):
        worst = None
        worst_profit = 1e9
        for p in self.b.positions():
            if self._is_grinder(p):
                continue  # no amputar los scalps del grinder
            if p.profit < worst_profit:
                worst_profit = p.profit
                worst = p
        if worst is None or worst_profit >= 0:
            return

        point = self.b.point()
        current_price = self.b.bid() if worst.type == mt5.POSITION_TYPE_BUY else self.b.ask()
        diff_points = abs(current_price - worst.price_open) / point
        if diff_points == 0:
            return

        tick_value = self.b.tick_value()
        min_lot = self.b.volume_min()
        cost_of_min_lot = (diff_points * tick_value) * min_lot
        if cost_of_min_lot > profit_available:
            return  # ni el lote minimo cabe en el presupuesto disponible

        budget = profit_available * 0.90
        lots_to_close = budget / (diff_points * tick_value)
        import math
        vol_step = self.b.volume_step()
        lots_to_close = math.floor(lots_to_close / vol_step) * vol_step
        if lots_to_close > worst.volume:
            lots_to_close = worst.volume  # no cerrar mas de lo abierto

        if lots_to_close >= min_lot:
            res = self.b.close_partial(worst, lots_to_close, "Healer Amputacion")
            if res is None or getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
                self.log.write("HEALER", f"Amputacion Tactica. Lotes: {lots_to_close:.2f}",
                               worst_profit, budget, self.b.account_balance())

    # ==============================================================
    # SMART GRINDER
    # ==============================================================
    def _run_grinder(self):
        if not self._p("use_grinder"):
            return
        grinder_lots = float(self._p("grinder_lots"))

        # A. Time stop / limpieza (corre siempre; limpia perdedores Y break-even)
        grinder_ops = 0
        time_stop = int(self._p("grinder_time_stop")) * 60
        for p in self.b.positions():
            if self._is_grinder(p):
                grinder_ops += 1
                if self.now - p.time > time_stop and p.profit <= 0:
                    self.b.close_position(p, "Grinder TimeStop")
                    self.log.write("GRINDER", "TimeStop Activado. Limpiando zona.", p.profit,
                                   balance=self.b.account_balance())
                    return
        if grinder_ops >= 1:
            return  # Solo 1 Grinder a la vez

        # --- Gates de apertura (OP11) ---
        if self.spread_high:
            return  # no scalpear con spread alto
        if self.now - self.last_grinder_open < int(self._p("grinder_cooldown")):
            return  # anti-churn: respeta cooldown tras el ultimo grinder
        if (self.atr0 / self.b.point()) < int(self._p("grinder_min_atr_points")):
            return  # mercado sin volatilidad: no scalpear
        if not self.b.check_free_margin(grinder_lots, mt5.ORDER_TYPE_BUY):
            return

        # B. Entrada SmartCut M5
        adx = self.g_adx0
        rsi = self.g_rsi0
        close_m5 = self.m5_close0
        ma_m5 = self.g_ema0

        opened = False
        if adx < int(self._p("grinder_adx_trend")):
            # Modo Scalper (reversion) - ADX bajo
            if rsi < int(self._p("grinder_rsi_os")):
                self.b.market_order(mt5.ORDER_TYPE_BUY, grinder_lots, "Grinder Scalp Buy")
                opened = True
            elif rsi > int(self._p("grinder_rsi_ob")):
                self.b.market_order(mt5.ORDER_TYPE_SELL, grinder_lots, "Grinder Scalp Sell")
                opened = True
        else:
            # Modo Surfer (tendencia) - ADX alto
            if close_m5 > ma_m5 and rsi < 70:
                self.b.market_order(mt5.ORDER_TYPE_BUY, grinder_lots, "Grinder Surf Buy")
                opened = True
            elif close_m5 < ma_m5 and rsi > 30:
                self.b.market_order(mt5.ORDER_TYPE_SELL, grinder_lots, "Grinder Surf Sell")
                opened = True
        if opened:
            self.last_grinder_open = self.now

    def _grinder_trailing(self):
        if not self._p("grinder_use_trail"):
            return
        point = self.b.point()
        digits = self.b.digits()
        trail_start = int(self._p("grinder_trail_start"))
        trail_dist = int(self._p("grinder_trail_dist"))
        turbo_trig = int(self._p("grinder_turbo_trig"))
        turbo_dist = int(self._p("grinder_turbo_dist"))

        for p in self.b.positions():
            if not self._is_grinder(p):
                continue
            if p.type == mt5.POSITION_TYPE_BUY:
                points = (p.price_current - p.price_open) / point
            else:
                points = (p.price_open - p.price_current) / point

            active_dist = trail_dist
            if points >= turbo_trig:
                active_dist = turbo_dist

            if points >= trail_start:
                update = False
                if p.type == mt5.POSITION_TYPE_BUY:
                    new_sl = round(p.price_current - active_dist * point, digits)
                    if new_sl > p.price_open and (p.sl == 0 or new_sl > p.sl):
                        update = True
                else:
                    new_sl = round(p.price_current + active_dist * point, digits)
                    if new_sl < p.price_open and (p.sl == 0 or new_sl < p.sl):
                        update = True
                if update:
                    self.b.modify_sl(p, new_sl)

    # ==============================================================
    # PROFIT BANKING (Unwind)
    # ==============================================================
    def _profit_banking(self):
        """Banca una posicion ganadora SOLO si la cesta total ya es neta positiva.

        Cambio clave: mientras el neto de la cesta sea <= 0 las ganadoras estan
        CUBRIENDO a las perdedoras (hedge lock). Desarmar esa cobertura re-exponia
        al perdedor desnudo (causa del -760 observado). Ahora el unwind respeta el
        congelamiento: el hedge solo se cierra cuando el saldo TOTAL es positivo.

        RF-C corregido: la condicion de dinero y la posicion elegida son la MISMA
        (se banca la ganadora mas rica que cumpla el umbral, no se mezclan).
        RF-F: comision ida+vuelta.
        """
        if not self._p("use_unwind_mode"):
            return
        positions = self.b.positions()
        if len(positions) < 2:
            return

        # FREEZE: no desarmar cobertura si la cesta sigue en negativo.
        if self._basket_net_profit() <= 0:
            return

        current_atr = self._effective_atr()
        best_pos = None
        best_money = -1e18
        for p in positions:
            money = p.profit + p.swap - self._round_trip_commission(p.volume)
            if money <= 0:
                continue
            dist = abs(p.price_current - p.price_open) / current_atr if current_atr else 0.0
            qualifies = (dist >= float(self._p("unwind_atr_mult"))
                         or money >= float(self._p("unwind_money_floor")))
            if qualifies and money > best_money:
                best_money = money
                best_pos = p

        if best_pos is not None:
            self.b.close_position(best_pos, "Unwind Profit Banking")
            self.log.write("UNWIND", f"Profit Banking. Money: {best_money:.2f}",
                           balance=self.b.account_balance())

    # ==============================================================
    # SENTINEL OP4 (Rescate)
    # ==============================================================
    def _check_rescue(self):
        if not self._p("use_rescue_mode"):
            return
        if self.spread_high:
            return  # no abrir martingala con spread alto
        # Op3 / cesta = solo posiciones core (RF-I: excluye scalps del grinder)
        positions = [p for p in self.b.positions() if not self._is_grinder(p)]
        if len(positions) < 3:
            return
        if self.now - self.last_rescue_time < int(self._p("rescue_cooldown")):
            return  # anti-spam (sobre todo en L3 "abre ahora")

        current_dd = self._basket_net_profit()
        abs_dd = abs(current_dd)
        level2 = self._dynamic_money(float(self._p("dd_percent_l2")))
        level3 = self._dynamic_money(float(self._p("dd_percent_l3")))

        active_atr_mult = float(self._p("rescue_atr_mult"))
        ignore_rsi = False
        immediate = False
        if abs_dd > level2:
            active_atr_mult = 3.0
            ignore_rsi = True
        if abs_dd > level3:
            active_atr_mult = 2.0
            ignore_rsi = True
            immediate = True  # L3 = ABRE AHORA (sin esperar distancia ni momentum)

        # Op3 = ultima posicion core abierta (por tiempo)
        last_time = 0
        vol_op3 = price_op3 = 0.0
        type_op3 = -1
        for p in positions:
            if p.time > last_time:
                last_time = p.time
                vol_op3 = p.volume
                price_op3 = p.price_open
                type_op3 = p.type

        current_atr = self._effective_atr()
        min_distance = current_atr * active_atr_mult
        rsi = self.rsi0
        rescue_rsi = int(self._p("rescue_rsi"))

        comment = "Sentinel Op 4 (L1)"
        if abs_dd > level2:
            comment = "Sentinel Op 4 (L2)"
        if abs_dd > level3:
            comment = "Sentinel Op 4 (L3-CRITICO)"

        signal = False
        if type_op3 == mt5.POSITION_TYPE_SELL:
            if immediate:
                signal = True  # L3: defensa inmediata
            else:
                dist = self.b.bid() - price_op3
                if dist >= min_distance and (ignore_rsi or rsi > rescue_rsi):
                    signal = True
            if signal and self.b.check_free_margin(vol_op3, mt5.ORDER_TYPE_SELL):
                self.b.market_order(mt5.ORDER_TYPE_SELL, vol_op3, comment)
                self.last_rescue_time = self.now
        elif type_op3 == mt5.POSITION_TYPE_BUY:
            if immediate:
                signal = True  # L3: defensa inmediata
            else:
                dist = price_op3 - self.b.ask()
                if dist >= min_distance and (ignore_rsi or rsi < (100 - rescue_rsi)):
                    signal = True
            if signal and self.b.check_free_margin(vol_op3, mt5.ORDER_TYPE_BUY):
                self.b.market_order(mt5.ORDER_TYPE_BUY, vol_op3, comment)
                self.last_rescue_time = self.now

    # ==============================================================
    # BUFFERS (equivalente a los CopyBuffer de OnTick)
    # ==============================================================
    def _rates(self, timeframe, count=150, min_bars=60):
        rates = mt5.copy_rates_from_pos(config.SYMBOL, timeframe, 0, count)
        if rates is None or len(rates) < min_bars:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df

    def _fetch_tick(self):
        """Seam de datos: en vivo devuelve el tick de MT5; el backtest lo sobrescribe."""
        return mt5.symbol_info_tick(config.SYMBOL)

    def _compute_buffers(self):
        tick = self._fetch_tick()
        if tick is None:
            return False
        self.now = tick.time

        df15 = self._rates(config.TIMEFRAME_CORE)
        df5 = self._rates(config.TIMEFRAME_GRINDER)
        dfh4 = self._rates(config.TIMEFRAME_STRUCT)
        if df15 is None or df5 is None or dfh4 is None:
            return False

        atr_period = int(self._p("atr_period"))
        atr15 = indicators.atr(df15, atr_period)
        self.atr0 = float(atr15.iloc[-1])
        self.atr1 = float(atr15.iloc[-2])
        self.ema0 = float(indicators.ema(df15["close"], int(self._p("fast_ma"))).iloc[-1])
        self.rsi0 = float(indicators.rsi(df15).iloc[-1])

        self.g_ema0 = float(indicators.ema(df5["close"], 50).iloc[-1])
        self.g_adx0 = float(indicators.adx(df5).iloc[-1])
        self.g_rsi0 = float(indicators.rsi(df5).iloc[-1])
        self.m5_close0 = float(df5["close"].iloc[-1])
        self.m5_open1 = float(df5["open"].iloc[-2])
        self.m5_close1 = float(df5["close"].iloc[-2])

        self.df_h4 = dfh4
        self.df_m15 = df15

        # Guarda contra NaN (warmup de indicadores)
        if pd.isna(self.atr0) or pd.isna(self.rsi0) or pd.isna(self.ema0):
            return False
        return True

    def _server_dt(self):
        return datetime.datetime.utcfromtimestamp(self.now)

    # ==============================================================
    # ON TICK (CORE)
    # ==============================================================
    def on_tick(self):
        if not self._compute_buffers():
            self.hud = {"status": "Esperando datos de MT5 (warmup de indicadores)..."}
            return

        struct_h4 = indicators.get_h4_structure(self.df_h4, self._p("use_h4_struct"))
        signal_m15 = indicators.check_m15_breakout(self.df_m15)

        dt = self._server_dt()
        dow = (dt.weekday() + 1) % 7  # MQL5: domingo=0 ... sabado=6
        hour = dt.hour
        is_friday_mode = (self._p("close_friday") and dow == 5 and hour >= int(self._p("friday_hour")))
        is_weekly_start_wait = (dow == 0 or (dow == 1 and hour < int(self._p("monday_start_hour"))))

        # Snapshot read-only para la TUI (se publica aun si el tick sale temprano).
        self._build_hud(struct_h4, signal_m15, is_friday_mode, is_weekly_start_wait)

        # Spread: NO congela la gestion (RF-A). Solo bloquea aperturas que
        # ANADEN riesgo (entrada, recovery, rescate, grinder). Cierres, trailing,
        # healer y profit banking corren igual; el Hedge (proteccion) tambien.
        self.spread_high = self.b.spread() > int(self._p("inp_max_spread"))

        # Subsistemas transversales
        self._check_healing()
        self._profit_banking()
        self._grinder_trailing()

        positions = self.b.positions()
        core_positions = [p for p in positions if not self._is_grinder(p)]
        my_positions = len(positions)
        core_count = len(core_positions)  # RF-I: estado por posiciones core (sin grinder)

        current_atr = self._effective_atr()
        point = self.b.point()
        digits = self.b.digits()

        # ============================================================
        # MONITOR DE SALIDA - trailing de cesta (OP8/OP9 unificados)
        # Se ARMA cuando el neto alcanza el target; luego sigue el pico y cierra
        # al retroceder. Deja correr tendencia favorable y asegura lo ganado.
        # ============================================================
        if my_positions > 0:
            net_pl = self._basket_net_profit()
            arm_target = self._dynamic_money(float(self._p("basket_percent")))
            if arm_target < 2.0:
                arm_target = 2.0
            # Cesta profunda (>=4 core): se arma antes, con el target de rescate.
            if core_count >= 4 and self._p("use_rescue_mode"):
                rescue_target = self._dynamic_money(float(self._p("rescue_target_pct")))
                if rescue_target < 2.0:
                    rescue_target = 2.0
                if rescue_target < arm_target:
                    arm_target = rescue_target

            if not self.cycle_armed and net_pl >= arm_target:
                self.cycle_armed = True
                self.max_cycle_peak = net_pl

            if self.cycle_armed:
                if net_pl > self.max_cycle_peak:
                    self.max_cycle_peak = net_pl
                allowed_retrace = float(self._p("fixed_retrace"))
                if self._p("use_dynamic_retrace"):
                    vol_money = self._vol_risk_money()
                    allowed_retrace = vol_money * float(self._p("retrace_atr_mult"))
                    allowed_retrace = max(1.0, min(allowed_retrace, 50.0))
                # RF-B: el retroceso se evalua SIEMPRE que este armado, aunque
                # net_pl haya caido por debajo del target entre ticks.
                if self.max_cycle_peak - net_pl >= allowed_retrace:
                    reason = "Rescue Mission Success" if core_count >= 4 else "Basket Profit Trail"
                    self._close_all(reason)
                    return
        else:
            self.cycle_armed = False
            self.max_cycle_peak = 0.0

        # Gate de cierre de viernes (usa flags ya calculados al inicio del tick)
        if is_friday_mode and my_positions > 0 and self._basket_net_profit() > 0:
            self._close_all("Viernes Close")
            return

        # --- ESTADO 0: ENTRY ---
        if core_count == 0:
            if self.spread_high:
                return
            if is_friday_mode or is_weekly_start_wait:
                return
            if self.last_recovery_close_time > 0 and (self.now - self.last_recovery_close_time < int(self._p("cooldown_seconds"))):
                return

            lots = self._calculate_lots(False)
            if struct_h4 >= 0 and signal_m15 == 1 and self._is_price_good_entry(mt5.ORDER_TYPE_BUY):
                if self.rsi0 < int(self._p("entry_rsi_max")):
                    if self.b.check_free_margin(lots, mt5.ORDER_TYPE_BUY):
                        self.b.market_order(mt5.ORDER_TYPE_BUY, lots, "SMC Buy Entry")
            elif struct_h4 <= 0 and signal_m15 == -1 and self._is_price_good_entry(mt5.ORDER_TYPE_SELL):
                if self.rsi0 > int(self._p("entry_rsi_min")):
                    if self.b.check_free_margin(lots, mt5.ORDER_TYPE_SELL):
                        self.b.market_order(mt5.ORDER_TYPE_SELL, lots, "SMC Sell Entry")

        # --- ESTADO 1: HEDGE MONITOR ---
        elif core_count == 1:
            p = core_positions[0]
            if p.type == mt5.POSITION_TYPE_BUY:
                profit_pts = (p.price_current - p.price_open) / point
                loss_pts = (p.price_open - p.price_current) / point
            else:
                profit_pts = (p.price_open - p.price_current) / point
                loss_pts = (p.price_current - p.price_open) / point

            if self._p("use_smart_trail") and profit_pts > (current_atr * float(self._p("trail_activate"))) / point:
                trail_dist = current_atr * float(self._p("trail_dist_atr"))
                if p.type == mt5.POSITION_TYPE_BUY:
                    new_sl = round(p.price_current - trail_dist, digits)
                    if new_sl > p.price_open and (p.sl == 0 or new_sl > p.sl):
                        self.b.modify_sl(p, new_sl)
                else:
                    new_sl = round(p.price_current + trail_dist, digits)
                    if new_sl < p.price_open and (p.sl == 0 or new_sl < p.sl):
                        self.b.modify_sl(p, new_sl)

            atr_points = current_atr / point
            dynamic_dist = atr_points * float(self._p("hedge_atr_mult"))
            active_hedge_dist = max(float(self._p("hedge_dist")), dynamic_dist)

            if loss_pts >= active_hedge_dist:
                self.b.modify_sl(p, 0)  # limpia SL antes de cubrir
                if p.type == mt5.POSITION_TYPE_BUY:
                    if self.b.check_free_margin(p.volume, mt5.ORDER_TYPE_SELL):
                        self.b.market_order(mt5.ORDER_TYPE_SELL, p.volume, "Hedge Lock")
                else:
                    if self.b.check_free_margin(p.volume, mt5.ORDER_TYPE_BUY):
                        self.b.market_order(mt5.ORDER_TYPE_BUY, p.volume, "Hedge Lock")

        # --- ESTADO 2: RECOVERY ---
        elif core_count == 2:
            if self.spread_high:
                return
            # Anti-espera v2 (OP3): tiempo minimo Y separacion por ATR del ultimo
            # leg. Antes era solo un timer fijo de 60s; ahora ademas exige que el
            # precio se haya movido >= ATR*spacing, para no apilar recovery en el
            # mismo nivel (clustering) durante ruido.
            last_leg = max(core_positions, key=lambda q: q.time)
            if self.now - last_leg.time < int(self._p("min_tech_wait")):
                return
            ref_price = self.b.ask()
            spacing = self._effective_atr() * float(self._p("recovery_min_spacing_atr"))
            if abs(ref_price - last_leg.price_open) < spacing:
                return

            base_lots = self._calculate_lots(False)
            recovery_lots = base_lots * 2.0
            if recovery_lots > float(self._p("max_recovery_lots")):
                recovery_lots = float(self._p("max_recovery_lots"))

            if self.atr0 > self.atr1:
                m5_buy = self._m5_momentum(mt5.ORDER_TYPE_BUY)
                m5_sell = self._m5_momentum(mt5.ORDER_TYPE_SELL)

                if signal_m15 == 1 or (m5_buy and signal_m15 != -1):
                    if self.b.check_free_margin(recovery_lots, mt5.ORDER_TYPE_BUY):
                        self.b.market_order(mt5.ORDER_TYPE_BUY, recovery_lots, "Recovery Buy (V-Shape)")
                if signal_m15 == -1 or (m5_sell and signal_m15 != 1):
                    if self.b.check_free_margin(recovery_lots, mt5.ORDER_TYPE_SELL):
                        self.b.market_order(mt5.ORDER_TYPE_SELL, recovery_lots, "Recovery Sell (V-Shape)")

        # --- ESTADO 3+: SENTINEL + BIO-REACTOR + TRAILING OP3 ---
        elif core_count >= 3:
            self._check_rescue()            # RF-D: corre con >=3 core (no solo ==3)
            if core_count >= 4:
                self._run_grinder()

            if self._p("use_op3_trail"):
                op3 = None
                last_t = 0
                for p in core_positions:
                    if p.time > last_t:
                        last_t = p.time
                        op3 = p

                if op3 is not None:
                    current_profit = op3.profit + op3.swap
                    if current_profit > 0:
                        if op3.type == mt5.POSITION_TYPE_BUY:
                            pts = (op3.price_current - op3.price_open) / point
                        else:
                            pts = (op3.price_open - op3.price_current) / point

                        start_pts = (current_atr * float(self._p("op3_start_atr"))) / point
                        active_dist = (current_atr * float(self._p("op3_base_dist_atr"))) / point
                        turbo_trigger_pts = (current_atr * float(self._p("op3_turbo_trigger_atr"))) / point
                        if pts >= turbo_trigger_pts:
                            active_dist = (current_atr * float(self._p("op3_turbo_dist_atr"))) / point

                        if pts > start_pts:
                            if op3.type == mt5.POSITION_TYPE_BUY:
                                new_sl = round(op3.price_current - active_dist * point, digits)
                                if op3.sl == 0 or new_sl > op3.sl:
                                    self.b.modify_sl(op3, new_sl)
                            else:
                                new_sl = round(op3.price_current + active_dist * point, digits)
                                if op3.sl == 0 or new_sl < op3.sl:
                                    self.b.modify_sl(op3, new_sl)
