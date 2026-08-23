"""Backtests the SMA-crossover strategy against historical price data.

Replays strategy.generate_spot_signal bar-by-bar over historical closes, so
backtest results exercise the exact same decision code the live bot runs -
not a separate reimplementation that could drift from it.

LIMITATION (important): this backtests the underlying PRICE crossover +
target/stop-loss rules only. Live Nifty trading buys an option (Delta ~0.50
call), whose premium moves differently from the index itself because of
time decay (theta) and changing implied volatility - free historical NSE
options-chain data doesn't exist, so there's no way to backtest the actual
option P&L. Nifty's backtest here approximates "traded the index directly,"
which tells you whether the SMA-crossover *trigger* is any good, but not
what the real option trade would have returned. BTC's backtest is exact,
since live BTC trading is spot-based too - no options involved there.

Usage:
    python backtest.py --instrument BTC
    python backtest.py --instrument NIFTY --days 60 --sma-period 20
    python backtest.py --instrument BTC --target-pct 0.03 --stop-loss-pct 0.02
"""

import argparse
import sys

import pandas as pd
import yfinance as yf

import strategy

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# yfinance's max lookback window per intraday interval (days)
_MAX_INTRADAY_LOOKBACK_DAYS = {
    "1m": 7,
    "2m": 60,
    "5m": 60,
    "15m": 60,
    "30m": 60,
    "60m": 730,
    "90m": 60,
}

INSTRUMENT_DEFAULTS = {
    "NIFTY": {
        "yfinance_ticker": "^NSEI",
        "currency_symbol": "₹",
        # Underlying-index moves are much smaller than option-premium moves,
        # so these are NOT the same as the live bot's PROFIT_TARGET_PCT/
        # STOP_LOSS_PCT (which apply to the option premium). Override via
        # --target-pct/--stop-loss-pct if you want to test something else.
        "profit_target_pct": 0.01,
        "stop_loss_pct": 0.005,
    },
    "BTC": {
        "yfinance_ticker": "BTC-USD",
        "currency_symbol": "$",
        "profit_target_pct": 0.03,
        "stop_loss_pct": 0.02,
    },
}


def fetch_historical(ticker: str, interval: str, days: int) -> pd.DataFrame:
    max_days = _MAX_INTRADAY_LOOKBACK_DAYS.get(interval)
    if max_days and days > max_days:
        raise ValueError(
            f"yfinance only provides '{interval}' data for the last {max_days} days "
            f"(requested {days}). Use a smaller --days or a coarser --interval."
        )

    history = yf.Ticker(ticker).history(period=f"{days}d", interval=interval)
    if history is None or history.empty:
        raise RuntimeError(f"No historical data returned for {ticker} at interval {interval}")
    return history


def run_backtest(
    closes: pd.Series,
    sma_period: int,
    profit_target_pct: float,
    stop_loss_pct: float,
    ticker: str = "BACKTEST",
    trend_sma_period: int | None = None,
) -> dict:
    """Replay the live spot strategy bar-by-bar over `closes` and return trade stats.

    If `trend_sma_period` is set, it's passed through to
    strategy.generate_spot_signal's `trend_sma` argument - the same
    trend-confirmation filter the live bot can apply.
    """
    sma_series = closes.rolling(window=sma_period).mean()
    trend_sma_series = closes.rolling(window=trend_sma_period).mean() if trend_sma_period else None
    state = {"in_position": False}
    trades = []
    open_trade = None

    for i in range(1, len(closes)):
        sma = sma_series.iloc[i]
        prev_sma = sma_series.iloc[i - 1]
        if pd.isna(sma) or pd.isna(prev_sma):
            continue
        trend_sma = None
        if trend_sma_series is not None:
            if pd.isna(trend_sma_series.iloc[i]):
                continue
            trend_sma = float(trend_sma_series.iloc[i])

        current_price = float(closes.iloc[i])
        prev_price = float(closes.iloc[i - 1])

        result = strategy.generate_spot_signal(
            ticker=ticker,
            current_price=current_price,
            sma=float(sma),
            prev_price=prev_price,
            prev_sma=float(prev_sma),
            state=state,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            trend_sma=trend_sma,
        )
        state = result["state"]
        signal = result["signal"]

        if signal["action"] == "BUY":
            open_trade = {"entry_time": closes.index[i], "entry_price": signal["entry_price"]}
        elif signal["action"] == "SELL" and open_trade is not None:
            exit_price = signal["exit_price"]
            pnl_pct = (exit_price - open_trade["entry_price"]) / open_trade["entry_price"]
            trades.append(
                {
                    "entry_time": open_trade["entry_time"],
                    "exit_time": closes.index[i],
                    "entry_price": open_trade["entry_price"],
                    "exit_price": exit_price,
                    "pnl_pct": pnl_pct,
                    "reason": signal["reason"],
                }
            )
            open_trade = None

    return _summarize_trades(trades)


