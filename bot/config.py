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
# CONEXION SUPABASE (cloud) - secretos via entorno/.env por instancia.
# El bot usa la service-role key (bypassa RLS); filtra por USER_ID.
# ==================================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# ==================================================================
# CONEXION POSTGRESQL LOCAL (legacy) - solo scripts de migracion.
# El runtime del bot ya no lo usa; se conserva para migrar datos.
# ==================================================================
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_NAME = os.environ.get("DB_NAME", "sentinel_local")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASS = os.environ.get("DB_PASS", "")  # <-- definir en .env (no se commitea)
DB_PORT = os.environ.get("DB_PORT", "5432")

# ==================================================================
# MERCADO / IDENTIDAD  (por-instancia via entorno; default = admin legacy)
# ==================================================================
SYMBOL = os.environ.get("SYMBOL", "XAUUSD+")   # sufijo del broker (XAUUSD.v, etc.)
MAGIC_NUMBER = int(os.environ.get("MAGIC_NUMBER", "100100"))
ADMIN_USER_UUID = "81118671-d5ba-4d49-9fb3-4499b54a3d93"

# ==================================================================
# MULTI-BOT SOBRE UNA MISMA CUENTA
# Un usuario puede correr varios motores (M15 Sentinel + M5 Grinder) contra
# la MISMA cuenta MT5. Se distinguen por:
#   - BOT_ID  -> discriminador de las filas en Supabase (bot_config, bot_state,
#                bot_instances, bot_positions, bot_logs).
#   - MAGIC   -> discriminador de las posiciones en el broker. Es lo que permite
#                que cada proceso vea SOLO lo suyo (Broker.positions) y a la vez
#                pueda leer la cesta del vecino (Broker.positions_of_magic).
# ==================================================================
BOT_ID = os.environ.get("BOT_ID", "m15")

MAGIC_M15 = int(os.environ.get("MAGIC_M15", "100100"))
MAGIC_M5 = int(os.environ.get("MAGIC_M5", "100200"))

ALL_MAGICS = (MAGIC_M15, MAGIC_M5)
# Magics de los OTROS bots de la cuenta: para calcular la reserva de proteccion
# sin necesidad de IPC ni de pasar por la DB. Ver bot/budget.py.
PEER_MAGICS = tuple(m for m in ALL_MAGICS if m != MAGIC_NUMBER)

# Magic que le corresponde a cada BOT_ID. Lo verifica validate_identity().
EXPECTED_MAGIC = {"m15": MAGIC_M15, "m5": MAGIC_M5}


def validate_identity():
    """Aborta el arranque si BOT_ID y MAGIC_NUMBER no se corresponden.

    El fallo mas facil de cometer al montar la segunda instancia es copiar el
    .env del M15 y cambiar solo BOT_ID, dejando MAGIC_NUMBER=100100. Los dos
    procesos compartirian magic y cada motor veria las posiciones del otro como
    propias: el Healer amputaria scalps del M5, core_count contaria de mas, el
    Hedge Lock cubriria el volumen equivocado y el Sentinel encontraria SLs que
    el nunca puso. Es silencioso y destructivo, asi que se corta en el arranque.

    Devuelve la lista de avisos no fatales (p.ej. un BOT_ID desconocido, que es
    valido pero impide comprobar nada).
    """
    warnings = []
    expected = EXPECTED_MAGIC.get(BOT_ID)
    if expected is None:
        warnings.append(
            f"BOT_ID='{BOT_ID}' no esta en EXPECTED_MAGIC: no se puede validar el magic."
        )
        return warnings

    if MAGIC_NUMBER != expected:
        raise SystemExit(
            f"\n[ABORTADO] Identidad de la instancia incoherente.\n"
            f"  BOT_ID       = {BOT_ID}\n"
            f"  MAGIC_NUMBER = {MAGIC_NUMBER}\n"
            f"  esperado     = {expected}\n\n"
            f"Cada motor necesita SU PROPIO magic o los dos procesos se veran las\n"
            f"posiciones mutuamente y corromperan la cesta. Corrige MAGIC_NUMBER\n"
            f"en el .env de esta instancia (ver instances/example-m5.env).\n"
        )

    if len(set(ALL_MAGICS)) != len(ALL_MAGICS):
        raise SystemExit(
            f"\n[ABORTADO] MAGIC_M15 y MAGIC_M5 son el mismo numero ({MAGIC_M15}).\n"
            f"Deben ser distintos en TODOS los .env del mismo usuario.\n"
        )
    return warnings

