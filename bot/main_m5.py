"""Entrypoint del motor M5 (Grinder SmartCut).

Reusa todo el arranque del Sentinel (MT5, Supabase, broker, bucle de polling,
heartbeat, bot_positions) y solo cambia la clase de motor. Ver bot/main.py.

Uso (modo consola):  python -m bot.main_m5
Uso (via instancia): scripts\\run_instance.ps1 instances\\<usuario>-m5.env

El .env de la instancia debe fijar:
    BOT_ID=m5
    MAGIC_NUMBER=100200      # distinto del M15, o los dos bots verian lo mismo
"""

from . import main as botmain
from .strategy_m5 import M5Engine


def run():
    botmain.run(engine_cls=M5Engine)


if __name__ == "__main__":
    run()
