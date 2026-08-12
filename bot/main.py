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

from . import config, notify
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

    if not mt5.initialize(**config.mt5_init_kwargs()):
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

    # Saldo inicial (base del objetivo de ganancia): reusa el ya persistido si
    # existe (sobrevive a restarts), si no lo fija al balance actual. El ancla
    # de flujos (last_flow_ticket) evita re-contar depositos/retiros ya
    # incorporados a la base; sin ancla previa se parte del ultimo deal de
    # flujo existente (el balance actual ya los refleja todos).
    (engine.initial_balance, engine.last_flow_ticket,
     engine.baseline_applied_at) = db.get_state_flow_info()
    if engine.initial_balance is None:
        engine.initial_balance = broker.account_balance()
    if engine.last_flow_ticket is None:
        engine.last_flow_ticket = max(
            (d.ticket for d in broker.capital_flows(0)), default=0)

    # Backfill de velas para la grafica del dashboard + poda de retencion.
    if config.BOT_ID == "m15":
        _publish_candles(db, CANDLES_BACKFILL)
        db.prune_candles(days=CANDLES_KEEP_DAYS)

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


# Velas para la grafica del dashboard (bot_candles). Solo las publica el
# proceso m15: una unica fuente por cuenta (los dos motores ven el mismo feed).
CANDLES_BACKFILL = 400   # ~4 dias de M15 al arrancar
CANDLES_KEEP_DAYS = 30   # retencion en la tabla
CANDLES_HEARTBEAT = 2    # la vela en formacion + la previa, por heartbeat


def _candle_rows(rates):
    """Filas para bot_candles desde copy_rates_from_pos. `ts` conserva el
    mismo criterio que bot_positions.open_time (hora del servidor como UTC),
    asi la grafica alinea velas y marcadores sin corregir zonas horarias."""
    return [
        {
            "symbol": config.SYMBOL,
            "timeframe": "M15",
            "ts": _epoch_to_iso(int(r["time"])),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
        }
        for r in (rates if rates is not None else [])
    ]


def _publish_candles(db, count):
    """Upsert de las ultimas `count` velas M15. Best-effort, solo m15."""
    if config.BOT_ID != "m15":
        return
    try:
        rates = mt5.copy_rates_from_pos(config.SYMBOL, config.TIMEFRAME_CORE, 0, count)
        db.upsert_candles(_candle_rows(rates))
    except Exception:  # noqa: BLE001
        pass


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


def _reconcile_capital_flows(engine, logger):
    """Incorpora a la base los depositos/retiros posteriores al ancla.

    La ganancia medida debe ser SOLO la del trading:
        profit = equity - (inicial + flujos externos netos)
    Un deposito sube la base (la ganancia no salta con dinero fresco) y un
    retiro la baja (retirar no hunde el % del objetivo). El ancla por ticket
    (bot_state.last_flow_ticket) evita el doble conteo entre heartbeats y
    restarts; la persistencia va en el mismo report_state del heartbeat.
    """
    flows = engine.b.capital_flows(engine.last_flow_ticket)
    if not flows:
        return
    net = sum(d.profit for d in flows)
    engine.initial_balance += net
    engine.last_flow_ticket = max(d.ticket for d in flows)
    logger.write("SYSTEM",
                 f"Flujo de capital detectado ({len(flows)} mov., neto {net:+.2f}): "
                 f"base del objetivo ajustada a {engine.initial_balance:.2f}.",
                 balance=engine.b.account_balance())


def _apply_baseline_reset(engine, logger, acct, info):
    """Reinicio del P&L por ciclo de meta (account_settings.baseline_reset_at).

    Lo estampa el dashboard al reactivar/re-armar tras un objetivo alcanzado:
    la ganancia del ciclo NUEVO se mide desde el monto presente de la cuenta
    (post-retiro si lo hubo; equity para incluir el flotante). Se aplica UNA
    vez por sello (bot_state.baseline_applied_at) y se adelanta el ancla de
    flujos: los depositos/retiros previos ya estan reflejados en la base nueva.
    """
    reset_at = acct.get("baseline_reset_at")
    if not reset_at or reset_at == engine.baseline_applied_at:
        return
    engine.initial_balance = float(info.equity)
    engine.last_flow_ticket = max(
        (d.ticket for d in engine.b.capital_flows(0)),
        default=engine.last_flow_ticket or 0)
    engine.baseline_applied_at = reset_at
    logger.write("SYSTEM",
                 f"BASE DEL P&L reiniciada a {info.equity:.2f} (ciclo nuevo tras "
                 f"objetivo alcanzado / retiro).",
                 balance=info.balance)


