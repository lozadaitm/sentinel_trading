"""Wrapper fino sobre MetaTrader5.

Centraliza precios, info de simbolo, posiciones (filtradas por magic),
envio/cierre/modificacion de ordenes y calculo de margen. Respeta
SHADOW_MODE: si esta activo, las operaciones de escritura se loguean
pero NO se envian al broker.
"""

import datetime
from types import SimpleNamespace

import MetaTrader5 as mt5

from . import config

# Traduccion de los retcodes que mas cuesta diagnosticar leyendo solo el numero.
_RETCODE_AYUDA = {
    10004: "Requote: el precio se movio. Transitorio.",
    10006: "Rechazada por el broker.",
    10014: "Volumen invalido para el simbolo.",
    10015: "Precio invalido.",
    10016: "Stops invalidos: el SL/TP cae dentro del SYMBOL_TRADE_STOPS_LEVEL.",
    10017: "Trading deshabilitado para la cuenta.",
    10018: "Mercado cerrado.",
    10019: "Fondos insuficientes para abrir ese volumen.",
    10021: "Sin precios: el simbolo no cotiza ahora mismo.",
    10026: "AutoTrading desactivado en el SERVIDOR del broker.",
    10027: "AutoTrading desactivado en el TERMINAL: boton 'Algo Trading' (Ctrl+E).",
    10030: "Modo de llenado no soportado por el broker (type_filling).",
}


