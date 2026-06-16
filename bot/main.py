"""Entrypoint. Inicializa MT5 + DB y corre el bucle de polling (estilo OnTick).

Control por DB: la fila bot_config con status='ACTIVE' corre el motor;
cualquier otro status pausa (espera). Sin news filter ni PAUSED_AI_OPUS
(port puro MQL5).

Uso (modo consola):  python -m bot.main
Uso (modo TUI):      python -m bot.tui

El cuerpo se parte en setup() / trading_loop() / shutdown() para que la TUI
pueda reutilizar el mismo motor en un hilo aparte sin duplicar logica.
"""

import time

import MetaTrader5 as mt5

from . import config
from .broker import Broker
from .db import Database
from .logger import Logger
from .strategy import SentinelEngine


def setup(logger=None):
    """Inicializa MT5, DB, broker y motor. Devuelve (db, broker, engine, logger)."""
    if not mt5.initialize():
        raise RuntimeError(f"No se pudo inicializar MetaTrader 5: {mt5.last_error()}")

    if not mt5.symbol_select(config.SYMBOL, True):
        print(f"[ALERTA] El simbolo {config.SYMBOL} no pudo ser seleccionado en el broker.")

    db = Database()
    db.connect()  # NO trunca bot_config (a diferencia del intento previo)

    if logger is None:
        logger = Logger(enable_file=True, file_name="Gold_HyperGrinder_v20", symbol=config.SYMBOL)

    logger.sink = db.insert_log  # persiste cada write() en bot_logs

    broker = Broker(logger, shadow=config.SHADOW_MODE)
    engine = SentinelEngine(broker, logger)
    engine.init_history_cursor()

    logger.write("SYSTEM", "HYPER GRINDER v20.0 (Python) INICIADO.",
                 balance=broker.account_balance())
    if config.SHADOW_MODE:
        logger.write("SYSTEM", "MODO SOMBRA activo: no se enviaran ordenes reales; solo se loguean decisiones.")

    return db, broker, engine, logger


def trading_loop(db, engine, logger, stop_event=None):
    """Bucle principal. Corre hasta stop_event (TUI) o KeyboardInterrupt (consola)."""
    last_status = None

    def running():
        return stop_event is None or not stop_event.is_set()

    while running():
        try:
            cfg, status = db.load_config()

            if status != "ACTIVE":
                if status != last_status:
                    logger.write("SYSTEM", f"Estado leido: '{status}'. Esperando ACTIVE...")
                    last_status = status
                time.sleep(2)
                continue

            if last_status != "ACTIVE":
                logger.write("SYSTEM", "Estado ACTIVE: motor corriendo.")
                last_status = "ACTIVE"

            engine.cfg = cfg
            engine.on_tick()
            time.sleep(config.LOOP_SLEEP)

        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001  (robustez del bucle, igual que el ref)
            logger.write("ERROR", f"Error en el bucle principal: {e}")
            time.sleep(2)


def shutdown(db):
    db.close()
    mt5.shutdown()


def run():
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
