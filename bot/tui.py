"""TUI en vivo del bot: grafica (plotext) + HUD de gates + log + panel de params.

Layout:
  - Arriba:  grafica de velas M15 (plotext).
  - Centro:  HUD "actual vs requerido" de las condiciones de entrada Op1.
  - Abajo:   stream de log (capturado del Logger via sink), con scroll.
  - Tecla p: muestra/oculta el panel de PARAMETROS (bot_config) a la derecha.

Uso:    python -m bot.tui
Teclas: [p] params   [Up/Dn, PgUp/PgDn] desplazar log   [Fin] log al vivo   [q] salir

El motor de trading corre en un HILO aparte (bot.main.trading_loop). La UI
solo LEE engine.hud / engine.df_m15 / engine.cfg (snapshots publicados por el
motor). Ninguna llamada a MetaTrader5 ocurre en el hilo de la UI.
"""

import threading
import time
from collections import deque

import msvcrt

try:
    import plotext as plt
    from rich.columns import Columns
    from rich.console import Console, Group
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.align import Align
except ImportError:
    raise SystemExit(
        "Faltan dependencias de UI. Instala con:\n"
        "    python -m pip install rich plotext"
    )

from . import config, main as botmain
from .logger import Logger


LOG_COLORS = {
    "SYSTEM": "cyan", "ERROR": "bold red", "EXITO": "bold green",
    "OPERACION": "green", "HEALER": "magenta", "UNWIND": "yellow",
    "GRINDER": "blue", "SHADOW": "dim", "PROTECCION": "yellow",
    "ENTRADA": "bold green", "RECOVERY": "green", "RESCATE": "bold yellow",
    "CIERRE": "green", "VCB": "bold red",
}

PARAM_SKIP = {"id", "user_id", "updated_at"}

HELP_HINT = "p=params   Up/Dn PgUp/PgDn=log   Fin=al vivo   q=salir"


class LogBuffer:
    """Ring buffer thread-safe para los eventos del Logger."""

    def __init__(self, maxlen=1000):
        self._dq = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._added = 0  # total de eventos recibidos (monotonico, ignora el drop del ring)

    def add(self, log_type, message, ts, price, lots, balance, ticket=0):
        if ticket:
            message = f"{message} #{ticket}"
        with self._lock:
            self._dq.append((ts, log_type, message))
            self._added += 1

    def tail(self, n):
        with self._lock:
            return list(self._dq)[-n:]

    def snapshot(self):
        """Copia completa del buffer (para render con scroll)."""
        with self._lock:
            return list(self._dq)

    def added(self):
        """Contador monotonico de eventos recibidos (para anclar el scroll)."""
        with self._lock:
            return self._added


# ------------------------------------------------------------------
# Renderers
# ------------------------------------------------------------------
def render_chart(df, symbol, width, height):
    if df is None or len(df) < 2:
        return Align.center(Text("Esperando datos de MT5...", style="dim"))

    sub = df.iloc[-min(len(df), 60):]
    plt.clf()
    plt.plotsize(max(width, 12), max(height, 5))
    plt.theme("pro")
    try:
        plt.date_form("d/m/Y H:M")
        dates = [t.strftime("%d/%m/%Y %H:%M") for t in sub["time"]]
        plt.candlestick(dates, {
            "Open": sub["open"].tolist(),
            "High": sub["high"].tolist(),
            "Low": sub["low"].tolist(),
            "Close": sub["close"].tolist(),
        })
    except Exception:  # noqa: BLE001  (fallback robusto si plotext falla)
        plt.clf()
        plt.plotsize(max(width, 12), max(height, 5))
        plt.theme("pro")
        plt.plot(sub["close"].tolist())
    return Text.from_ansi(plt.build())


