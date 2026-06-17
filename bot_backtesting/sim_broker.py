"""Broker simulado: misma interfaz que bot.broker.Broker, sin MT5 real.

Modela posiciones, cuenta (balance/equity/margen), cierres totales/parciales,
modificacion de SL, auto-cierre por SL al tocar el rango de la barra, y un
historial de deals que alimenta al Healer. El PnL de cada deal es BRUTO (sin
comision) para coincidir con lo que ve el Healer en vivo; la comision se descuenta
del balance aparte (apertura + cierre).
"""

import calendar
from types import SimpleNamespace

import MetaTrader5 as mt5

from bot import config


class SimPosition:
    __slots__ = ("ticket", "type", "volume", "price_open", "price_current",
                 "sl", "tp", "time", "comment", "magic", "swap", "profit")

    def __init__(self, ticket, ptype, volume, price_open, ptime, comment, magic):
        self.ticket = ticket
        self.type = ptype
        self.volume = volume
        self.price_open = price_open
        self.price_current = price_open
        self.sl = 0.0
        self.tp = 0.0
        self.time = ptime
        self.comment = comment
        self.magic = magic
        self.swap = 0.0
        self.profit = 0.0


class SimDeal:
    __slots__ = ("ticket", "magic", "entry", "profit", "time", "volume", "type", "price")

    def __init__(self, ticket, magic, entry, profit, time, volume, ptype, price):
        self.ticket = ticket
        self.magic = magic
        self.entry = entry
        self.profit = profit
        self.time = time
        self.volume = volume
        self.type = ptype
        self.price = price


