"""Modo LITE del motor M5 (Grinder SmartCut). Ver bot/lite.py.

Uso:                 python -m bot.lite_m5
Uso (via launcher):  scripts\\run_instance.ps1 instances\\<usuario>.env -BotId m5 -Lite
"""

from .lite import run_lite
from .strategy_m5 import M5Engine


def main():
    run_lite(engine_cls=M5Engine)


if __name__ == "__main__":
    main()