# Usuario que identifica la fila de bot_config/bot_instances a cargar.
# = auth.users.id en Supabase. Se pasa por .env de la instancia.
USER_ID = os.environ.get("USER_ID", ADMIN_USER_UUID)

# Persistencia del CycleLedger (identidad por rol de cada posicion del ciclo;
# ver bot/ledger.py). Un archivo por instancia (bot + usuario): sobrevive
# restarts sin depender de red y no colisiona entre procesos de la misma PC.
LEDGER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "instances",
    f"cycle_ledger_{BOT_ID}_{USER_ID}.json")

# ==================================================================
# ATTACH A METATRADER 5  (por-instancia; vacio = terminal por defecto)
# Permite que cada proceso ataque el terminal/cuenta Vantage correcta.
# ==================================================================
MT5_PATH = os.environ.get("MT5_PATH", "")       # ruta al terminal64.exe de esa instancia
MT5_LOGIN = os.environ.get("MT5_LOGIN", "")     # login numerico de la cuenta
MT5_SERVER = os.environ.get("MT5_SERVER", "")   # servidor del broker
MT5_PASSWORD = os.environ.get("MT5_PASSWORD", "")
# true = terminal en modo /portable (clones creados por scripts/provision.py:
# los datos viven dentro de la carpeta del clon, no en AppData).
MT5_PORTABLE = os.environ.get("MT5_PORTABLE", "").lower() in ("1", "true", "yes")

# ==================================================================
# RESEND (bot/notify.py) - notificaciones por correo al usuario.
# Sin RESEND_API_KEY el modulo queda inerte (el evento igual queda en DB).
# El from por defecto (onboarding@resend.dev) solo entrega al dueño de la
# cuenta Resend; para usuarios reales, verificar dominio y setear RESEND_FROM.
# ==================================================================
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "Sentinel <onboarding@resend.dev>")
# URL publica del dashboard (CTA de los correos).
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://sentinel-trading-webapp.vercel.app/")

# ==================================================================
# NEWSGUARD (bot/news.py)  - snapshot del calendario economico
# Lo escribe scripts/CalendarExporter.mq5 en la carpeta COMUN de
# MetaQuotes: una sola ruta que sirve a todas las instancias de esta PC.
# ==================================================================
NEWS_CALENDAR_PATH = os.environ.get(
    "NEWS_CALENDAR_PATH",
    os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal",
                 "Common", "Files", "news_calendar.json"))

TIMEFRAME_CORE = mt5.TIMEFRAME_M15
TIMEFRAME_M5 = mt5.TIMEFRAME_M5
TIMEFRAME_STRUCT = mt5.TIMEFRAME_H4

# Frecuencia del bucle principal (segundos). 1 = comportamiento tipo OnTick.
LOOP_SLEEP = 1

# Cada cuantos segundos releer config + instancia (bot_config/bot_instances)
# desde Supabase. Desacopla el control de la frecuencia del on_tick para no
# pegarle a la REST API cada segundo.
CONTROL_REFRESH = 3

# Cada cuantos segundos escribir heartbeat + bot_status en bot_instances.
HEARTBEAT_INTERVAL = 15

# Modo sombra: si True, on_tick loguea decisiones pero NO envia ordenes.
SHADOW_MODE = False
