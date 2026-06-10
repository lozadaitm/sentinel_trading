"""Entrypoint. Inicializa MT5 + DB y corre el bucle de polling (estilo OnTick).

Control por DB: la fila bot_config con status='ACTIVE' corre el motor;
cualquier otro status pausa (espera). Sin news filter ni PAUSED_AI_OPUS
(port puro MQL5).

Uso:  python -m bot.main
"""

import time

import MetaTrader5 as mt5

from . import config
from .broker import Broker
from .db import Database
from .logger import Logger
from .strategy import SentinelEngine


def run():
    if not mt5.initialize():
        print(f"[ERROR] No se pudo inicializar MetaTrader 5: {mt5.last_error()}")
        return

    if not mt5.symbol_select(config.SYMBOL, True):
        print(f"[ALERTA] El simbolo {config.SYMBOL} no pudo ser seleccionado en el broker.")

    db = Database()
    db.connect()  # NO trunca bot_config (a diferencia del intento previo)

    logger = Logger(enable_file=True, file_name="Gold_HyperGrinder_v20", symbol=config.SYMBOL)
    broker = Broker(logger, shadow=config.SHADOW_MODE)
    engine = SentinelEngine(broker, logger)
    engine.init_history_cursor()

    logger.write("SYSTEM", "HYPER GRINDER v20.0 (Python) INICIADO.",
                 balance=broker.account_balance())
    if config.SHADOW_MODE:
        print("[MODO SOMBRA] No se enviaran ordenes reales; solo se loguean decisiones.")

    while True:
        try:
            cfg, status = db.load_config()

            if status != "ACTIVE":
                print(f"[AUDITORIA] Estado leido: '{status}'. Esperando ACTIVE...")
                time.sleep(5)
                continue

            engine.cfg = cfg
            engine.on_tick()
            time.sleep(config.LOOP_SLEEP)

        except KeyboardInterrupt:
            print("\n[SISTEMA] Detencion manual solicitada.")
            break
        except Exception as e:  # noqa: BLE001  (robustez del bucle, igual que el ref)
            print(f"[ERROR] Error en el bucle principal: {e}")
            time.sleep(2)

    db.close()
    mt5.shutdown()


if __name__ == "__main__":
    run()