def render_hud(hud):
    if not hud or hud.get("status"):
        msg = (hud or {}).get("status") or "Iniciando motor..."
        return Panel(Align.center(Text(msg, style="yellow")),
                     title="HUD ENTRADA OP1", border_style="yellow")

    ready = hud["ready"]
    if hud["positions"] > 0:
        state = Text(f"EN OPERACION ({hud['positions']} pos)", style="bold cyan")
        border = "cyan"
    elif ready:
        state = Text("LISTO PARA ENTRAR", style="bold green")
        border = "green"
    else:
        state = Text(f"Faltan {hud['blockers']} condicion(es)", style="bold yellow")
        border = "yellow"

    head = Table.grid(expand=True)
    head.add_column(justify="left")
    head.add_column(justify="right")
    head.add_row(
        Text.assemble(
            ("Lado ", "dim"), (f"{hud['side']}   ", "bold"),
            ("Bid ", "dim"), (f"{hud['bid']:.2f}  ", "bold cyan"),
            ("Ask ", "dim"), (f"{hud['ask']:.2f}  ", "bold cyan"),
            ("RSI ", "dim"), (f"{hud['rsi']:.1f}", "bold"),
        ),
        Text.assemble(
            ("Bal ", "dim"), (f"{hud['balance']:.2f}  ", "white"),
            ("Eq ", "dim"), (f"{hud['equity']:.2f}", "white"),
        ),
    )
    head.add_row(state, Text(f"servidor {hud['server_time']}", style="dim"))

    tbl = Table(expand=True, show_edge=False, pad_edge=False)
    tbl.add_column("Condicion", no_wrap=True)
    tbl.add_column("Actual", justify="right")
    tbl.add_column("Requerido", justify="right", style="dim")
    tbl.add_column("", justify="center", width=4)
    for name, actual, needed, ok in hud["gates"]:
        mark = Text("OK", style="bold green") if ok else Text("X", style="bold red")
        tbl.add_row(name, Text(str(actual), style="green" if ok else "red"), str(needed), mark)

    return Panel(Group(head, tbl),
                 title="HUD ENTRADA OP1  —  actual vs requerido",
                 border_style=border)


def render_log(buf, height, offset=0):
    """Renderiza una ventana del log de `height` filas.

    `offset` = numero de lineas desplazadas HACIA ARRIBA desde el fondo:
      0 = pegado al final (live tail, comportamiento previo).
    Devuelve (Panel, offset_clampeado) para que el caller no acumule desborde.
    """
    rows_all = buf.snapshot()
    total = len(rows_all)
    view = max(height, 1)
    max_off = max(0, total - view)
    off = max(0, min(offset, max_off))       # clamp al rango valido
    end = total - off
    start = max(0, end - view)
    rows = rows_all[start:end]

    if not rows:
        body = Text("(sin eventos aun)", style="dim")
    else:
        body = Text()
        for ts, lt, msg in rows:
            body.append(f"{ts} ", style="dim")
            body.append(f"[{lt}] ", style=LOG_COLORS.get(lt, "white"))
            body.append(f"{msg}\n")

    if off == 0:
        title = "LOG  (Up/Dn PgUp/PgDn = desplazar)"
        border = "blue"
    else:
        title = f"LOG  ^{off}  [{start + 1}-{end}/{total}]  (Fin = al vivo)"
        border = "yellow"
    return Panel(body, title=title, border_style=border), off


def render_params(cfg):
    if not cfg:
        return Panel(Text("Aun no se cargo bot_config...", style="dim"),
                     title="PARAMETROS", border_style="magenta")

    items = [(k, cfg[k]) for k in sorted(cfg) if k not in PARAM_SKIP]
    mid = (len(items) + 1) // 2

    def mk(chunk):
        t = Table(show_edge=False, expand=True, pad_edge=False)
        t.add_column("Param", style="cyan", no_wrap=True)
        t.add_column("Valor", justify="right", style="white")
        for k, v in chunk:
            t.add_row(str(k), str(v))
        return t

    cols = Columns([mk(items[:mid]), mk(items[mid:])], expand=True)
    return Panel(cols, title="PARAMETROS (bot_config)  —  (p) cerrar",
                 border_style="magenta")


