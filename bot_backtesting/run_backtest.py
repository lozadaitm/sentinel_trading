"""Runner del backtest.

Uso:
    python -m bot_backtesting.run_backtest --balance 5000 \
        --start-date 2026-06-12 --end-date 2026-06-16 [--output ruta/o/dir]

El backtester descarga las velas (M15/M5/H4) directamente de MT5 para el periodo
pedido (mas un margen de calentamiento para los indicadores). No hace falta
generar CSVs antes. Itera barra a barra sobre M15 (decision en el cierre, sin
lookahead), simula el broker y corre el SentinelEngine real en cada paso.
"""

import argparse
import datetime
import os

from bot import config
from bot.logger import Logger

from . import metrics
from .config_bt import SYMBOL_SPECS, make_params
from .fetch_data import fetch_frames
from .market import Market
from .sim_broker import SimBroker
from .sim_engine import BacktestEngine

# Margen (dias) descargado ANTES de start_date para calentar indicadores. La
# estructura H4 mira hasta ~150 barras (=25 dias de mercado); 45 dias calendario
# dan holgura aun descontando fines de semana sin cotizacion.
DEFAULT_WARMUP_DAYS = 45


def _parse_date(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)


def run_backtest(balance, start_date, end_date, symbol=config.SYMBOL,
                 output=None, params=None, warmup_days=DEFAULT_WARMUP_DAYS,
                 progress_every=0):
    """Descarga datos de MT5 para [start_date, end_date] y corre el backtest.

    start_date / end_date: 'YYYY-MM-DD' (UTC). end_date es inclusivo (cubre todo
    el dia). output: directorio donde escribir trades.csv y equity.csv (opcional).
    """
    start_dt = _parse_date(start_date)
    # end inclusivo: hasta el final del dia end_date.
    end_dt = _parse_date(end_date) + datetime.timedelta(days=1)
    fetch_from = start_dt - datetime.timedelta(days=warmup_days)

    start_epoch = int(start_dt.timestamp())
    end_epoch = int(end_dt.timestamp())

    frames = fetch_frames(symbol, fetch_from, end_dt, verbose=bool(progress_every))
    # Estricto: open_time < medianoche del dia siguiente a end_date. Asi se incluye
    # la ultima barra de end_date (abre 23:45, cierra 00:00) pero NO la primera del
    # dia posterior (que abriria justo en el limite).
    for tf in list(frames):
        frames[tf] = frames[tf][frames[tf]["time"] < end_epoch].reset_index(drop=True)

    market = Market(frames)
    logger = Logger(enable_file=False, console_print=False)
    broker = SimBroker(market, SYMBOL_SPECS, logger, balance)
    engine = BacktestEngine(broker, logger, market)
    engine.cfg = params if params is not None else make_params()

    start_i = market.start_index_for(start_epoch)
    n = len(market)
    stop_out = SYMBOL_SPECS.get("stop_out_level", 0)

    equity_curve = []
    for i in range(start_i, n):
        market.step_to(i)
        broker.process_bar_sl()   # SL primero (rango de la barra)
        broker.mark_to_market()
        engine.on_tick()          # decisiones del motor real
        broker.mark_to_market()   # refleja ordenes nuevas

        # Stop-out del broker (mecanica de la cuenta, no de la estrategia)
        if stop_out and broker.margin_level() < stop_out:
            broker.liquidate("StopOut")
            equity_curve.append((market.current_time, broker.account_equity(), 0))
            break

        equity_curve.append((market.current_time, broker.account_equity(), len(broker.positions())))
        if progress_every and (i - start_i) % progress_every == 0:
            print(f"  ... barra {i - start_i}/{n - start_i}  equity={broker.account_equity():.2f}")

    if not broker.blown:
        broker.close_all_eot()
    stats = metrics.compute(equity_curve, broker.closed_trades, balance, broker.balance,
                            blown=broker.blown)

    if output:
        os.makedirs(output, exist_ok=True)
        metrics.write_trades_csv(os.path.join(output, "trades.csv"), broker.closed_trades)
        metrics.write_equity_csv(os.path.join(output, "equity.csv"), equity_curve)

    return stats, broker, equity_curve


def main():
    ap = argparse.ArgumentParser(description="Backtest del bot Sentinel (motor real, datos de MT5).")
    ap.add_argument("--balance", type=float, required=True, help="Balance inicial de la cuenta")
    ap.add_argument("--start-date", required=True, help="YYYY-MM-DD (inicio del periodo, UTC)")
    ap.add_argument("--end-date", required=True, help="YYYY-MM-DD (fin inclusivo del periodo, UTC)")
    ap.add_argument("--output", default=os.path.join("bot_backtesting", "results"),
                    help="Directorio para trades.csv y equity.csv (opcional)")
    ap.add_argument("--symbol", default=config.SYMBOL, help=f"Simbolo (default {config.SYMBOL})")
    ap.add_argument("--warmup-days", type=int, default=DEFAULT_WARMUP_DAYS,
                    help="Dias de calentamiento descargados antes de start-date")
    ap.add_argument("--progress-every", type=int, default=0, help="imprime avance cada N barras")
    args = ap.parse_args()

    stats, _broker, _eq = run_backtest(
        balance=args.balance, start_date=args.start_date, end_date=args.end_date,
        symbol=args.symbol, output=args.output, warmup_days=args.warmup_days,
        progress_every=args.progress_every,
    )
    print(metrics.format_summary(stats))
    if args.output:
        print(f"\nCSV en: {args.output}/trades.csv , {args.output}/equity.csv")


if __name__ == "__main__":
    main()
