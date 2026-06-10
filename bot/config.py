"""Constantes de conexion y de mercado.

Los ~60 parametros de estrategia NO viven aqui: se leen desde la tabla
bot_config (PostgreSQL). Aqui solo van las constantes de infraestructura
(conexion DB, simbolo, magic, timeframes) y la identidad del bot.
"""

import os

import MetaTrader5 as mt5

# Carga .env si existe (sin dependencia dura de python-dotenv).
def _load_dotenv():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

# ==================================================================
# CONEXION POSTGRESQL (nativo en Windows) - secretos via entorno/.env
# ==================================================================
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_NAME = os.environ.get("DB_NAME", "sentinel_local")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASS = os.environ.get("DB_PASS", "")  # <-- definir en .env (no se commitea)
DB_PORT = os.environ.get("DB_PORT", "5432")

# ==================================================================
# MERCADO / IDENTIDAD
# ==================================================================
SYMBOL = "XAUUSD+"   # <-- si tu broker usa sufijo distinto (XAUUSD.v), cambialo
MAGIC_NUMBER = 100100
ADMIN_USER_UUID = "81118671-d5ba-4d49-9fb3-4499b54a3d93"

# Usuario/simbolo que identifican la fila de bot_config a cargar
USER_ID = ADMIN_USER_UUID

TIMEFRAME_CORE = mt5.TIMEFRAME_M15
TIMEFRAME_GRINDER = mt5.TIMEFRAME_M5
TIMEFRAME_STRUCT = mt5.TIMEFRAME_H4

# Frecuencia del bucle principal (segundos). 1 = comportamiento tipo OnTick.
LOOP_SLEEP = 1

# Modo sombra: si True, on_tick loguea decisiones pero NO envia ordenes.
SHADOW_MODE = False
