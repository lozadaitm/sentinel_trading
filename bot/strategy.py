"""SentinelEngine: motor M15, derivado del EA MQL5 'M15 Gold Sentinel HyperGrinder v20.0'.

Maquina de estados derivada de los ROLES del CycleLedger (bot/ledger.py):
cada posicion tiene identidad (ENTRY/HEDGE/RECOVERY/RESCUE/ORPHAN) y el par
OP1<->OP2 es atomico. El conteo (0/1/2/3+) del MQL5 original se conserva como
profundidad del CICLO (cycle_legs: excluye huerfanos), no del magic entero:
contar posiciones sin identidad re-etiquetaba hedges huerfanos como OP1 y
construia ciclos sobre bases falsas (ver docs/memory/
unwind-orphans-hedge-count-state). on_tick() replica OnTick del MQL5.
Los parametros se reciben en self.cfg (cargados de bot_config).

El scalper M5 que el EA original llevaba embebido ("Bio-Reactor / Smart
Grinder") YA NO VIVE AQUI: se extrajo a su propio proceso y su propio magic
(bot/strategy_m5.py). Consecuencia directa: todas las posiciones que este
motor ve por su magic son cesta core, sin excepciones ni filtros de identidad.

Indexacion: ver bot/indicators.py. MQL5 [0]==.iloc[-1], [1]==.iloc[-2].
Tiempos: se usa el epoch del servidor (tick.time) para todas las comparaciones
de tiempo, consistente con position.time.
"""

import datetime

import MetaTrader5 as mt5
import pandas as pd

from . import budget, config, indicators, ledger, news

