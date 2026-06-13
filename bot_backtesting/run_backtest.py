"""Runner del backtest.

Uso:
    python -m bot_backtesting.run_backtest --data-dir bot_backtesting/data \
        --balance 5000 --out-dir bot_backtesting/results [--start 2024-01-01] [--end 2024-06-01]

Itera barra a barra sobre M15 (decision en el cierre, sin lookahead), simula el
broker y corre el SentinelEngine real en cada paso.
"""

import argparse
import datetime
import os

from bot import config
from bot.logger import Logger

from . import metrics
from .config_bt import DATA_FILES, INITIAL_BALANCE, SYMBOL_SPECS, make_params
from .market import Market, load_frames
from .sim_broker import SimBroker
from .sim_engine import BacktestEngine


def _iso_to_epoch(s):
    if not s:
        return None
    dt = datetime.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def _file_map(data_dir, symbol):
    safe = symbol.replace("/", "_")
    return {tf: os.path.join(data_dir, pat.format(symbol=safe)) for tf, pat in DATA_FILES.items()}


def run_backtest(data_dir, symbol=config.SYMBOL, balance=INITIAL_BALANCE,
                 out_dir=None, start=None, end=None, params=None, progress_every=0):
    frames = load_frames(_file_map(data_dir, symbol))

    end_epoch = _iso_to_epoch(end)
    if end_epoch:
        for tf in list(frames):
            frames[tf] = frames[tf][frames[tf]["time"] <= end_epoch].reset_index(drop=True)

    market = Market(frames)
    logger = Logger(enable_file=False, console_print=False)
    broker = SimBroker(market, SYMBOL_SPECS, logger, balance)
    engine = BacktestEngine(broker, logger, market)
    engine.cfg = params if params is not None else make_params()

    start_i = market.start_index_for(_iso_to_epoch(start))
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

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        metrics.write_trades_csv(os.path.join(out_dir, "trades.csv"), broker.closed_trades)
        metrics.write_equity_csv(os.path.join(out_dir, "equity.csv"), equity_curve)

    return stats, broker, equity_curve


def main():
    ap = argparse.ArgumentParser(description="Backtest del bot Sentinel (motor real).")
    ap.add_argument("--data-dir", default=os.path.join("bot_backtesting", "data"))
    ap.add_argument("--symbol", default=config.SYMBOL)
    ap.add_argument("--balance", type=float, default=INITIAL_BALANCE)
    ap.add_argument("--out-dir", default=os.path.join("bot_backtesting", "results"))
    ap.add_argument("--start", default=None, help="YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD")
    ap.add_argument("--progress-every", type=int, default=0, help="imprime avance cada N barras")
    args = ap.parse_args()

    stats, _broker, _eq = run_backtest(
        data_dir=args.data_dir, symbol=args.symbol, balance=args.balance,
        out_dir=args.out_dir, start=args.start, end=args.end,
        progress_every=args.progress_every,
    )
    print(metrics.format_summary(stats))
    if args.out_dir:
        print(f"\nCSV en: {args.out_dir}/trades.csv , {args.out_dir}/equity.csv")


if __name__ == "__main__":
    main()