class Broker:
    def __init__(self, logger, shadow=False):
        self.symbol = config.SYMBOL
        self.magic = config.MAGIC_NUMBER
        self.logger = logger
        self.shadow = shadow
        self._block_reason = None   # motivo de bloqueo ya logueado (anti-spam)
        self._last_reject = None    # ultimo (retcode, comment) rechazado

    # --------------------------------------------------------------
    # Info de simbolo / cuenta
    # --------------------------------------------------------------
    def _info(self):
        return mt5.symbol_info(self.symbol)

    def point(self):
        return self._info().point

    def digits(self):
        return self._info().digits

    def tick_value(self):
        tv = self._info().trade_tick_value
        return tv if tv else 1.0

    def volume_step(self):
        return self._info().volume_step

    def volume_min(self):
        return self._info().volume_min

    def volume_max(self):
        return self._info().volume_max

    def spread(self):
        return self._info().spread

    def stops_level(self):
        """SYMBOL_TRADE_STOPS_LEVEL: distancia minima (en puntos) del SL/TP al precio.

        El broker rechaza cualquier modificacion que caiga dentro de esa zona
        congelada. El trailing debe respetarla o se pasa la vida recibiendo
        retcodes de 'invalid stops'.
        """
        info = self._info()
        return getattr(info, "trade_stops_level", 0) if info else 0

    def ask(self):
        return mt5.symbol_info_tick(self.symbol).ask

    def bid(self):
        return mt5.symbol_info_tick(self.symbol).bid

    def account_balance(self):
        info = mt5.account_info()
        return info.balance if info else 0.0

    def account_equity(self):
        info = mt5.account_info()
        return info.equity if info else 0.0

    def margin_free(self):
        info = mt5.account_info()
        return info.margin_free if info else 0.0

    # --------------------------------------------------------------
    # Posiciones
    # --------------------------------------------------------------
    def positions(self):
        """Lista de posiciones del simbolo con nuestro magic."""
        return self.positions_of_magic(self.magic)

    def positions_all(self):
        """TODAS las posiciones del simbolo, sin filtrar por magic.

        Base de la coordinacion entre bots: cada proceso ve la cesta del otro
        leyendo directamente de MT5 (fuente de verdad, latencia cero) en vez de
        pasar por la DB. Ver bot/budget.py.
        """
        pos = mt5.positions_get(symbol=self.symbol)
        return list(pos) if pos else []

    def positions_of_magic(self, magic):
        """Posiciones del simbolo pertenecientes a un magic concreto."""
        return [p for p in self.positions_all() if p.magic == magic]

    # --------------------------------------------------------------
    # Margen
    # --------------------------------------------------------------
    def margin_level(self):
        """Nivel de margen en % (equity/margin*100). Infinito si no hay margen usado."""
        info = mt5.account_info()
        if info is None:
            return 0.0
        if not info.margin:
            return float("inf")  # sin posiciones: no hay presion de margen
        return info.margin_level

    def stop_out_levels(self):
        """(margin_call, stop_out) declarados por el broker, en %.

        Sirven para calibrar la escalera de degradacion contra los umbrales
        reales de la cuenta en vez de contra numeros inventados.
        """
        info = mt5.account_info()
        if info is None:
            return (0.0, 0.0)
        return (info.margin_so_call, info.margin_so_so)

    def hedged_margin_rate(self):
        """(margin_hedged, margin_hedged_use_leg) del simbolo.

        Si margin_hedged es 0, un par cubierto (Op1+Hedge) no consume margen
        adicional y el presupuesto es holgado. Si cobra completo, el lock es lo
        mas caro del sistema. Cambia por completo el dimensionado.
        """
        info = self._info()
        if info is None:
            return (0.0, False)
        return (getattr(info, "margin_hedged", 0.0),
                bool(getattr(info, "margin_hedged_use_leg", False)))

    def order_margin(self, order_type, lots):
        """Margen requerido para abrir `lots` al precio actual. 0.0 si MT5 no responde."""
        if lots <= 0:
            return 0.0
        price = self.ask() if order_type == mt5.ORDER_TYPE_BUY else self.bid()
        required = mt5.order_calc_margin(order_type, self.symbol, lots, price)
        return float(required) if required is not None else 0.0

    def magic_margin(self, magic):
        """Margen aproximado consumido por las posiciones de un magic.

        Aproximacion por suma de order_calc_margin leg a leg: MT5 no expone el
        margen por posicion, y account_info().margin es de la cuenta entera
        (compartida entre bots). Sobreestima cuando el broker aplica descuento
        por cobertura (margin_hedged), lo que va del lado conservador.
        """
        total = 0.0
        for p in self.positions_of_magic(magic):
            otype = (mt5.ORDER_TYPE_BUY if p.type == mt5.POSITION_TYPE_BUY
                     else mt5.ORDER_TYPE_SELL)
            total += self.order_margin(otype, p.volume)
        return total

    def own_margin(self):
        """Margen aproximado consumido por ESTE bot (su propio magic)."""
        return self.magic_margin(self.magic)

    def check_free_margin(self, lots, order_type):
        """Replica CheckFreeMargin (MQL5 143-148)."""
        margin_required = self.order_margin(order_type, lots)
        if margin_required <= 0:
            return False
        return self.margin_free() >= margin_required * 1.1

    # --------------------------------------------------------------
    # Permiso de trading
    # --------------------------------------------------------------
    def trade_allowed(self):
        """(permitido, motivo). Comprueba terminal y cuenta ANTES de mandar nada.

        Sin esto el motor descubre el bloqueo orden a orden: on_tick corre cada
        segundo y un AutoTrading apagado produce un rechazo por segundo, cada uno
        con su ERROR en bot_logs. Son estados que no se arreglan solos, asi que
        conviene detectarlos antes de enviar y avisar UNA vez.
        """
        ti = mt5.terminal_info()
        if ti is not None and not ti.trade_allowed:
            return False, ("AutoTrading DESACTIVADO en el terminal. Pulsa el boton "
                           "'Algo Trading' (Ctrl+E); debe quedar en verde.")
        ai = mt5.account_info()
        if ai is not None:
            if not ai.trade_allowed:
                return False, "El broker tiene el trading deshabilitado para esta cuenta."
            if not ai.trade_expert:
                return False, "El broker no permite EAs/algoritmos en esta cuenta."
        return True, None

    def _log_block(self, motivo):
        """Loguea el bloqueo solo cuando CAMBIA, y su levantamiento."""
        if motivo == self._block_reason:
            return
        if motivo is None:
            self.logger.write("SYSTEM", "Trading rehabilitado: se reanudan las aperturas.",
                              balance=self.account_balance())
        else:
            self.logger.write("ERROR", f"APERTURAS BLOQUEADAS: {motivo}",
                              balance=self.account_balance())
        self._block_reason = motivo

    # --------------------------------------------------------------
    # Ordenes
    # --------------------------------------------------------------
    @staticmethod
    def _open_log_type(comment):
        """Mapea el comment de apertura a un log_type analizable en bot_logs."""
        c = comment or ""
        # "Grinder ..." es el prefijo historico del scalper embebido en el
        # Sentinel (ya extraido); "M5 ..." es el del motor M5 actual. Se
        # conservan ambos para que el histórico de bot_logs siga clasificando.
        if c.startswith("Grinder") or c.startswith("M5 "):
            return "GRINDER"
        if c.startswith("Hedge"):
            return "PROTECCION"
        if c.startswith("Recovery"):
            return "RECOVERY"
        if c.startswith("Sentinel Op 4"):
            return "RESCATE"
        if c.startswith("SMC"):
            return "ENTRADA"
        return "OPERACION"

    def _log_open(self, res, order_type, lots, price, comment):
        """Loguea el resultado de una apertura: evento con ticket si fue aceptada,
        o ERROR con el retcode si el broker la rechazo. Nunca tumba el trading."""
        side = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
        bal = self.account_balance()
        if res is None:
            self.logger.write("ERROR", f"Apertura {side} '{comment}' sin respuesta del broker.",
                              price, lots, bal)
            return
        ticket = getattr(res, "order", 0) or 0
        if getattr(res, "retcode", None) == mt5.TRADE_RETCODE_DONE:
            self._last_reject = None
            self.logger.write(self._open_log_type(comment), f"Apertura {side}: {comment}",
                              price, lots, bal, ticket=ticket)
            return

        rc = getattr(res, "retcode", "?")
        # Anti-spam: on_tick corre cada segundo, asi que un rechazo persistente
        # (mercado cerrado, stops invalidos, sin dinero) generaria un ERROR por
        # segundo. Se registra el primero de cada racha y, al terminar, cuantos
        # hubo. Un rechazo distinto rompe la racha y vuelve a loguear.
        if self._last_reject is not None and self._last_reject[0:2] == (rc, comment):
            self._last_reject = (rc, comment, self._last_reject[2] + 1)
            return
        if self._last_reject is not None and self._last_reject[2] > 1:
            self.logger.write("ERROR",
                              f"(el rechazo anterior se repitio {self._last_reject[2]} veces)",
                              balance=bal)
        self._last_reject = (rc, comment, 1)
        self.logger.write("ERROR", f"Apertura {side} '{comment}' RECHAZADA (retcode {rc}). "
                                   f"{_RETCODE_AYUDA.get(rc, '')}".strip(),
                          price, lots, bal, ticket=ticket)

    def market_order(self, order_type, lots, comment, sl=0.0, tp=0.0):
        """Abre a mercado. `sl`/`tp` en PRECIO (0 = sin stop), como MQL5.

        El Sentinel M15 abre siempre sin SL (su red es el Hedge Lock); el motor
        M5 abre con SL por ATR desde el primer tick.
        """
        if self.shadow:
            self.logger.write("SHADOW", f"ORDER {('BUY' if order_type==mt5.ORDER_TYPE_BUY else 'SELL')} {comment}", 0, lots)
            return None

        # Puerta previa: si el terminal o la cuenta no permiten operar, no se
        # manda nada. Devolvemos el retcode real para que el llamante lo trate
        # como el rechazo que es (p.ej. _open_hedge preserva el SL).
        allowed, motivo = self.trade_allowed()
        self._log_block(motivo)
        if not allowed:
            return SimpleNamespace(retcode=mt5.TRADE_RETCODE_CLIENT_DISABLES_AT, order=0)

        price = self.ask() if order_type == mt5.ORDER_TYPE_BUY else self.bid()
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(lots),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 50,
            "magic": self.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(request)
        self._log_open(res, order_type, lots, price, comment)
        return res

    def _close_volume(self, position, volume, comment):
        order_type = (
            mt5.ORDER_TYPE_SELL
            if position.type == mt5.POSITION_TYPE_BUY
            else mt5.ORDER_TYPE_BUY
        )
        price = self.bid() if position.type == mt5.POSITION_TYPE_BUY else self.ask()
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(volume),
            "type": order_type,
            "position": position.ticket,
            "price": price,
            "deviation": 50,
            "magic": self.magic,
            "comment": comment,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        return mt5.order_send(request)

    def close_position(self, position, comment="Close"):
        if self.shadow:
            self.logger.write("SHADOW", f"CLOSE ticket {position.ticket} {comment}", 0, position.volume)
            return None
        return self._close_volume(position, position.volume, comment)

    def close_partial(self, position, lots, comment="Partial"):
        if self.shadow:
            self.logger.write("SHADOW", f"PARTIAL ticket {position.ticket} {comment}", 0, lots)
            return None
        return self._close_volume(position, lots, comment)

    def modify_sl(self, position, sl):
        """Modifica SL (TRADE_ACTION_SLTP). sl=0 limpia el SL."""
        if self.shadow:
            self.logger.write("SHADOW", f"MODIFY SL ticket {position.ticket} -> {sl}")
            return None
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": position.ticket,
            "sl": float(sl),
            "tp": float(position.tp),
            "magic": self.magic,
        }
        return mt5.order_send(request)

    # --------------------------------------------------------------
    # Historial
    # --------------------------------------------------------------
    def history_deals(self, from_dt, to_dt):
        deals = mt5.history_deals_get(from_dt, to_dt)
        return list(deals) if deals else []

    def capital_flows(self, min_ticket, days=45):
        """Deals de deposito/retiro/credito de la CUENTA (DEAL_TYPE_BALANCE /
        DEAL_TYPE_CREDIT) con ticket > min_ticket, mirando `days` hacia atras.

        El ancla es el TICKET (creciente por cuenta) y no el tiempo: los deals
        de MT5 vienen en hora del servidor y anclar por reloj local arriesga
        saltarse o duplicar una ventana de horas. La ventana de dias solo
        acota la consulta (con margen por TZ); un flujo mas viejo que `days`
        con el bot apagado todo ese tiempo se perderia (raro; asumido).
        """
        now = datetime.datetime.now()
        deals = mt5.history_deals_get(
            now - datetime.timedelta(days=days),
            now + datetime.timedelta(days=2),
        ) or []
        kinds = (mt5.DEAL_TYPE_BALANCE, mt5.DEAL_TYPE_CREDIT)
        floor = int(min_ticket or 0)
        return [d for d in deals if d.type in kinds and d.ticket > floor]

    def history_deals_for_position(self, ticket):
        """Todos los deals (IN + OUT/OUT_BY) de una posicion por su ticket.

        Permite reconstruir el cierre exacto (precio, hora, P&L total incl.
        cierres parciales del Healer/Unwind) sin depender de una ventana de
        tiempo. Ver bot/db.py::upsert_positions (bot_positions).
        """
        deals = mt5.history_deals_get(position=ticket)
        return list(deals) if deals else []
