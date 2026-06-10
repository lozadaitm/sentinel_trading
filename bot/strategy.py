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
}

_LOT_EPS = 1e-8


class SentinelEngine:
    def __init__(self, broker, logger):
        self.b = broker
        self.log = logger
        self.cfg = {}

        # Estado (globals del MQL5)
        self.last_recovery_close_time = 0
        self.last_healed_ticket = 0
        self.max_cycle_peak = 0.0

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
        """Init lastHealedTicket desde el ultimo deal de las ultimas 48h (OnInit MQL5 190-194)."""
        to_dt = datetime.datetime.now()
        from_dt = to_dt - datetime.timedelta(days=2)
        deals = self.b.history_deals(from_dt, to_dt)
        if deals:
            self.last_healed_ticket = deals[-1].ticket

    # ==============================================================
    # Utils numericas (UTILS MQL5)
    # ==============================================================
    def _dynamic_money(self, percent):
        return (self.b.account_balance() * percent) / 100.0

    def _effective_atr(self):
        return max(self.atr0, self.b.point() * 150)

    def _is_grinder(self, p):
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
    # Calculos de cesta
    # ==============================================================
    def _basket_net_profit(self):
        commission = float(self._p("commission_per_lot"))
        net = 0.0
        for p in self.b.positions():
            net += p.profit + p.swap
            net -= p.volume * commission
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
        if not self._p("use_healer"):
            return
        to_dt = datetime.datetime.now()
        from_dt = to_dt - datetime.timedelta(hours=1)
        deals = self.b.history_deals(from_dt, to_dt)
        for d in reversed(deals):
            if d.ticket <= self.last_healed_ticket:
                break
            if d.magic == self.b.magic and d.entry == mt5.DEAL_ENTRY_OUT:
                if d.profit > 0:
                    self._apply_healing(d.profit)
                    self.last_healed_ticket = d.ticket
                    return

    def _apply_healing(self, profit_available):
        worst = None
        worst_profit = 1e9
        for p in self.b.positions():
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
            return

        budget = profit_available * 0.90
        lots_to_close = budget / (diff_points * tick_value)
        import math
        vol_step = self.b.volume_step()
        lots_to_close = math.floor(lots_to_close / vol_step) * vol_step

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
        if not self.b.check_free_margin(grinder_lots, mt5.ORDER_TYPE_BUY):
            return

        # A. Time stop / limpieza
        grinder_ops = 0
        time_stop = int(self._p("grinder_time_stop")) * 60
        for p in self.b.positions():
            if self._is_grinder(p):
                grinder_ops += 1
                if self.now - p.time > time_stop and p.profit < 0:
                    self.b.close_position(p, "Grinder TimeStop")
                    self.log.write("GRINDER", "TimeStop Activado. Limpiando zona.", p.profit,
                                   balance=self.b.account_balance())
                    return
        if grinder_ops >= 1:
            return  # Solo 1 Grinder a la vez

        # B. Entrada SmartCut M5
        adx = self.g_adx0
        rsi = self.g_rsi0
        close_m5 = self.m5_close0
        ma_m5 = self.g_ema0

        if adx < int(self._p("grinder_adx_trend")):
            # Modo Scalper (reversion) - ADX bajo
            if rsi < int(self._p("grinder_rsi_os")):
                self.b.market_order(mt5.ORDER_TYPE_BUY, grinder_lots, "Grinder Scalp Buy")
            elif rsi > int(self._p("grinder_rsi_ob")):
                self.b.market_order(mt5.ORDER_TYPE_SELL, grinder_lots, "Grinder Scalp Sell")
        else:
            # Modo Surfer (tendencia) - ADX alto
            if close_m5 > ma_m5 and rsi < 70:
                self.b.market_order(mt5.ORDER_TYPE_BUY, grinder_lots, "Grinder Surf Buy")
            elif close_m5 < ma_m5 and rsi > 30:
                self.b.market_order(mt5.ORDER_TYPE_SELL, grinder_lots, "Grinder Surf Sell")

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
        if not self._p("use_unwind_mode"):
            return
        commission = float(self._p("commission_per_lot"))
        current_atr = self._effective_atr()

        best_pos = None
        best_dist = -1.0
        best_money = -999999.0
        my_count = 0

        for p in self.b.positions():
            my_count += 1
            profit = p.profit + p.swap
            if profit <= 0:
                continue
            net_profit = profit - (p.volume * commission)
            if net_profit > best_money:
                best_money = net_profit
            raw_dist = abs(p.price_current - p.price_open)
            atr_multiples = raw_dist / current_atr if current_atr else 0.0
            if atr_multiples > best_dist:
                best_dist = atr_multiples
                best_pos = p

        if my_count < 2:
            return
        if best_pos is not None:
            if best_dist >= float(self._p("unwind_atr_mult")) or best_money >= float(self._p("unwind_money_floor")):
                self.b.close_position(best_pos, "Unwind Profit Banking")
                self.log.write("UNWIND", f"Profit Banking. Money: {best_money:.2f}",
                               balance=self.b.account_balance())

    # ==============================================================
    # SENTINEL OP4 (Rescate)
    # ==============================================================
    def _check_rescue(self):
        if not self._p("use_rescue_mode"):
            return
        positions = self.b.positions()
        if len(positions) < 3:
            return

        current_dd = self._basket_net_profit()
        abs_dd = abs(current_dd)
        level2 = self._dynamic_money(float(self._p("dd_percent_l2")))
        level3 = self._dynamic_money(float(self._p("dd_percent_l3")))

        active_atr_mult = float(self._p("rescue_atr_mult"))
        ignore_rsi = False
        force_entry = False
        if abs_dd > level2:
            active_atr_mult = 3.0
            ignore_rsi = True
        if abs_dd > level3:
            active_atr_mult = 2.0
            ignore_rsi = True
            force_entry = True

        # Op3 = ultima posicion abierta (por tiempo)
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
            dist = self.b.bid() - price_op3
            if dist >= min_distance:
                if ignore_rsi or rsi > rescue_rsi:
                    if not force_entry or self._m5_momentum(mt5.ORDER_TYPE_SELL):
                        signal = True
            if signal and self.b.check_free_margin(vol_op3, mt5.ORDER_TYPE_SELL):
                self.b.market_order(mt5.ORDER_TYPE_SELL, vol_op3, comment)
        elif type_op3 == mt5.POSITION_TYPE_BUY:
            dist = price_op3 - self.b.ask()
            if dist >= min_distance:
                if ignore_rsi or rsi < (100 - rescue_rsi):
                    if not force_entry or self._m5_momentum(mt5.ORDER_TYPE_BUY):
                        signal = True
            if signal and self.b.check_free_margin(vol_op3, mt5.ORDER_TYPE_BUY):
                self.b.market_order(mt5.ORDER_TYPE_BUY, vol_op3, comment)

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

    def _compute_buffers(self):
        tick = mt5.symbol_info_tick(config.SYMBOL)
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
            return

        # Filtro de spread
        if self.b.spread() > int(self._p("inp_max_spread")):
            return

        # Subsistemas transversales
        self._check_healing()
        self._profit_banking()
        self._grinder_trailing()

        positions = self.b.positions()
        buy_count = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_BUY)
        sell_count = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_SELL)
        my_positions = buy_count + sell_count

        current_atr = self._effective_atr()
        point = self.b.point()
        digits = self.b.digits()

        # MONITOR DE SALIDA
        if my_positions > 0:
            net_pl = self._basket_net_profit()
            target = self._dynamic_money(float(self._p("basket_percent")))
            if target < 2.0:
                target = 2.0

            if net_pl >= target:
                if net_pl > self.max_cycle_peak:
                    self.max_cycle_peak = net_pl
                allowed_retrace = float(self._p("fixed_retrace"))
                if self._p("use_dynamic_retrace"):
                    vol_money = self._vol_risk_money()
                    allowed_retrace = vol_money * float(self._p("retrace_atr_mult"))
                    allowed_retrace = max(1.0, min(allowed_retrace, 50.0))
                if self.max_cycle_peak - net_pl >= allowed_retrace:
                    self._close_all("Basket Profit Trail")
                    return

            if my_positions >= 4 and self._p("use_rescue_mode"):
                rescue_target = self._dynamic_money(float(self._p("rescue_target_pct")))
                if net_pl >= rescue_target:
                    self._close_all("Rescue Mission Success")
                    return
        else:
            self.max_cycle_peak = 0.0

        # Gates de tiempo (servidor)
        dt = self._server_dt()
        dow = (dt.weekday() + 1) % 7  # MQL5: domingo=0 ... sabado=6
        hour = dt.hour
        is_friday_mode = (self._p("close_friday") and dow == 5 and hour >= int(self._p("friday_hour")))
        if is_friday_mode and my_positions > 0 and self._basket_net_profit() > 0:
            self._close_all("Viernes Close")
            return
        is_weekly_start_wait = (dow == 0 or (dow == 1 and hour < int(self._p("monday_start_hour"))))

        struct_h4 = indicators.get_h4_structure(self.df_h4, self._p("use_h4_struct"))
        signal_m15 = indicators.check_m15_breakout(self.df_m15)

        # --- ESTADO 0: ENTRY ---
        if my_positions == 0:
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
        elif my_positions == 1:
            p = positions[0]
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
        elif my_positions == 2:
            op2_time = max(p.time for p in positions)
            if self.now - op2_time < int(self._p("min_tech_wait")):
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
        elif my_positions >= 3:
            if my_positions == 3:
                self._check_rescue()
            if my_positions >= 4:
                self._run_grinder()

            if self._p("use_op3_trail"):
                op3 = None
                last_t = 0
                for p in self.b.positions():
                    if self._is_grinder(p):
                        continue
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
