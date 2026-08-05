"""TUI en vivo del motor M5 (Grinder SmartCut).

Misma UI que bot/tui.py (grafica + HUD de gates + log + panel de parametros),
montada sobre M5Engine en vez del Sentinel. La grafica pasa a M5 y el HUD
muestra los gates propios del scalper (modo ADX, Gran Hermano, ancho de banda,
nivel de margen, presupuesto).

Uso:                 python -m bot.tui_m5
Uso (via instancia): scripts\\run_instance.ps1 instances\\<usuario>-m5.env -Module bot.tui_m5
"""

from .strategy_m5 import M5Engine
from .tui import run_tui


def main():
    run_tui(engine_cls=M5Engine)


if __name__ == "__main__":
    main()