def build_layout(layout, engine, log_buf, show_params, size, log_scroll=0):
    width, height = size.width, size.height
    avail = max(height - 14, 12)        # 14 filas reservadas al HUD
    top_h = int(avail * 0.72)
    log_h = max(avail - top_h, 4)

    layout.split_column(
        Layout(name="top", ratio=72),
        Layout(name="hud", size=14),
        Layout(name="log", ratio=28),
    )

    df = engine.df_m15
    symbol = config.SYMBOL
    if show_params:
        layout["top"].split_row(Layout(name="chart", ratio=62), Layout(name="params", ratio=38))
        chart_w = int(width * 0.62) - 4
        layout["chart"].update(Panel(render_chart(df, symbol, chart_w, top_h - 2),
                                     title=f"{symbol}  M15", border_style="cyan",
                                     subtitle=Text(HELP_HINT, style="dim")))
        layout["params"].update(render_params(engine.cfg))
    else:
        layout["top"].update(Panel(render_chart(df, symbol, width - 4, top_h - 2),
                                   title=f"{symbol}  M15", border_style="cyan",
                                   subtitle=Text(HELP_HINT, style="dim")))

    layout["hud"].update(render_hud(engine.hud))
    log_panel, log_scroll = render_log(log_buf, log_h - 2, log_scroll)
    layout["log"].update(log_panel)
    return log_scroll


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def run_tui():
    botmain.disable_quickedit()
    console = Console()
    log_buf = LogBuffer()

    logger = Logger(enable_file=True, file_name="Gold_HyperGrinder_v20",
                    symbol=config.SYMBOL, console_print=False)
    logger.add_sink(log_buf.add)

    try:
        db, broker, engine, logger = botmain.setup(logger=logger)
    except Exception as e:  # noqa: BLE001
        console.print(f"[bold red]Error de arranque:[/] {e}")
        return

    stop_event = threading.Event()
    worker = threading.Thread(
        target=botmain.trading_loop, args=(db, engine, logger, stop_event), daemon=True
    )
    worker.start()

    show_params = False
    log_scroll = 0            # 0 = log pegado al final (al vivo); >0 = desplazado hacia atras
    prev_added = log_buf.added()
    layout = Layout()

    try:
        with Live(layout, console=console, screen=True, auto_refresh=False) as live:
            while not stop_event.is_set():
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):        # prefijo de teclas especiales
                        code = msvcrt.getwch()
                        if code == "H":               # Flecha arriba
                            log_scroll += 1
                        elif code == "P":             # Flecha abajo
                            log_scroll -= 1
                        elif code == "I":             # RePag (PageUp)
                            log_scroll += 10
                        elif code == "Q":             # AvPag (PageDown)
                            log_scroll -= 10
                        elif code == "G":             # Inicio (Home) -> tope del historial
                            log_scroll = 10 ** 9
                        elif code == "O":             # Fin (End) -> volver al vivo
                            log_scroll = 0
                        continue
                    ch = ch.lower()
                    if ch == "q":
                        stop_event.set()
                    elif ch == "p":
                        show_params = not show_params
                    elif ch in ("k", "w"):            # alternativas sin flechas: subir
                        log_scroll += 1
                    elif ch in ("j", "s"):            # bajar
                        log_scroll -= 1
                    elif ch == "g":                   # ir al vivo (fin)
                        log_scroll = 0
                if log_scroll < 0:
                    log_scroll = 0

                # Anclaje: si el usuario esta leyendo hacia atras (scroll>0) y llegan
                # eventos nuevos, se desplaza el offset para que la vista NO se mueva.
                now_added = log_buf.added()
                if log_scroll > 0 and now_added > prev_added:
                    log_scroll += now_added - prev_added
                prev_added = now_added

                log_scroll = build_layout(layout, engine, log_buf, show_params,
                                          console.size, log_scroll)
                live.refresh()
                time.sleep(0.12)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        worker.join(timeout=2)
        botmain.shutdown(db)
        console.print("Bot detenido.")


if __name__ == "__main__":
    run_tui()
