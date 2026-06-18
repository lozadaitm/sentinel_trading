"""TUI guiada del backtester (rich): el usuario lo inicia y la interfaz lo guia.

En vez de pasar parametros por linea de comandos, esta TUI pregunta paso a paso
(balance, fechas, simbolo, output) con valores por defecto, confirma, descarga
las velas de MT5 y muestra el resultado en paneles/tablas.

Uso:
    python -m bot_backtesting.gui
    python -m bot_backtesting.run_backtest     (sin argumentos -> lanza esta TUI)
"""

import datetime
import os

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt
    from rich.table import Table
    from rich.text import Text
except ImportError:
    raise SystemExit(
        "Faltan dependencias de UI. Instala con:\n"
        "    python -m pip install rich"
    )

from bot import config

from . import metrics
from .config_bt import INITIAL_BALANCE
from .run_backtest import DEFAULT_WARMUP_DAYS, run_backtest

console = Console()

_DATE_FMT = "%Y-%m-%d"


def _ask_date(label, default=None):
    """Pide una fecha YYYY-MM-DD y reintenta hasta que sea valida."""
    while True:
        raw = Prompt.ask(f"  [cyan]{label}[/cyan]", default=default)
        try:
            return datetime.datetime.strptime(raw, _DATE_FMT).date()
        except (ValueError, TypeError):
            console.print("    [red]Formato invalido. Usa YYYY-MM-DD (ej. 2026-06-12).[/red]")


def _collect_inputs():
    """Wizard de entrada. Devuelve dict con los parametros del backtest."""
    console.print(Panel.fit(
        Text("BACKTESTER SENTINEL", justify="center", style="bold cyan"),
        subtitle="asistente guiado", border_style="cyan",
    ))
    console.print("[dim]Enter para aceptar el valor por defecto entre [ ].[/dim]\n")

    balance = FloatPrompt.ask("  [cyan]Balance inicial[/cyan]", default=float(INITIAL_BALANCE))

    today = datetime.date.today()
    default_start = (today - datetime.timedelta(days=7)).strftime(_DATE_FMT)
    default_end = today.strftime(_DATE_FMT)

    while True:
        start_date = _ask_date("Fecha inicio (YYYY-MM-DD)", default=default_start)
        end_date = _ask_date("Fecha fin    (YYYY-MM-DD)", default=default_end)
        if end_date >= start_date:
            break
        console.print("    [red]La fecha fin debe ser >= fecha inicio.[/red]")

    symbol = Prompt.ask("  [cyan]Simbolo[/cyan]", default=config.SYMBOL)
    output = Prompt.ask("  [cyan]Carpeta de salida[/cyan]",
                        default=os.path.join("bot_backtesting", "results"))
    warmup = IntPrompt.ask("  [cyan]Dias de calentamiento[/cyan]", default=DEFAULT_WARMUP_DAYS)

    return {
        "balance": balance,
        "start_date": start_date.strftime(_DATE_FMT),
        "end_date": end_date.strftime(_DATE_FMT),
        "symbol": symbol,
        "output": output,
        "warmup_days": warmup,
    }


def _confirm(params):
    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_column(style="dim")
    t.add_column(style="bold")
    t.add_row("Balance inicial", f"{params['balance']:.2f}")
    t.add_row("Periodo", f"{params['start_date']}  ->  {params['end_date']}  (fin inclusivo)")
    t.add_row("Simbolo", params["symbol"])
    t.add_row("Salida", params["output"])
    t.add_row("Warmup", f"{params['warmup_days']} dias")
    console.print(Panel(t, title="Resumen", border_style="cyan"))
    return Confirm.ask("[cyan]Ejecutar backtest?[/cyan]", default=True)


def _render_results(stats, output):
    blown = stats.get("blown")
    net = stats["net_profit"]
    net_style = "bold green" if net >= 0 else "bold red"
    pf = stats["profit_factor"]
    pf_txt = "inf" if pf == float("inf") else f"{pf:.2f}"

    t = Table(show_header=False, box=None, pad_edge=False)
    t.add_column(style="dim")
    t.add_column(justify="right")
    t.add_row("Balance inicial", f"{stats['initial_balance']:.2f}")
    t.add_row("Balance final", f"{stats['final_balance']:.2f}")
    t.add_row("Net profit", Text(f"{net:.2f} ({stats['return_pct']:.2f}%)", style=net_style))
    t.add_row("Max drawdown", f"{stats['max_drawdown']:.2f} ({stats['max_drawdown_pct']:.2f}%)")
    t.add_row("Trades", f"{stats['trades']}  (W {stats['wins']} / L {stats['losses']})")
    t.add_row("Win rate", f"{stats['win_rate_pct']:.1f}%")
    t.add_row("Profit factor", pf_txt)
    t.add_row("Avg win / loss", f"{stats['avg_win']:.2f} / {stats['avg_loss']:.2f}")
    t.add_row("Max posiciones", str(stats["max_concurrent_positions"]))
    t.add_row("Barras evaluadas", str(stats["bars"]))

    border = "red" if blown else "green"
    title = "RESULTADO (cuenta reventada)" if blown else "RESULTADO"
    console.print(Panel(t, title=title, border_style=border))
    if output:
        console.print(f"[dim]CSV en:[/dim] {output}/trades.csv , {output}/equity.csv")


def run_gui():
    try:
        params = _collect_inputs()
    except (KeyboardInterrupt, EOFError):
        console.print("\n[yellow]Cancelado.[/yellow]")
        return

    if not _confirm(params):
        console.print("[yellow]Cancelado.[/yellow]")
        return

    try:
        with console.status("[cyan]Descargando velas de MT5 y corriendo backtest...[/cyan]",
                            spinner="dots"):
            stats, _broker, _eq = run_backtest(
                balance=params["balance"],
                start_date=params["start_date"],
                end_date=params["end_date"],
                symbol=params["symbol"],
                output=params["output"],
                warmup_days=params["warmup_days"],
            )
    except Exception as e:  # noqa: BLE001 - mostrar el error al usuario, no traceback crudo
        console.print(Panel(f"[red]{type(e).__name__}: {e}[/red]",
                            title="Fallo el backtest", border_style="red"))
        return

    console.print()
    _render_results(stats, params["output"])


def main():
    run_gui()


if __name__ == "__main__":
    main()
