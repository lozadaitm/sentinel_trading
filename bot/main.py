"""Entrypoint. Inicializa MT5 + DB y corre el bucle de polling (estilo OnTick).

Control por DB: la fila bot_config con status='ACTIVE' corre el motor;
cualquier otro status pausa (espera). Sin news filter ni PAUSED_AI_OPUS
(port puro MQL5).

Uso (modo consola):  python -m bot.main
Uso (modo TUI):      python -m bot.tui

El cuerpo se parte en setup() / trading_loop() / shutdown() para que la TUI
pueda reutilizar el mismo motor en un hilo aparte sin duplicar logica.
"""

import ctypes
import os
import time

import MetaTrader5 as mt5

from . import config
from .broker import Broker
from .db import Database
from .logger import Logger
from .strategy import SentinelEngine


def disable_quickedit():
    """Desactiva QuickEdit Mode de la consola de Windows.

    Con QuickEdit ON, hacer click o seleccionar texto en la consola suspende
    cualquier hilo que escriba a stdout hasta presionar Enter/Esc. Eso congela
    la UI de la TUI (y, si el worker llega a imprimir, tambien el trading).
    Lo apagamos al arrancar. No-op fuera de Windows o si falla.
    """
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        STD_INPUT_HANDLE = -10
        ENABLE_QUICK_EDIT = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        # Hay que setear EXTENDED_FLAGS para que limpiar QUICK_EDIT surta efecto.
        new_mode = (mode.value & ~ENABLE_QUICK_EDIT) | ENABLE_EXTENDED_FLAGS
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:  # noqa: BLE001  (nunca debe tumbar el arranque)
        pass


def _mt5_init_kwargs():
    """Argumentos de mt5.initialize() para atacar el terminal de ESTA instancia.

    Vacio => terminal por defecto (comportamiento legacy). Con MT5_LOGIN se
    conecta a la cuenta Vantage concreta; con MT5_PATH se abre ese terminal64.exe.
    """
    kw = {}
    if config.MT5_PATH:
        kw["path"] = config.MT5_PATH
    if config.MT5_LOGIN:
        kw["login"] = int(config.MT5_LOGIN)
        kw["server"] = config.MT5_SERVER
        kw["password"] = config.MT5_PASSWORD
    return kw


def setup(logger=None):
    """Inicializa MT5, DB, broker y motor. Devuelve (db, broker, engine, logger)."""
    if not mt5.initialize(**_mt5_init_kwargs()):
        raise RuntimeError(f"No se pudo inicializar MetaTrader 5: {mt5.last_error()}")

    if not mt5.symbol_select(config.SYMBOL, True):
        print(f"[ALERTA] El simbolo {config.SYMBOL} no pudo ser seleccionado en el broker.")

    db = Database()
    db.connect()  # conecta a Supabase + arranca el flusher de logs

    if logger is None:
        logger = Logger(enable_file=True, file_name="Gold_HyperGrinder_v20", symbol=config.SYMBOL)

    logger.add_sink(db.log_sink)  # encola cada write() -> bot_logs (no bloquea; sin pisar otros sinks)

    broker = Broker(logger, shadow=config.SHADOW_MODE)
    engine = SentinelEngine(broker, logger)
    engine.init_history_cursor()
    engine.user_email = db.get_user_email()  # una vez al arrancar (para identificar la instancia)

    who = engine.user_email or config.USER_ID
    logger.write("SYSTEM", f"HYPER GRINDER v20.0 (Python) INICIADO. user={who} symbol={config.SYMBOL}",
                 balance=broker.account_balance())
    if config.SHADOW_MODE:
        logger.write("SYSTEM", "MODO SOMBRA activo: no se enviaran ordenes reales; solo se loguean decisiones.")

    return db, broker, engine, logger


def _bot_status(engine):
    """Estado que reporta el bot a bot_instances (observacional, para la UI)."""
    if not engine.close_only:
        return "RUNNING"
    try:
        has_pos = len(engine.b.positions()) > 0
    except Exception:  # noqa: BLE001
        has_pos = True  # ante duda, asumimos que aun gestiona
    return "CLOSE_ONLY" if has_pos else "FLAT"


def trading_loop(db, engine, logger, stop_event=None):
    """Bucle principal. Corre hasta stop_event (TUI) o KeyboardInterrupt (consola).

    Control por bot_instances.is_active (interruptor del usuario):
      True  -> opera normal (close_only=False).
      False -> close-only: NO abre nada nuevo pero SIGUE gestionando/cerrando
               lo abierto (por eso on_tick corre siempre, nunca se pausa duro).
    """
    last_reported = None
    last_refresh = 0.0
    last_report = 0.0
    cfg = {}

    def running():
        return stop_event is None or not stop_event.is_set()

    while running():
        try:
            now = time.time()

            # Refresco de control (config + instancia) desacoplado del on_tick.
            if now - last_refresh >= config.CONTROL_REFRESH:
                cfg, _ = db.load_config()
                inst = db.get_instance()
                engine.is_active = bool(inst.get("is_active", False))
                engine.close_only = not engine.is_active
                engine.cfg = cfg
                last_refresh = now

            if not cfg:
                # Sin config activa ni cache: nada que gestionar aun.
                time.sleep(2)
                continue

            engine.on_tick()

            # Heartbeat + estado observacional.
            if now - last_report >= config.HEARTBEAT_INTERVAL:
                status = _bot_status(engine)
                db.report(status)
                if status != last_reported:
                    logger.write("SYSTEM", f"Estado del bot: {status}.")
                    last_reported = status
                last_report = now

            time.sleep(config.LOOP_SLEEP)

        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001  (robustez del bucle, igual que el ref)
            logger.write("ERROR", f"Error en el bucle principal: {e}")
            time.sleep(2)


def shutdown(db):
    db.report("STOPPED")  # best-effort: marca offline antes de cerrar el flusher
    db.close()
    mt5.shutdown()


def run():
    disable_quickedit()
    db, broker, engine, logger = setup()
    try:
        trading_loop(db, engine, logger)
    except KeyboardInterrupt:
        pass
    finally:
        logger.write("SYSTEM", "Detencion manual solicitada.")
        shutdown(db)


if __name__ == "__main__":
    run()
