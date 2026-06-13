"""Metricas y volcado de resultados del backtest."""

import csv
import datetime


def _fmt_ts(epoch):
    return datetime.datetime.utcfromtimestamp(int(epoch)).strftime("%Y-%m-%d %H:%M")


def compute(equity_curve, closed_trades, initial_balance, final_balance, blown=False):
    equity = [e[1] for e in equity_curve]

    peak = float("-inf")
    max_dd = 0.0
    max_dd_pct = 0.0
    for eq in equity:
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
        if peak > 0 and (dd / peak) * 100 > max_dd_pct:
            max_dd_pct = (dd / peak) * 100

    profits = [t["profit"] for t in closed_trades]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    n = len(profits)
    pf = (gross_win / gross_loss) if gross_loss > 1e-9 else float("inf")
    max_positions = max((e[2] for e in equity_curve), default=0)

    return {
        "initial_balance": initial_balance,
        "final_balance": final_balance,
        "net_profit": final_balance - initial_balance,
        "return_pct": (final_balance / initial_balance - 1) * 100 if initial_balance else 0,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": (len(wins) / n * 100) if n else 0,
        "profit_factor": pf,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "avg_win": (gross_win / len(wins)) if wins else 0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0,
        "max_concurrent_positions": max_positions,
        "bars": len(equity_curve),
        "blown": blown,
    }


def format_summary(stats):
    pf = stats["profit_factor"]
    pf_txt = "inf" if pf == float("inf") else f"{pf:.2f}"
    return (
        "===== BACKTEST SENTINEL =====\n"
        f"Balance inicial : {stats['initial_balance']:.2f}\n"
        f"Balance final   : {stats['final_balance']:.2f}\n"
        f"Net profit      : {stats['net_profit']:.2f} ({stats['return_pct']:.2f}%)\n"
        f"Max drawdown    : {stats['max_drawdown']:.2f} ({stats['max_drawdown_pct']:.2f}%)\n"
        f"Trades          : {stats['trades']} (W {stats['wins']} / L {stats['losses']}, "
        f"win {stats['win_rate_pct']:.1f}%)\n"
        f"Profit factor   : {pf_txt}\n"
        f"Avg win / loss  : {stats['avg_win']:.2f} / {stats['avg_loss']:.2f}\n"
        f"Max posiciones  : {stats['max_concurrent_positions']}\n"
        f"Barras evaluadas: {stats['bars']}\n"
        + ("*** CUENTA REVENTADA (stop-out del broker) ***\n" if stats.get("blown") else "")
        + "============================="
    )


def write_trades_csv(path, closed_trades):
    cols = ["ticket", "side", "volume", "open_time", "close_time",
            "open_price", "close_price", "profit", "open_comment", "close_reason"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols + ["open_dt", "close_dt"])
        for t in closed_trades:
            w.writerow([t[c] for c in cols] + [_fmt_ts(t["open_time"]), _fmt_ts(t["close_time"])])


def write_equity_csv(path, equity_curve):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "datetime", "equity", "positions"])
        for ts, eq, npos in equity_curve:
            w.writerow([ts, _fmt_ts(ts), f"{eq:.2f}", npos])
