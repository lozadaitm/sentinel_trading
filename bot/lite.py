"""Modo LITE: minimo consumo para VPS. Sin TUI, sin velas, sin log en consola.

El motor de trading es EXACTAMENTE el mismo que en consola/TUI (bot.main.setup
+ trading_loop); lo unico que cambia es la presentacion: se imprime un banner
"Operando en <usuario>" una sola vez y no se vuelve a tocar stdout. El log
sigue completo en el CSV local y en Supabase (bot_logs), y el dashboard sigue
recibiendo heartbeat/posiciones igual que siempre.

Que se ahorra respecto a la TUI:
  - rich.Live refrescando el layout ~8 veces por segundo.
  - plotext redibujando la grafica de velas en cada frame.
  - el hilo de UI y el polling de teclado (msvcrt).
  - los print() por cada linea de log del modo consola.

Los eventos ERROR si se imprimen (son raros y baratos): si una instancia se
atasca, la ventana lo muestra sin tener que abrir el CSV.

Uso:                 python -m bot.lite
Uso (via launcher):  scripts\\run_instance.ps1 instances\\<usuario>.env -Lite
"""

from . import config, main as botmain
from .logger import Logger


def _print_error_sink(log_type, message, ts, price, lots, balance, ticket):
    """Unico contacto con stdout tras el banner: solo errores."""
    if log_type == "ERROR":
        print(f"{ts} [ERROR] {message}")


def run_lite(engine_cls=None):
    botmain.disable_quickedit()

    logger = Logger(enable_file=True, file_name=botmain._log_file_name(),
                    symbol=config.SYMBOL, console_print=False)
    logger.add_sink(_print_error_sink)

    try:
        db, broker, engine, logger = botmain.setup(logger=logger, engine_cls=engine_cls)
    except Exception as e:  # noqa: BLE001
        print(f"Error de arranque: {e}")
        raise SystemExit(1)

    who = engine.user_email or config.USER_ID
    print()
    print("=" * 60)
    print(f"  SENTINEL LITE  -  Operando en {who}")
    print(f"  bot={config.BOT_ID}  magic={config.MAGIC_NUMBER}  symbol={config.SYMBOL}")
    print("  Sin render (modo ahorro VPS). Log completo en CSV + dashboard.")
    print("  Ctrl+C para detener.")
    print("=" * 60)

    try:
        botmain.trading_loop(db, engine, logger)
    except KeyboardInterrupt:
        pass
    finally:
        logger.write("SYSTEM", "Detencion manual solicitada.")
        botmain.shutdown(db)
        print("Bot detenido.")


if __name__ == "__main__":
    run_lite()
