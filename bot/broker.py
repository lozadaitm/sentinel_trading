"""Wrapper fino sobre MetaTrader5.

Centraliza precios, info de simbolo, posiciones (filtradas por magic),
envio/cierre/modificacion de ordenes y calculo de margen. Respeta
SHADOW_MODE: si esta activo, las operaciones de escritura se loguean
pero NO se envian al broker.
"""

import MetaTrader5 as mt5

from . import config


class Broker:
    def __init__(self, logger, shadow=False):
        self.symbol = config.SYMBOL
        self.magic = config.MAGIC_NUMBER
        self.logger = logger
        self.shadow = shadow

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
        pos = mt5.positions_get(symbol=self.symbol)
        if pos is None:
            return []
        return [p for p in pos if p.magic == self.magic]

    # --------------------------------------------------------------
    # Margen
    # --------------------------------------------------------------
    def check_free_margin(self, lots, order_type):
        """Replica CheckFreeMargin (MQL5 143-148)."""
        price = self.ask() if order_type == mt5.ORDER_TYPE_BUY else self.bid()
        margin_required = mt5.order_calc_margin(order_type, self.symbol, lots, price)
        if margin_required is None:
            return False
        return self.margin_free() >= margin_required * 1.1

    # --------------------------------------------------------------
    # Ordenes
    # --------------------------------------------------------------
    def market_order(self, order_type, lots, comment):
        if self.shadow:
            self.logger.write("SHADOW", f"ORDER {('BUY' if order_type==mt5.ORDER_TYPE_BUY else 'SELL')} {comment}", 0, lots)
            return None
        price = self.ask() if order_type == mt5.ORDER_TYPE_BUY else self.bid()
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(lots),
            "type": order_type,
            "price": price,
            "deviation": 50,
            "magic": self.magic,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        return mt5.order_send(request)

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
