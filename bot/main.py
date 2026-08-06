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
import datetime
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


def _log_file_name():
    """Nombre del CSV local. El M15 conserva el historico; los demas bots
    escriben su propio archivo para no mezclar dos motores en un mismo log."""
    if config.BOT_ID == "m15":
        return "Gold_HyperGrinder_v20"
    return f"Gold_{config.BOT_ID.upper()}"


def setup(logger=None, engine_cls=None):
    """Inicializa MT5, DB, broker y motor. Devuelve (db, broker, engine, logger).

    `engine_cls` permite reusar todo el arranque con otro motor (M5Engine) sin
    duplicar la conexion, la DB ni el bucle. Por defecto, el Sentinel M15.
    """
    if engine_cls is None:
        engine_cls = SentinelEngine

    # Antes de tocar MT5 o la DB: si BOT_ID y MAGIC_NUMBER no se corresponden,
    # los dos motores se verian las posiciones mutuamente. Aborta el arranque.
    identity_warnings = config.validate_identity()

    if not mt5.initialize(**_mt5_init_kwargs()):
        raise RuntimeError(f"No se pudo inicializar MetaTrader 5: {mt5.last_error()}")

    if not mt5.symbol_select(config.SYMBOL, True):
        print(f"[ALERTA] El simbolo {config.SYMBOL} no pudo ser seleccionado en el broker.")

    db = Database()
    db.connect()  # conecta a Supabase + arranca el flusher de logs

    if logger is None:
        logger = Logger(enable_file=True, file_name=_log_file_name(), symbol=config.SYMBOL)

    logger.add_sink(db.log_sink)  # encola cada write() -> bot_logs (no bloquea; sin pisar otros sinks)

    broker = Broker(logger, shadow=config.SHADOW_MODE)
    engine = engine_cls(broker, logger)
    engine.init_history_cursor()
    engine.user_email = db.get_user_email()  # una vez al arrancar (para identificar la instancia)

    # Saldo inicial para el dashboard (bot_state): reusa el ya persistido si
    # existe (sobrevive a restarts), si no lo fija al balance actual.
    engine.initial_balance = db.get_state_initial_balance()
    if engine.initial_balance is None:
        engine.initial_balance = broker.account_balance()

    # Reconciliacion de bot_positions: si alguna quedo marcada OPEN pero ya no
    # existe en MT5 (se cerro con el bot apagado), se cierra ahora con el
    # historial de deals. Evita posiciones "fantasma" en el dashboard.
    try:
        live_tickets = {p.ticket for p in broker.positions()}
        stale_tickets = db.get_open_tickets() - live_tickets
        if stale_tickets:
            db.upsert_positions([_closed_position_row(broker, t) for t in stale_tickets])
    except Exception:  # noqa: BLE001
        pass

    who = engine.user_email or config.USER_ID
    logger.write("SYSTEM",
                 f"{engine_cls.__name__} INICIADO. bot_id={config.BOT_ID} magic={config.MAGIC_NUMBER} "
                 f"user={who} symbol={config.SYMBOL}",
                 balance=broker.account_balance())
    for w in identity_warnings:
        logger.write("SYSTEM", f"[AVISO] {w}")

    # Aviso temprano: sin AutoTrading el motor gestiona y calcula, pero toda
    # apertura sera rechazada por el terminal. Mejor verlo al arrancar que
    # descubrirlo en el primer rechazo.
    allowed, motivo = broker.trade_allowed()
    if not allowed:
        logger.write("ERROR", f"APERTURAS BLOQUEADAS: {motivo}",
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


def _epoch_to_iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).isoformat()


def _open_position_row(p):
    """Fila OPEN para bot_positions a partir de una posicion live de MT5."""
    return {
        "ticket": p.ticket,
        "symbol": config.SYMBOL,
        "position_type": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
        "op_type": Broker._open_log_type(p.comment),
        "comment": p.comment,
        "lots": p.volume,
        "open_price": p.price_open,
        "open_time": _epoch_to_iso(p.time),
        "sl": p.sl,
        "tp": p.tp,
        "current_price": p.price_current,
        "profit": p.profit,
        "swap": p.swap,
        "status": "OPEN",
    }


def _closed_position_row(broker, ticket):
    """Fila de cierre para bot_positions, reconstruida del historial de deals
    de la posicion (P&L total incl. cierres parciales previos del Healer/Unwind).

    Si el historial no trae nada (broker sin retencion, etc.) devuelve solo el
    cambio de status: el upsert por conflicto no pisa los datos ya guardados.
    """
    row = {"ticket": ticket, "status": "CLOSED"}
    total_profit = 0.0
    total_swap = 0.0
    close_price = None
    last_time = -1
    for d in broker.history_deals_for_position(ticket):
        if d.entry == mt5.DEAL_ENTRY_IN:
            continue
        total_profit += d.profit
        total_swap += d.swap
        if d.time > last_time:
            last_time = d.time
            close_price = d.price
    if close_price is not None:
        row["close_price"] = close_price
        row["close_time"] = _epoch_to_iso(last_time)
        row["profit"] = total_profit
        row["swap"] = total_swap
    return row


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
    known_open_tickets = set()  # ultimo set de tickets OPEN reportado a bot_positions

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

                # Snapshot en vivo para el dashboard (bot_state). Best-effort;
                # nunca debe tumbar el bucle si Supabase/MT5 fallan.
                try:
                    info = mt5.account_info()
                    if info:
                        db.report_state(
                            symbol=config.SYMBOL,
                            balance=info.balance,
                            equity=info.equity,
                            margin_used=info.margin,
                            margin_free=info.margin_free,
                            floating_pnl=info.equity - info.balance,
                            open_positions=len(engine.b.positions()),
                            initial_balance=engine.initial_balance,
                        )
                except Exception:  # noqa: BLE001
                    pass

                # Posiciones para el dashboard (bot_positions): upsert de las
                # abiertas + cierre de las que desaparecieron desde el ultimo
                # heartbeat. Best-effort; nunca debe tumbar el bucle.
                try:
                    live_positions = engine.b.positions()
                    current_tickets = {p.ticket for p in live_positions}
                    closed_tickets = known_open_tickets - current_tickets
                    rows = [_open_position_row(p) for p in live_positions]
                    rows += [_closed_position_row(engine.b, t) for t in closed_tickets]
                    db.upsert_positions(rows)
                    known_open_tickets = current_tickets
                except Exception:  # noqa: BLE001
                    pass

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


def run(engine_cls=None):
    disable_quickedit()
    db, broker, engine, logger = setup(engine_cls=engine_cls)
    try:
        trading_loop(db, engine, logger)
    except KeyboardInterrupt:
        pass
    finally:
        logger.write("SYSTEM", "Detencion manual solicitada.")
        shutdown(db)


if __name__ == "__main__":
    run()