def _summarize_trades(trades: list) -> dict:
    if not trades:
        return {"trades": [], "num_trades": 0}

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]

    compounded = 1.0
    peak = 1.0
    max_drawdown_pct = 0.0
    for t in trades:
        compounded *= 1 + t["pnl_pct"]
        peak = max(peak, compounded)
        max_drawdown_pct = max(max_drawdown_pct, (peak - compounded) / peak)

    return {
        "trades": trades,
        "num_trades": len(trades),
        "win_rate_pct": len(wins) / len(trades) * 100,
        "avg_win_pct": (sum(t["pnl_pct"] for t in wins) / len(wins) * 100) if wins else 0.0,
        "avg_loss_pct": (sum(t["pnl_pct"] for t in losses) / len(losses) * 100) if losses else 0.0,
        "compounded_return_pct": (compounded - 1) * 100,
        "max_drawdown_pct": max_drawdown_pct * 100,
    }


def print_report(instrument: str, config: dict, stats: dict) -> None:
    cur = INSTRUMENT_DEFAULTS[instrument]["currency_symbol"]
    print(f"\n=== {instrument} backtest ===")
    trend_str = f"  trend_sma={config['trend_sma_period']}" if config.get("trend_sma_period") else ""
    print(
        f"ticker={config['yfinance_ticker']}  interval={config['interval']}  days={config['days']}  "
        f"sma_period={config['sma_period']}  target={config['profit_target_pct']:.2%}  "
        f"stop_loss={config['stop_loss_pct']:.2%}{trend_str}"
    )

    if stats["num_trades"] == 0:
        print("No trades triggered in this window - try a longer period or a shorter SMA.")
        return

    print(f"trades: {stats['num_trades']}")
    print(f"win rate: {stats['win_rate_pct']:.1f}%")
    print(f"avg win: {stats['avg_win_pct']:.2f}%   avg loss: {stats['avg_loss_pct']:.2f}%")
    print(f"compounded return: {stats['compounded_return_pct']:.2f}%")
    print(f"max drawdown: {stats['max_drawdown_pct']:.2f}%")
    print("\nlast 5 trades:")
    for t in stats["trades"][-5:]:
        print(
            f"  {t['entry_time']} -> {t['exit_time']}  "
            f"{cur}{t['entry_price']:.2f} -> {cur}{t['exit_price']:.2f}  "
            f"({t['pnl_pct']*100:+.2f}%)  [{t['reason']}]"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest the SMA-crossover strategy")
    parser.add_argument("--instrument", choices=["NIFTY", "BTC", "BOTH"], default="BOTH")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--sma-period", type=int, default=20)
    parser.add_argument("--target-pct", type=float, default=None, help="Overrides the instrument default")
    parser.add_argument("--stop-loss-pct", type=float, default=None, help="Overrides the instrument default")
    parser.add_argument(
        "--trend-sma-period",
        type=int,
        default=None,
        help="Only take BUY signals when price is also above this longer SMA (trend-confirmation filter)",
    )
    args = parser.parse_args()

    instruments = ["NIFTY", "BTC"] if args.instrument == "BOTH" else [args.instrument]

    for instrument in instruments:
        defaults = INSTRUMENT_DEFAULTS[instrument]
        config = {
            "yfinance_ticker": defaults["yfinance_ticker"],
            "interval": args.interval,
            "days": args.days,
            "sma_period": args.sma_period,
            "profit_target_pct": args.target_pct if args.target_pct is not None else defaults["profit_target_pct"],
            "stop_loss_pct": args.stop_loss_pct if args.stop_loss_pct is not None else defaults["stop_loss_pct"],
            "trend_sma_period": args.trend_sma_period,
        }

        if instrument == "NIFTY":
            print(
                "\nNOTE: NIFTY backtest trades the underlying index directly, not the "
                "option premium the live bot actually buys. Real option P&L differs "
                "due to theta decay and IV changes - treat this as a trigger-quality "
                "check, not a return forecast."
            )

        try:
            history = fetch_historical(config["yfinance_ticker"], config["interval"], config["days"])
            stats = run_backtest(
                history["Close"],
                sma_period=config["sma_period"],
                profit_target_pct=config["profit_target_pct"],
                stop_loss_pct=config["stop_loss_pct"],
                ticker=instrument,
                trend_sma_period=args.trend_sma_period,
            )
            print_report(instrument, config, stats)
        except Exception as exc:  # noqa: BLE001
            print(f"\n=== {instrument} backtest FAILED: {exc} ===")


if __name__ == "__main__":
    main()
