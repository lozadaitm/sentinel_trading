"""Configuracion del backtest: specs del instrumento simulado + params.

Los ~60 parametros de estrategia se toman de DEFAULTS (bot.strategy) y se pueden
sobrescribir con make_params(clave=valor). En vivo vienen de la tabla bot_config;
aqui se pasan directos para no depender de PostgreSQL.
"""

import copy

from bot import config
from bot.strategy import DEFAULTS

# ------------------------------------------------------------------
# Specs del instrumento simulado (XAUUSD+). Ajusta a tu broker real.
# ------------------------------------------------------------------
SYMBOL_SPECS = {
    "point": 0.01,
    "digits": 2,
    "tick_value": 1.0,          # USD por punto (0.01) por 1.0 lote
    "contract_size": 100,       # onzas por lote
    "volume_step": 0.01,
    "volume_min": 0.01,
    "volume_max": 50.0,
    "leverage": 100,
    "spread_points": 20,        # spread fijo simulado (en puntos)
    "commission_per_lot": 6.0,  # por lado (round-trip = x2)
    "stop_out_level": 50.0,     # % nivel de margen: por debajo, el broker liquida (0 = off)
}

INITIAL_BALANCE = 5000.0

# Plantillas de nombre de archivo por timeframe dentro del data dir.
DATA_FILES = {
    config.TIMEFRAME_CORE:    "{symbol}_M15.csv",
    config.TIMEFRAME_M5: "{symbol}_M5.csv",
    config.TIMEFRAME_STRUCT:  "{symbol}_H4.csv",
}


def make_params(**overrides):
    """Copia de DEFAULTS con overrides opcionales."""
    p = copy.deepcopy(DEFAULTS)
    p.update(overrides)
    return p