# Defaults = valores input del MQL5 (fallback si falta la columna en bot_config)
DEFAULTS = {
    "inp_max_spread": 350,
    "base_risk": 5000.0, "base_lots": 0.12, "max_entry_lots": 1.0, "max_recovery_lots": 2.0,
    "use_basket_close": True, "basket_percent": 0.15, "commission_per_lot": 6.0,
    "use_dynamic_retrace": True, "retrace_atr_mult": 0.1, "fixed_retrace": 2.0,
    "use_dynamic_hedge": True, "hedge_dist": 350, "hedge_atr_mult": 2.0,
    "use_healer": True, "healer_balance_bias": True, "healer_min_core": 3, "use_unwind_mode": True,
    "unwind_atr_mult": 2.0, "unwind_money_floor": 30.0,
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
    "min_green_profit": 3.0,          # OP12: piso verde del trail en OP1 sola; bajo esto se desarma a hedge (no cierra rojo)
    # --- Fork A: respiro/supervivencia ante volatilidad anomala (noticia/manipulacion) ---
    "use_vol_breaker": True,          # VCB: circuit breaker de volatilidad (bloquea aperturas, no la gestion)
    "vcb_atr_mult": 2.8,              # VCB: dispara si el rango de la vela M15 en curso >= ATR * este mult
    "max_net_lots": 1.0,              # tope DURO de exposicion neta core (long-short); 0 = desactivado
    "max_rescue_legs": 3,             # cap de legs de Op4 por ciclo (acota el martingala)
    "use_recovery_h4_gate": True,     # no promediar (recovery/rescue) contra la estructura H4 confirmada
    # --- NewsGuard Fase 1 (bloqueo duro por noticias rojas; ver bot/news.py) ---
    "news_enforce": False,            # False = OBSERVACION: loguea lo que habria bloqueado, no bloquea
    "news_pre_seconds": 7200,         # ventana previa: T-2h (una OP1 abierta despues llega al dato sin colchon)
    "news_post_seconds": 3600,        # ventana posterior: T+1h (clusters se funden por solape de ventanas)
    "news_currencies": "USD",         # divisas cuyas rojas cuentan (coma-separado); el oro es un trade de USD
    # --- Gobierno de margen (convivencia con el bot M5; ver bot/budget.py) ---
    "equity_weight": 0.80,            # fraccion del equity que dimensiona el lote de ESTE bot
    "margin_cap_pct": 60.0,           # techo de margen propio, en % del equity
    "ml_no_add": 200.0,               # bajo este margin level no se abren aditivas (el hedge sigue)
    "ml_flatten": 0.0,                # el senior nunca se auto-liquida: lo hace el M5
}

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

        # Gobierno de margen. Rol SENIOR: este motor no reserva margen para
        # nadie y sus protectoras (Hedge Lock) son inbloqueables. Es el bot M5
        # quien cede. Lee la config vigente por referencia (main la refresca).
        self.gov = budget.MarginGovernor(
            broker, lambda: self.cfg, budget.ROLE_SENIOR,
            peer_magics=config.PEER_MAGICS, logger=logger)

        # Identidad por ROL de cada posicion del ciclo (persistida por
        # instancia; rehidratable desde los comments de MT5 tras un restart).
        self.cycle = ledger.CycleLedger(config.LEDGER_PATH, logger)

        # NewsGuard (Fase 1): ventana dura alrededor de noticias rojas.
        # Complementa al VCB: el VCB es reactivo (la vela ya exploto), este es
        # predictivo (la vela va a explotar y esta agendada).
        self.news = news.NewsGuard(config.NEWS_CALENDAR_PATH, logger)
        self.news_active = False      # dentro de una ventana de noticia este tick
        self.news_block = False       # ventana activa Y news_enforce=True
        self.news_reason = None       # texto del evento que manda (HUD/logs)

        # Estado (globals del MQL5)
        self.last_recovery_close_time = 0
        self.last_healed_ticket = 0
        self.cycle_realized = 0.0            # B: P&L realizado del ciclo en curso (para el freeze del Unwind)
        self._healer_needs_rebaseline = True  # C: al iniciar ciclo, rebasa el cursor del Healer (presupuesto por ciclo)
        self._cover_protect_logged = False    # dedupe del aviso de cobertura protegida (1 por ciclo)
        self.max_cycle_peak = 0.0
        self.cycle_armed = False      # trailing de cesta armado (OP8/OP9)
        self.spread_high = False      # cache del filtro de spread del tick actual
        self.close_only = False       # modo wind-down: bloquea aperturas, deja gestion/cierres (bot_instances.is_active=false)
        self.is_active = False         # espejo de bot_instances.is_active (Supabase) para la UI; lo setea main.trading_loop
        self.user_email = None         # email del usuario (auth.users) para identificar la instancia en la UI
        self.last_rescue_time = 0     # cooldown de rescate (OP4)
        self.vol_breaker = False      # VCB activo este tick (Fork A)
        self.vol_breaker_prev = False # para loguear solo las transiciones del VCB
        self.struct_h4 = 0            # estructura H4 del tick (cache para gates de recovery/rescue)

        # Buffers del tick actual (rellenados por _compute_buffers)
        self.now = 0
        self.atr0 = self.atr1 = 0.0
        self.ema0 = self.rsi0 = 0.0
        self.m5_open1 = self.m5_close1 = 0.0
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

    # ==============================================================
    # Fork A: respiro/supervivencia (VCB + tope neto + gate H4)
    # ==============================================================
    def _vol_breaker_active(self):
        """Circuit breaker de volatilidad. True si la vela M15 EN CURSO es anomala
        (rango high-low >= ATR * vcb_atr_mult). Mientras este activo se bloquean
        SOLO las aperturas que ANADEN riesgo (entry, recovery, rescate), igual que
        spread_high: el Hedge (proteccion), cierres, trailing y healer siguen
        corriendo. Captura noticia, manipulacion o spikes fuera de calendario.
        """
        if not self._p("use_vol_breaker"):
            return False
        if self.df_m15 is None:
            return False
        bar_range = float(self.df_m15["high"].iloc[-1]) - float(self.df_m15["low"].iloc[-1])
        atr = self._effective_atr()
        if atr <= 0:
            return False
        return bar_range >= atr * float(self._p("vcb_atr_mult"))

    def _net_exposure(self):
        """Lotes netos de la cesta (long - short)."""
        net = 0.0
        for p in self.b.positions():
            net += p.volume if p.type == mt5.POSITION_TYPE_BUY else -p.volume
        return net

    def _net_cap_ok(self, lots, order_type):
        """True si anadir `lots` en `order_type` no rompe max_net_lots. SIEMPRE
        permite lo que REDUCE la magnitud del neto (p.ej. el hedge), aunque ya se
        este sobre el tope; solo frena los adds que LO AGRANDAN mas alla del cap.
        """
        cap = float(self._p("max_net_lots"))
        if cap <= 0:
            return True  # tope desactivado
        net = self._net_exposure()
        delta = lots if order_type == mt5.ORDER_TYPE_BUY else -lots
        new_net = net + delta
        if abs(new_net) <= abs(net):
            return True  # reduce o no cambia el neto: permitido
        return abs(new_net) <= cap

    def _h4_gate_ok(self, order_type):
        """Bloquea promediar CONTRA la estructura H4 confirmada: no comprar si H4
        es bajista, no vender si es alcista. El martingala vive en rangos y muere
        en tendencias; este gate corta el apilado contra una tendencia macro.
        """
        if not self._p("use_recovery_h4_gate"):
            return True
        if order_type == mt5.ORDER_TYPE_BUY:
            return self.struct_h4 >= 0
        return self.struct_h4 <= 0

    def _rescue_leg_count(self, legs):
        """Cuenta legs de Op4 vivos DEL CICLO (por rol), para acotar el martingala."""
        return sum(1 for p in legs
                   if self.cycle.role_of(p.ticket) == ledger.ROLE_RESCUE)

    # ==============================================================
    # Lotaje y cierre total
    # ==============================================================
    def _calculate_lots(self, is_recovery):
        # Equity PONDERADO por el peso de este bot: con dos motores sobre la
        # misma cuenta, dimensionar contra el equity completo haria que ambos
        # escalasen sobre el mismo capital (doble conteo). Ver bot/budget.py.
        equity = self.gov.sizing_equity()
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

    def close_all(self, reason):
        """Cierre inmediato de toda la cesta por comando externo (force_close
        del dashboard). El usuario asume el flotante actual; NO es un stop
        automatico (esos siguen vetados por diseño).

        A diferencia de _close_all (cierre de ciclo por TP, best-effort), aqui
        se verifica el retcode de CADA orden y al final se re-consulta MT5:
        devuelve True solo si la instancia quedo PLANA. Si el terminal rechaza
        (AutoTrading off, requote, mercado cerrado) se loguea ERROR con el
        retcode y el caller NO consume el comando: reintenta en ~3 s.
        """
        if self.b.shadow:
            self.log.write("SHADOW", f"CLOSE ALL ({reason}): sin ordenes reales en modo sombra.")
            return True
        total = 0.0
        for p in self.b.positions():
            leg_pl = p.profit + p.swap
            res = self.b.close_position(p, "Close: Forzado (usuario)")
            rc = getattr(res, "retcode", None)
            if rc not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
                self.log.write("ERROR", f"Cierre forzado RECHAZADO (retcode {rc}).",
                               p.price_current, p.volume, self.b.account_balance(),
                               ticket=p.ticket)
                continue
            total += leg_pl
            self.log.write("CIERRE", f"Leg cerrado ({reason}). PnL: {leg_pl:.2f}",
                           p.price_current, p.volume, self.b.account_balance(),
                           ticket=p.ticket)
        if self.b.positions():
            return False  # quedo algo vivo (rechazo o cierre parcial): reintentar
        # Plano: cerrar el ciclo igual que un cierre normal de cesta.
        self.log.write("EXITO", f"Cierre CICLO ({reason}). PnL: {total:.2f}",
                       balance=self.b.account_balance())
        self.last_recovery_close_time = self.now
        self.max_cycle_peak = 0.0
        self.cycle_armed = False
        self.cycle_realized = 0.0
        self._healer_needs_rebaseline = True
        self._cover_protect_logged = False
        return True

    def _close_all(self, reason):
        total_profit = 0.0
        for p in self.b.positions():
            leg_pl = p.profit + p.swap
            total_profit += leg_pl
            self.b.close_position(p, f"Close: {reason}")
            # Traza por-leg con ticket (verbose): permite reconstruir el cierre de
            # cesta pos-a-pos en bot_logs sin depender del reporte de MT5.
            self.log.write("CIERRE", f"Leg cerrado ({reason}). PnL: {leg_pl:.2f}",
                           p.price_current, p.volume, self.b.account_balance(), ticket=p.ticket)
        self.log.write("EXITO", f"Cierre CICLO ({reason}). PnL: {total_profit:.2f}",
                       balance=self.b.account_balance())
        self.last_recovery_close_time = self.now
        self.max_cycle_peak = 0.0
        self.cycle_armed = False
        self.cycle_realized = 0.0             # B: cierra el ciclo -> reinicia el realizado
        self._healer_needs_rebaseline = True  # C: el proximo ciclo rebasa el cursor del Healer
        self._cover_protect_logged = False

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
    # HEDGE LOCK (orden protectora)
    # ==============================================================
    def _open_hedge(self, p):
        """Abre el Hedge Lock y SOLO limpia el SL si el hedge quedo confirmado.

        El orden es critico. Antes se hacia `modify_sl(p, 0)` ANTES de intentar
        cubrir, de modo que un rechazo (margen insuficiente, retcode del broker)
        dejaba la posicion DESNUDA: sin SL y sin cobertura, en silencio. El M15
        no lleva SL catastrofico por diseño (ver docs/memory/bot-design-constraints),
        asi que el hedge es la unica red que congela la perdida: si no entra, el
        SL previo debe sobrevivir y hay que avisar.

        Es una orden PROTECTORA: no se pre-filtra por margen ni por ningun tope
        de presupuesto. Se intenta siempre y manda el broker; si rechaza, se
        loguea el retcode real y el bucle reintenta en el siguiente tick.
        """
        hedge_type = (mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY
                      else mt5.ORDER_TYPE_BUY)
        res = self.b.market_order(hedge_type, p.volume, "Hedge Lock")

        # En SHADOW_MODE market_order devuelve None a proposito (no envia nada):
        # se trata como exito para no ensuciar el log con errores fantasma.
        ok = self.b.shadow or (
            res is not None and getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE
        )
        if not ok:
            rc = getattr(res, "retcode", "sin respuesta del broker")
            self.log.write(
                "ERROR",
                f"Hedge Lock FALLIDO sobre ticket #{p.ticket} ({p.volume:.2f} lotes). "
                f"retcode={rc}. Margen libre: {self.b.margin_free():.2f}. "
                f"SL preservado; se reintenta en el proximo tick.",
                p.price_current, p.volume, self.b.account_balance(), ticket=p.ticket)
            return False

        # Cobertura confirmada: el par queda registrado en el ledger (quien
        # cubre a quien). Sin este vinculo, un restart re-emparejaria por
        # inferencia y un hedge de un ciclo muerto podria adoptar la OP1 nueva.
        self._register_open(res, ledger.ROLE_HEDGE, covers=p.ticket)

        # Recien ahora es seguro soltar el SL para que el par Op1+Hedge quede
        # congelado y lo gestionen los subsistemas de cesta.
        if p.sl != 0:
            self.b.modify_sl(p, 0)
        return True

    def _register_open(self, res, role, covers=None):
        """Registra en el ledger una apertura CONFIRMADA por el broker.

        En SHADOW_MODE res es None (no existe posicion real): no se registra y
        el sync() por comments cubriria cualquier hueco si algo quedo abierto.
        Devuelve True si la apertura quedo confirmada y registrada.
        """
        if res is None:
            return False
        if getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
            self.cycle.register(getattr(res, "order", 0), role, covers=covers)
            return True
        return False

    def _news_note(self, what):
        """Traza de auditoria del NewsGuard: apertura ocurrida DENTRO de una
        ventana de noticia. En modo observacion es el dato que calibra los
        umbrales antes de activar el enforcement; con enforcement activo solo
        puede ocurrir por la excepcion defensiva (rescate L3-CRITICO)."""
        if not self.news_active:
            return
        modo = "excepcion al bloqueo" if self.news_block else "observacion"
        self.log.write("NOTICIAS",
                       f"{what} abierta en ventana de noticia [{modo}]: {self.news_reason}.",
                       balance=self.b.account_balance())

    def _resize_hedge(self, entry, hedge):
        """Encoge el hedge al volumen remanente de la ENTRY (nunca lo agranda).

        Si el Healer amputo parte de la ENTRY, el exceso del hedge ya no
        congela nada: es exposicion direccional nueva disfrazada de cobertura.
        Lo realizado por el recorte entra a cycle_realized para que el freeze
        del Unwind lo vea (mismo tratamiento que una amputacion del Healer).
        """
        step = self.b.volume_step()
        excess = hedge.volume - entry.volume
        if excess < step - 1e-9:
            return
        import math
        lots = math.floor(excess / step + 1e-9) * step
        frac = lots / hedge.volume
        res = self.b.close_partial(hedge, lots, "Hedge Resize")
        if res is None or getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
            self.cycle_realized += (hedge.profit + hedge.swap) * frac
            self.log.write(
                "PROTECCION",
                f"Hedge ajustado al remanente de la OP1 (ticket #{entry.ticket}): "
                f"cerrados {lots:.2f} lotes de exceso.",
                hedge.price_current, lots, self.b.account_balance(), ticket=hedge.ticket)

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
        if self.news_active:
            estado = (self.news_reason or "ventana activa")
            if not self._p("news_enforce"):
                estado += " (obs)"
            gates.append(("Noticias", estado, "fuera de ventana roja", not self.news_block))

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
        margin_ok = self.gov.probe(lots, otype, budget.KIND_ADDITIVE)
        gates.append(("Presupuesto", self.gov.last_block or f"{self.b.margin_free():.0f} libre",
                      f"req {lots:.2f} lot", margin_ok))

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

        # Gate de profundidad: el Healer solo ampu­ta en cestas reales (OP3/OP4+).
        # Con OP1 sola (core==1) la cobertura correcta es el Hedge Lock (congela la
        # perdida en ESTADO 1); con OP1+Hedge (core==2) el hedge ya congela. Amputar
        # antes realizaria en rojo una entrada que aun debe cubrirse. Se sale ANTES de
        # tocar el cursor para NO consumir presupuesto: los ganadores nuevos se siguen
        # acumulando y quedan disponibles cuando la cesta llega a OP3+.
        if len(self.b.positions()) < int(self._p("healer_min_core")):
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
                budget += d.profit  # acumula los ganadores nuevos DEL CICLO

        # C: presupuesto acotado al ciclo. Al arrancar un ciclo nuevo se rebasa el
        # cursor al ultimo ticket para NO contar verdes de ciclos ya cerrados. Antes
        # se acumulaban cross-ciclo (cursor congelado mientras core<3) y disparaban
        # una amputacion gigante al llegar la primera cesta a OP3+. Ver docs/memory:
        # healer-budget-accumulates-across-cycles.
        if self._healer_needs_rebaseline:
            self.last_healed_ticket = max_ticket
            self._healer_needs_rebaseline = False
            return  # este pass no ampu­ta: el presupuesto del ciclo arranca en cero

        if max_ticket > self.last_healed_ticket:
            self.last_healed_ticket = max_ticket
        if budget > 0:
            self._apply_healing(budget)

    def _closing_increases_exposure(self, p):
        """True si cerrar `p` AUMENTA la exposicion neta absoluta de la cesta.

        Es la definicion estructural de "leg de cobertura": no depende del
        comment ni de quien lo abrio, sino de lo que su cierre le hace al
        riesgo direccional. El Hedge Lock cumple esto por construccion mientras
        siga cubriendo; deja de cumplirlo en cuanto la cesta se equilibra.
        """
        net = self._net_exposure()
        contrib = p.volume if p.type == mt5.POSITION_TYPE_BUY else -p.volume
        return abs(net - contrib) > abs(net)

    def _apply_healing(self, profit_available):
        """Amputa parcialmente el peor leg AMPUTABLE con el presupuesto disponible.

        Proteccion de la cobertura: mientras el P&L total del ciclo siga en
        negativo, se excluyen los legs cuyo cierre aumentaria la exposicion neta.

        Por que. `_apply_healing` era un argmin(profit+swap) sin nocion de rol:
        no distinguia entrada, cobertura, recovery ni rescate. Y como recovery y
        rescate abren SIEMPRE del lado de Op1, la cesta acumula N legs de un lado
        y uno solo del otro -> en cuanto la tendencia acompaña, el hedge es el
        peor leg por geometria, no por casualidad. En produccion, 16 de 21
        amputaciones mordieron un Hedge Lock, a 0.01 lotes por vez (el ticket
        #1609321275, 12 veces en 2h30m). Cada mordisco realiza ~90% del verde
        acumulado como perdida, asi que el bot convertia en rojo permanente lo
        que el Unwind acababa de bancar, y de paso desmontaba su unica cobertura.

        El gate replica el freeze del Unwind (cycle_realized + neto flotante):
        con la cesta en positivo la cobertura ya no hace falta y desarmarla es
        trabajo del Unwind, que lo hace ordenadamente. Ver docs/memory:
        healer-unwind-hedge-cascade.
        """
        protect_cover = (self.cycle_realized + self._basket_net_profit()) <= 0

        positions = self.b.positions()
        # Rol: los miembros de un par intacto (ENTRY<->HEDGE, huerfano<->cover)
        # son intocables mientras el ciclo siga en rojo. La regla estructural
        # de abajo no basta sola: con N recoveries del lado de la ENTRY, cerrar
        # la ENTRY (o su hedge) puede REDUCIR |neto| y pasar el filtro, pero
        # huerfana al par igual (asi murio la OP1s del 11-ago: -1,332 de
        # amputacion financiada por el churn). El par se congela completo.
        pair_frozen = self.cycle.pair_members(positions) if protect_cover else set()

        # Peor leg por DINERO REAL = profit + swap (antes solo miraba p.profit e
        # ignoraba el swap acumulado, que en holds largos puede ser material).
        worst = None
        worst_money = 1e9
        cover_skipped = 0
        for p in positions:
            if p.ticket in pair_frozen:
                cover_skipped += 1
                continue
            if protect_cover and self._closing_increases_exposure(p):
                cover_skipped += 1
                continue
            money = p.profit + p.swap
            if money < worst_money:
                worst_money = money
                worst = p

        if cover_skipped and not self._cover_protect_logged:
            self.log.write(
                "HEALER",
                f"Cobertura protegida: {cover_skipped} leg(s) excluido(s) de la "
                f"amputacion mientras el ciclo siga en negativo.",
                balance=self.b.account_balance())
            self._cover_protect_logged = True

        if worst is None or worst_money >= 0 or worst.volume <= 0:
            return

        # Costo real por lote = P&L (precio + swap) prorrateado por volumen. Es exacto
        # porque el P&L de la posicion escala lineal con el volumen (mismo precio de
        # apertura); reemplaza al calculo por diff_points, que no consideraba swap.
        cost_per_lot = -worst_money / worst.volume
        min_lot = self.b.volume_min()
        cost_of_min_lot = cost_per_lot * min_lot
        if cost_of_min_lot > profit_available:
            return  # ni el lote minimo cabe en el presupuesto disponible

        budget = profit_available * 0.90
        lots_to_close = budget / cost_per_lot
        import math
        vol_step = self.b.volume_step()
        lots_to_close = math.floor(lots_to_close / vol_step) * vol_step
        if lots_to_close > worst.volume:
            lots_to_close = worst.volume  # no cerrar mas de lo abierto

        if lots_to_close >= min_lot:
            vol_before = worst.volume  # captura antes: close_partial puede dejar worst.volume=0
            # Identidad de la operacion amputada, capturada ANTES del cierre para
            # dejar constancia en el log de a que leg se le aplico la amputacion.
            op_side = "BUY" if worst.type == mt5.POSITION_TYPE_BUY else "SELL"
            op_desc = worst.comment or "sin comentario"
            op_ticket = worst.ticket
            op_open = worst.price_open
            res = self.b.close_partial(worst, lots_to_close, "Healer Amputacion")
            if res is None or getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
                # B: registra la perdida realizada (incl. swap, prorrateada por el
                # volumen cerrado) en el acumulado del ciclo, para que el freeze del
                # Unwind la tenga en cuenta y la amputacion NO levante el neto flotante
                # abriendo la cobertura (cascade). Ver docs/memory: healer-unwind-hedge-cascade.
                frac = lots_to_close / vol_before
                self.cycle_realized += worst_money * frac
                self.log.write(
                    "HEALER",
                    f"Amputacion Tactica sobre {op_side} '{op_desc}' (ticket #{op_ticket}) "
                    f"abierta @ {op_open:.2f}. Lotes amputados: {lots_to_close:.2f}",
                    worst_money, budget, self.b.account_balance(), ticket=op_ticket)

    # ==============================================================
    # PROFIT BANKING (Unwind)
    # ==============================================================
    def _profit_banking(self):
        """Banca una posicion ganadora SOLO si la cesta total ya es neta positiva.

        Cambio clave: mientras el neto de la cesta sea <= 0 las ganadoras estan
        CUBRIENDO a las perdedoras (hedge lock). Desarmar esa cobertura re-exponia
        al perdedor desnudo (causa del -760 observado). Ahora el unwind respeta el
        congelamiento: el hedge solo se cierra cuando el saldo TOTAL es positivo.

        PARES (rol): los miembros de un par intacto (ENTRY<->HEDGE) NUNCA se
        bancan sueltos, ni siquiera con el freeze abierto. La auditoria 6-11 ago
        mostro los dos modos de fallo: (a) bancar la OP1 verde dejaba el hedge
        rojo huerfano, que la maquina re-etiquetaba como OP1 y cubria de nuevo
        (hedge-sobre-hedge); (b) bancar el hedge verde con la OP1 roja flotando
        lo reabria al tick siguiente en un loop de scalping (12+ vueltas el
        10-ago) mientras el freeze seguia abierto por los verdes del churn. Un
        par solo se desarma COMPLETO: aqui si su P&L conjunto es positivo, o en
        _close_all cuando el TOTAL de la cesta alcanza el target. Bancables
        sueltos quedan RECOVERY/RESCUE y los verdes sin par.

        RF-C corregido: la condicion de dinero y la posicion elegida son la MISMA
        (se banca la ganadora mas rica que cumpla el umbral, no se mezclan).
        RF-F: comision ida+vuelta.
        """
        if not self._p("use_unwind_mode"):
            return
        positions = self.b.positions()
        if len(positions) < 2:
            return

        # FREEZE (B): no desarmar cobertura si el P&L TOTAL del ciclo (realizado +
        # flotante) sigue en negativo. Antes se miraba SOLO el flotante, y una
        # amputacion del Healer (que realiza rojo y sube el flotante remanente) abria
        # el gate y bancaba el hedge -> cesta desnuda -> stop-out. Al incluir lo ya
        # realizado en el ciclo, la amputacion es NEUTRA para el freeze (mueve dinero
        # de flotante a realizado sin cambiar la suma) y la cascade queda rota.
        if self.cycle_realized + self._basket_net_profit() <= 0:
            return

        current_atr = self._effective_atr()
        atr_mult = float(self._p("unwind_atr_mult"))
        floor = float(self._p("unwind_money_floor"))
        pair_tickets = self.cycle.pair_members(positions)

        best_single = None
        best_pair = None
        best_money = -1e18
        for p in positions:
            if p.ticket in pair_tickets:
                continue  # miembro de par intacto: solo se desarma junto
            money = p.profit + p.swap - self._round_trip_commission(p.volume)
            if money <= 0:
                continue
            dist = abs(p.price_current - p.price_open) / current_atr if current_atr else 0.0
            if (dist >= atr_mult or money >= floor) and money > best_money:
                best_money = money
                best_single, best_pair = p, None

        for leg, hedge in self.cycle.pairs(positions):
            money = (leg.profit + leg.swap + hedge.profit + hedge.swap
                     - self._round_trip_commission(leg.volume)
                     - self._round_trip_commission(hedge.volume))
            if money <= 0:
                continue  # par congelado en rojo: intocable
            dist = max(abs(leg.price_current - leg.price_open),
                       abs(hedge.price_current - hedge.price_open))
            dist = dist / current_atr if current_atr else 0.0
            if (dist >= atr_mult or money >= floor) and money > best_money:
                best_money = money
                best_single, best_pair = None, (leg, hedge)

        if best_single is not None:
            banked_ticket = best_single.ticket
            self.b.close_position(best_single, "Unwind Profit Banking")
            self.cycle_realized += best_single.profit + best_single.swap  # B: acumula lo realizado del ciclo
            self.log.write("UNWIND", f"Profit Banking. Money: {best_money:.2f}",
                           balance=self.b.account_balance(), ticket=banked_ticket)
        elif best_pair is not None:
            leg, hedge = best_pair
            # Primero el miembro ROJO: si el segundo cierre fallara, lo que
            # queda vivo es un verde desnudo (benigno), nunca un rojo huerfano.
            first, second = ((leg, hedge)
                             if (leg.profit + leg.swap) <= (hedge.profit + hedge.swap)
                             else (hedge, leg))
            for p in (first, second):
                self.b.close_position(p, "Unwind Profit Banking")
                self.cycle_realized += p.profit + p.swap
            self.log.write("UNWIND",
                           f"Profit Banking de PAR completo (tickets #{leg.ticket} + "
                           f"#{hedge.ticket}). Money conjunto: {best_money:.2f}",
                           balance=self.b.account_balance(), ticket=leg.ticket)

    # ==============================================================
    # SENTINEL OP4 (Rescate)
    # ==============================================================
    def _check_rescue(self, positions):
        """OP4. `positions` son los legs del CICLO (cycle_legs): los huerfanos y
        sus coberturas no cuentan profundidad ni pueden ser el 'op3' de anclaje."""
        if not self._p("use_rescue_mode"):
            return
        if self.spread_high or self.vol_breaker or self.close_only:
            return  # no abrir martingala con spread alto, vela anomala (VCB) ni en close-only
        if len(positions) < 3:
            return
        if self._rescue_leg_count(positions) >= int(self._p("max_rescue_legs")):
            return  # cap de legs de Op4: acota el martingala (anti-blowup)
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

        # NewsGuard: en ventana dura no se martingalea... salvo el L3-CRITICO,
        # que es la ultima linea de defensa de un ciclo ya muy herido y no
        # debe morir por calendario (excepcion defensiva acordada por diseño).
        if self.news_block and not immediate:
            return

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
            if (signal and self._h4_gate_ok(mt5.ORDER_TYPE_SELL)
                    and self._net_cap_ok(vol_op3, mt5.ORDER_TYPE_SELL)
                    and self.gov.can_open(vol_op3, mt5.ORDER_TYPE_SELL, budget.KIND_ADDITIVE)):
                res = self.b.market_order(mt5.ORDER_TYPE_SELL, vol_op3, comment)
                if self._register_open(res, ledger.ROLE_RESCUE):
                    self._news_note(comment)
                self.last_rescue_time = self.now
        elif type_op3 == mt5.POSITION_TYPE_BUY:
            if immediate:
                signal = True  # L3: defensa inmediata
            else:
                dist = price_op3 - self.b.ask()
                if dist >= min_distance and (ignore_rsi or rsi < (100 - rescue_rsi)):
                    signal = True
            if (signal and self._h4_gate_ok(mt5.ORDER_TYPE_BUY)
                    and self._net_cap_ok(vol_op3, mt5.ORDER_TYPE_BUY)
                    and self.gov.can_open(vol_op3, mt5.ORDER_TYPE_BUY, budget.KIND_ADDITIVE)):
                res = self.b.market_order(mt5.ORDER_TYPE_BUY, vol_op3, comment)
                if self._register_open(res, ledger.ROLE_RESCUE):
                    self._news_note(comment)
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
        df5 = self._rates(config.TIMEFRAME_M5)
        dfh4 = self._rates(config.TIMEFRAME_STRUCT)
        if df15 is None or df5 is None or dfh4 is None:
            return False

        atr_period = int(self._p("atr_period"))
        atr15 = indicators.atr(df15, atr_period)
        self.atr0 = float(atr15.iloc[-1])
        self.atr1 = float(atr15.iloc[-2])
        self.ema0 = float(indicators.ema(df15["close"], int(self._p("fast_ma"))).iloc[-1])
        self.rsi0 = float(indicators.rsi(df15).iloc[-1])

        # M5 solo para confirmar el momentum de la entrada (_m5_momentum). Los
        # indicadores M5 del grinder (EMA/ADX/RSI) se fueron con el a su proceso.
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

        # NewsGuard (Fase 1): ¿estamos dentro de la ventana dura de una roja?
        # Igual que spread/VCB, SOLO condiciona aperturas aditivas: la gestion
        # (hedge, cierres, trailing, healer, unwind) nunca se congela. Con
        # news_enforce=False (default) es pura observacion: loguea y no toca.
        self.news_active, self.news_reason = self.news.check(
            self.now, int(self._p("news_pre_seconds")),
            int(self._p("news_post_seconds")), self._p("news_currencies"))
        self.news_block = self.news_active and bool(self._p("news_enforce"))

        # Snapshot read-only para la TUI (se publica aun si el tick sale temprano).
        self._build_hud(struct_h4, signal_m15, is_friday_mode, is_weekly_start_wait)

        # Spread: NO congela la gestion (RF-A). Solo bloquea aperturas que
        # ANADEN riesgo (entrada, recovery, rescate). Cierres, trailing,
        # healer y profit banking corren igual; el Hedge (proteccion) tambien.
        self.spread_high = self.b.spread() > int(self._p("inp_max_spread"))

        # VCB (Fork A): circuit breaker de volatilidad. Bloquea las aperturas que
        # anaden riesgo durante velas anomalas (noticia/manipulacion/spike); la
        # gestion (hedge, cierres, trailing, healer) sigue. Cachea la estructura H4
        # para los gates de recovery/rescate. Solo se loguea la transicion on/off.
        self.struct_h4 = struct_h4
        self.vol_breaker = self._vol_breaker_active()
        if self.vol_breaker != self.vol_breaker_prev:
            estado = "ACTIVADO" if self.vol_breaker else "liberado"
            self.log.write("VCB", f"Circuit breaker de volatilidad {estado}.",
                           balance=self.b.account_balance())
            self.vol_breaker_prev = self.vol_breaker

        # Identidad por ROL: reconcilia el ledger ANTES de los subsistemas,
        # para que Unwind/Healer conozcan los pares vigentes de este tick
        # (adopcion por comment tras un restart, hedges viudos -> ORPHAN,
        # promocion de huerfano solitario a ENTRY).
        self.cycle.sync(self.b.positions())

        # Subsistemas transversales
        self._check_healing()
        self._profit_banking()

        # Todas las posiciones del magic son cesta core: el grinder salio del
        # Sentinel a su propio proceso (bot/strategy_m5.py), con su propio magic.
        # Se relee DESPUES de los subsistemas (pueden haber cerrado legs).
        core_positions = self.b.positions()
        my_positions = core_count = len(core_positions)

        # Vistas por ROL. La profundidad de la maquina de estados es la del
        # CICLO (cycle_legs: sin huerfanos ni sus coberturas): un hedge viudo
        # de un ciclo muerto no debe fabricar Recovery/OP4 ni bloquear nada.
        entry = self.cycle.entry_pos(core_positions)
        entry_hedge = (self.cycle.hedge_of(entry.ticket, core_positions)
                       if entry is not None else None)
        cycle_legs = self.cycle.cycle_legs(core_positions)
        cycle_count = len(cycle_legs)

        # Red de seguridad de ciclo (B/C): si NO hay cesta core (cerro por _close_all,
        # stop-out del broker o SL), reinicia el realizado del ciclo y marca rebaseline
        # del Healer. Cubre los finales de ciclo que no pasan por _close_all.
        if core_count == 0:
            self.cycle_realized = 0.0
            self._healer_needs_rebaseline = True
            self._cover_protect_logged = False

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
                    # OP12: piso verde para OP1 SOLA (core_count == 1). Una entrada
                    # desnuda nunca debe cerrarse en rojo por el trail; eso actuaria
                    # como un SL y contradice la filosofia hedge-congela (OP2 congela
                    # la perdida, no hay SL catastrofico). Si el cierre caeria bajo
                    # min_green (reverson veloz que cruza el piso entre ticks), se
                    # DESARMA y la posicion vuelve a ESTADO 1 este mismo tick para que
                    # el Hedge Lock la cubra a hedge_dist. Con >=2 legs la cesta es
                    # real: bankear el neto (con legs rojas individuales) es el
                    # comportamiento deseado y se mantiene intacto.
                    if core_count == 1 and net_pl < float(self._p("min_green_profit")):
                        self.cycle_armed = False
                        self.max_cycle_peak = 0.0
                    else:
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

        # Distancia de cobertura vigente (comun al monitor y al watchdog)
        atr_points = current_atr / point
        dynamic_dist = atr_points * float(self._p("hedge_atr_mult"))
        active_hedge_dist = max(float(self._p("hedge_dist")), dynamic_dist)

        # --- HEDGE MONITOR (por ROL: corre a CUALQUIER conteo) ---
        # Antes vivia en el ESTADO 1 (count==1): una OP1 que se quedaba sin
        # cobertura con la cesta ya profunda (op3/op4 abiertas) era imposible
        # de cubrir. Ahora la ENTRY sin hedge vivo se congela a hedge_dist sin
        # importar cuantos legs mas existan.
        if entry is not None:
            if entry.type == mt5.POSITION_TYPE_BUY:
                profit_pts = (entry.price_current - entry.price_open) / point
                loss_pts = (entry.price_open - entry.price_current) / point
            else:
                profit_pts = (entry.price_open - entry.price_current) / point
                loss_pts = (entry.price_current - entry.price_open) / point

            if entry_hedge is None:
                if loss_pts >= active_hedge_dist:
                    self._open_hedge(entry)
            else:
                # Invariante del par: el hedge nunca mas grande que la ENTRY
                # (si el Healer amputo parte de la ENTRY, se recorta el exceso).
                self._resize_hedge(entry, entry_hedge)

            # Smart trail: solo con la entrada SOLA (comportamiento original
            # del ESTADO 1; con mas legs la gestiona la cesta).
            if (core_count == 1 and self._p("use_smart_trail")
                    and profit_pts > (current_atr * float(self._p("trail_activate"))) / point):
                trail_dist = current_atr * float(self._p("trail_dist_atr"))
                if entry.type == mt5.POSITION_TYPE_BUY:
                    new_sl = round(entry.price_current - trail_dist, digits)
                    if new_sl > entry.price_open and (entry.sl == 0 or new_sl > entry.sl):
                        self.b.modify_sl(entry, new_sl)
                else:
                    new_sl = round(entry.price_current + trail_dist, digits)
                    if new_sl < entry.price_open and (entry.sl == 0 or new_sl < entry.sl):
                        self.b.modify_sl(entry, new_sl)

        # --- WATCHDOG de exposicion desnuda (huerfanos sin cobertura) ---
        # Red final para lo que el ledger no vio nacer (restarts, cierres
        # manuales): un leg huerfano en rojo mas alla de la distancia de hedge
        # se congela igual que una OP1. Orden protectora: sin pre-filtros.
        for orphan in self.cycle.naked_orphans(core_positions):
            if orphan.type == mt5.POSITION_TYPE_BUY:
                orphan_loss = (orphan.price_open - orphan.price_current) / point
            else:
                orphan_loss = (orphan.price_current - orphan.price_open) / point
            if orphan_loss >= active_hedge_dist:
                self._open_hedge(orphan)

        # --- ESTADO 0: ENTRY (sin ciclo activo; huerfanos congelados no bloquean) ---
        if cycle_count == 0:
            if self.spread_high or self.vol_breaker or self.close_only or self.news_block:
                return
            if is_friday_mode or is_weekly_start_wait:
                return
            if self.last_recovery_close_time > 0 and (self.now - self.last_recovery_close_time < int(self._p("cooldown_seconds"))):
                return

            lots = self._calculate_lots(False)
            if struct_h4 >= 0 and signal_m15 == 1 and self._is_price_good_entry(mt5.ORDER_TYPE_BUY):
                if self.rsi0 < int(self._p("entry_rsi_max")):
                    if self.gov.can_open(lots, mt5.ORDER_TYPE_BUY, budget.KIND_ADDITIVE):
                        res = self.b.market_order(mt5.ORDER_TYPE_BUY, lots, "SMC Buy Entry")
                        if self._register_open(res, ledger.ROLE_ENTRY):
                            self._news_note("OP1 BUY")
            elif struct_h4 <= 0 and signal_m15 == -1 and self._is_price_good_entry(mt5.ORDER_TYPE_SELL):
                if self.rsi0 > int(self._p("entry_rsi_min")):
                    if self.gov.can_open(lots, mt5.ORDER_TYPE_SELL, budget.KIND_ADDITIVE):
                        res = self.b.market_order(mt5.ORDER_TYPE_SELL, lots, "SMC Sell Entry")
                        if self._register_open(res, ledger.ROLE_ENTRY):
                            self._news_note("OP1 SELL")

        # --- ESTADO 2: RECOVERY (ciclo = OP1 + hedge) ---
        elif cycle_count == 2:
            if self.spread_high or self.vol_breaker or self.close_only or self.news_block:
                return
            # Anti-espera v2 (OP3): tiempo minimo Y separacion por ATR del ultimo
            # leg. Antes era solo un timer fijo de 60s; ahora ademas exige que el
            # precio se haya movido >= ATR*spacing, para no apilar recovery en el
            # mismo nivel (clustering) durante ruido.
            last_leg = max(cycle_legs, key=lambda q: q.time)
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
                # OP3 (Fork A): direccion anclada a ESTRUCTURA H4, no a ruido M5. Si
                # H4 acompana a la cesta se promedia; si H4 volteo en contra, el add
                # cae al lado opuesto (de-risk con la tendencia) en vez de promediar
                # contra el movimiento. Gateado por el tope de exposicion neta.
                if struct_h4 > 0:
                    rtype, label = mt5.ORDER_TYPE_BUY, "Recovery Buy (V-Shape)"
                elif struct_h4 < 0:
                    rtype, label = mt5.ORDER_TYPE_SELL, "Recovery Sell (V-Shape)"
                else:
                    rtype = None
                if (rtype is not None and self._net_cap_ok(recovery_lots, rtype)
                        and self.gov.can_open(recovery_lots, rtype, budget.KIND_ADDITIVE)):
                    res = self.b.market_order(rtype, recovery_lots, label)
                    if self._register_open(res, ledger.ROLE_RECOVERY):
                        self._news_note("Recovery")

        # --- ESTADO 3+: SENTINEL + TRAILING OP3 ---
        elif cycle_count >= 3:
            self._check_rescue(cycle_legs)  # RF-D: corre con >=3 legs de ciclo (no solo ==3)

            if self._p("use_op3_trail"):
                # 'Op3' = ultimo leg DEL CICLO (un huerfano o su cobertura,
                # aunque sean mas recientes, no son el leg a trailear).
                op3 = None
                last_t = 0
                for p in cycle_legs:
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