def _check_profit_target(db, engine, logger, info, acct):
    """Objetivo de ganancia de la CUENTA (account_settings.profit_target_pct).

    La ganancia se mide en EQUITY (incluye flotante) contra la base: el
    initial_balance auto-capturado de MT5 en la primera corrida de la
    instancia (bot_state; la columna account_settings.initial_deposit quedo
    sin uso por decision de producto). Al alcanzarla:
      1. claim atomico de target_reached_at (solo un motor gana -> un email).
      2. is_active=false para TODAS las instancias del usuario (close-only:
         se deja de abrir y se gestiona lo abierto hasta quedar plano; el
         usuario decide en el dashboard si espera o fuerza el cierre).
      3. Email de notificacion (best-effort, hilo aparte).
    """
    target = acct.get("profit_target_pct")
    if not target or acct.get("target_reached_at"):
        return
    base = engine.initial_balance
    if not base or base <= 0:
        return
    profit_pct = (info.equity - base) / base * 100.0
    if profit_pct < float(target):
        return
    if not db.claim_profit_target():
        return  # el otro motor ya reclamo el evento (o hipo de red): no duplicar
    db.deactivate_all_instances()
    engine.is_active = False
    engine.close_only = True
    logger.write("TARGET",
                 f"OBJETIVO DE GANANCIA alcanzado: +{profit_pct:.2f}% >= {float(target):g}%. "
                 f"Equity {info.equity:.2f} / base {base:.2f}. Bots del usuario en close-only.",
                 balance=info.balance)
    mail_kwargs = dict(profit_pct=profit_pct, target_pct=float(target),
                       equity=info.equity, base=base, symbol=config.SYMBOL)
    notify.send_async(
        engine.user_email,
        "Sentinel: objetivo de ganancia alcanzado",
        notify.profit_target_body(**mail_kwargs),
        logger,
        html=notify.profit_target_html(**mail_kwargs),
    )


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

                # Cierre forzado pedido desde el dashboard: el usuario asume el
                # flotante actual. El comando SOLO se consume (force_close=false
                # + is_active=false en la DB) cuando la instancia queda plana:
                # si el terminal rechaza ordenes (AutoTrading off, requote...)
                # close_all devuelve False y se reintenta en el proximo refresh
                # (~3 s) hasta lograrlo, logueando el retcode de cada rechazo.
                if inst.get("force_close"):
                    logger.write("SYSTEM", "CIERRE FORZADO solicitado por el usuario: "
                                           "cerrando todas las posiciones de esta instancia.")
                    allowed, motivo = engine.b.trade_allowed()
                    if not allowed:
                        logger.write("ERROR", f"CIERRE FORZADO bloqueado por el terminal: {motivo}")
                    if engine.close_all("Cierre forzado por el usuario"):
                        db.clear_force_close()
                    else:
                        logger.write("ERROR", "Cierre forzado INCOMPLETO: quedan posiciones "
                                              "abiertas; se reintenta en ~3 s.")
                    engine.is_active = False
                    engine.close_only = True

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
                # Gate de comision: con una comision de retiro PENDIENTE la
                # instancia no opera. Si el usuario (o cualquiera) pone
                # is_active=true en la DB con deuda viva, el bot lo revierte a
                # close-only. El desbloqueo llega solo cuando el pago USDT se
                # verifica on-chain (status=PAID) desde el dashboard.
                try:
                    if engine.is_active and db.has_pending_commission():
                        db.deactivate_all_instances()
                        engine.is_active = False
                        engine.close_only = True
                        logger.write("SYSTEM", "Comision de retiro PENDIENTE: instancia en "
                                               "solo-cierre hasta verificar el pago en el dashboard.")
                except Exception:  # noqa: BLE001
                    pass

                try:
                    _reconcile_capital_flows(engine, logger)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    info = mt5.account_info()
                    if info:
                        # account_settings se lee UNA vez por heartbeat y
                        # alimenta el reinicio de base y el chequeo de meta.
                        acct = db.get_account_settings()
                        _apply_baseline_reset(engine, logger, acct, info)
                        db.report_state(
                            symbol=config.SYMBOL,
                            balance=info.balance,
                            equity=info.equity,
                            margin_used=info.margin,
                            margin_free=info.margin_free,
                            floating_pnl=info.equity - info.balance,
                            open_positions=len(engine.b.positions()),
                            initial_balance=engine.initial_balance,
                            last_flow_ticket=engine.last_flow_ticket,
                            baseline_applied_at=engine.baseline_applied_at,
                        )
                        _check_profit_target(db, engine, logger, info, acct)
                except Exception:  # noqa: BLE001
                    pass

                # Velas para la grafica (la en formacion + la previa).
                _publish_candles(db, CANDLES_HEARTBEAT)

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