class SimBroker:
    def __init__(self, market, specs, logger, initial_balance):
        self.market = market
        self.specs = specs
        self.logger = logger
        self.magic = config.MAGIC_NUMBER
        self.shadow = False

        self.balance = float(initial_balance)
        self._positions = []
        self._deals = []
        self._next_ticket = 1
        self.closed_trades = []  # dicts para metricas
        self.blown = False       # True si el broker liquido por stop-out

    # -------------------- specs --------------------
    def point(self):
        return self.specs["point"]

    def digits(self):
        return self.specs["digits"]

    def tick_value(self):
        return self.specs["tick_value"]

    def volume_step(self):
        return self.specs["volume_step"]

    def volume_min(self):
        return self.specs["volume_min"]

    def volume_max(self):
        return self.specs["volume_max"]

    def spread(self):
        return self.specs["spread_points"]

    def bid(self):
        return self.market.current_price

    def ask(self):
        return self.market.current_price + self.specs["spread_points"] * self.specs["point"]

    # -------------------- modelo de dinero --------------------
    def _points(self, ptype, entry, price):
        d = (price - entry) if ptype == mt5.POSITION_TYPE_BUY else (entry - price)
        return d / self.specs["point"]

    def _money(self, ptype, entry, price, volume):
        return self._points(ptype, entry, price) * self.specs["tick_value"] * volume

    def _close_price(self, ptype):
        # cerrar BUY al bid, SELL al ask
        return self.bid() if ptype == mt5.POSITION_TYPE_BUY else self.ask()

    def mark_to_market(self):
        for p in self._positions:
            price = self._close_price(p.type)
            p.price_current = price
            p.profit = self._money(p.type, p.price_open, price, p.volume)

    # -------------------- cuenta --------------------
    def account_balance(self):
        return self.balance

    def account_equity(self):
        floating = sum(self._money(p.type, p.price_open, self._close_price(p.type), p.volume)
                       for p in self._positions)
        return self.balance + floating

    def _used_margin(self):
        cs = self.specs["contract_size"]
        lev = self.specs["leverage"]
        price = self.market.current_price
        return sum(p.volume * cs * price / lev for p in self._positions)

    def margin_free(self):
        return self.account_equity() - self._used_margin()

    def margin_level(self):
        """Nivel de margen en % (equity/margen_usado*100). inf si no hay margen usado."""
        um = self._used_margin()
        if um <= 0:
            return float("inf")
        return self.account_equity() / um * 100.0

    def positions(self):
        return list(self._positions)

    def check_free_margin(self, lots, order_type):
        cs = self.specs["contract_size"]
        lev = self.specs["leverage"]
        price = self.ask() if order_type == mt5.ORDER_TYPE_BUY else self.bid()
        margin_required = lots * cs * price / lev
        return self.margin_free() >= margin_required * 1.1

    # -------------------- ordenes --------------------
    def _new_ticket(self):
        t = self._next_ticket
        self._next_ticket += 1
        return t

    def market_order(self, order_type, lots, comment):
        lots = float(lots)
        ptype = mt5.POSITION_TYPE_BUY if order_type == mt5.ORDER_TYPE_BUY else mt5.POSITION_TYPE_SELL
        price = self.ask() if order_type == mt5.ORDER_TYPE_BUY else self.bid()
        pos = SimPosition(self._new_ticket(), ptype, lots, price,
                          self.market.current_time, comment, self.magic)
        self._positions.append(pos)
        self.balance -= lots * self.specs["commission_per_lot"]  # comision de apertura
        return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, order=pos.ticket)

    def _record_close(self, pos, volume, exit_price, reason):
        gross = self._money(pos.type, pos.price_open, exit_price, volume)
        self.balance += gross
        self.balance -= volume * self.specs["commission_per_lot"]  # comision de cierre
        self._deals.append(SimDeal(self._new_ticket(), self.magic, mt5.DEAL_ENTRY_OUT,
                                   gross, self.market.current_time, volume, pos.type, exit_price))
        self.closed_trades.append({
            "ticket": pos.ticket,
            "side": "BUY" if pos.type == mt5.POSITION_TYPE_BUY else "SELL",
            "volume": volume,
            "open_price": pos.price_open,
            "close_price": exit_price,
            "open_time": pos.time,
            "close_time": self.market.current_time,
            "profit": gross,
            "balance": self.balance,  # balance de la cuenta despues de cerrar este deal
            "open_comment": pos.comment,
            "close_reason": reason,
        })

    def close_position(self, position, comment="Close"):
        exit_price = self._close_price(position.type)
        self._record_close(position, position.volume, exit_price, comment)
        self._positions.remove(position)
        return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE)

    def close_partial(self, position, lots, comment="Partial"):
        lots = min(float(lots), position.volume)
        if lots <= 0:
            return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE)
        exit_price = self._close_price(position.type)
        self._record_close(position, lots, exit_price, comment)
        position.volume -= lots
        if position.volume < self.specs["volume_min"] - 1e-9:
            # remanente por debajo del minimo: cerrar lo que queda
            if position.volume > 1e-9:
                self._record_close(position, position.volume, exit_price, comment + " (resto)")
            self._positions.remove(position)
        return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE)

    def modify_sl(self, position, sl):
        position.sl = float(sl)
        return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE)

    # -------------------- historial / SL --------------------
    def history_deals(self, from_dt, to_dt):
        f = calendar.timegm(from_dt.utctimetuple())
        t = calendar.timegm(to_dt.utctimetuple())
        return [d for d in self._deals if f <= d.time <= t]

    def process_bar_sl(self):
        """Cierra por SL si el rango [low, high] de la barra actual lo toca."""
        hi = self.market.bar_high
        lo = self.market.bar_low
        for p in list(self._positions):
            if p.sl <= 0:
                continue
            hit = ((p.type == mt5.POSITION_TYPE_BUY and lo <= p.sl) or
                   (p.type == mt5.POSITION_TYPE_SELL and hi >= p.sl))
            if hit:
                self._record_close(p, p.volume, p.sl, "SL")
                self._positions.remove(p)

    def close_all_eot(self):
        """Cierre forzado al final del test (End Of Test)."""
        for p in list(self._positions):
            exit_price = self._close_price(p.type)
            self._record_close(p, p.volume, exit_price, "EndOfTest")
        self._positions.clear()

    def liquidate(self, reason="StopOut"):
        """Liquidacion total del broker por stop-out (no es logica de estrategia)."""
        for p in list(self._positions):
            exit_price = self._close_price(p.type)
            self._record_close(p, p.volume, exit_price, reason)
        self._positions.clear()
        self.blown = True
