"""BacktestEngine: SentinelEngine real con el feed de datos sobrescrito.

Solo se reemplazan los DOS seams de datos (_fetch_tick y _rates). Toda la logica
de trading (estados, healer, banking, rescate, trailing) es heredada sin
cambios -> el backtest prueba el mismo codigo que el bot en vivo.
"""

from types import SimpleNamespace

from bot.strategy import SentinelEngine


class BacktestEngine(SentinelEngine):
    def __init__(self, broker, logger, market):
        super().__init__(broker, logger)
        self.market = market

    def _fetch_tick(self):
        # _compute_buffers solo usa tick.time; el precio se lee via broker.bid/ask.
        return SimpleNamespace(time=self.market.current_time)

    def _rates(self, timeframe, count=150, min_bars=60):
        df = self.market.rates(timeframe, count)
        if df is None or len(df) < min_bars:
            return None
        return df
