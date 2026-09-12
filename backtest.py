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
    "USDINR": {
        "yfinance_ticker": "USDINR=X",
        "currency_symbol": "₹",
        # Forex moves are far smaller than crypto's - these are ~10x tighter
        # than BTC's, sized off USD/INR's actual 15m volatility.
        "profit_target_pct": 0.004,
        "stop_loss_pct": 0.002,
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


def _rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    """Vectorized RSI matching analyzer.calculate_rsi's formula, one value per bar."""
    delta = closes.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.where(avg_loss != 0, 100.0)


def _macd_histogram_series(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """Vectorized MACD histogram (macd line - signal line), one value per bar."""
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line - signal_line


_FIB_FRACTIONS = {"0.236": 0.236, "0.382": 0.382, "0.5": 0.5, "0.618": 0.618, "0.786": 0.786}


def _fib_support_series(closes: pd.Series, lookback: int = 50, level: str = "0.5") -> pd.Series:
    """Rolling Fibonacci retracement support level, one value per bar."""
    rolling_high = closes.rolling(window=lookback).max()
    rolling_low = closes.rolling(window=lookback).min()
    span = rolling_high - rolling_low
    return rolling_high - span * _FIB_FRACTIONS[level]


def run_backtest(
    closes: pd.Series,
    sma_period: int,
    profit_target_pct: float,
    stop_loss_pct: float,
    ticker: str = "BACKTEST",
    trend_sma_period: int | None = None,
    profit_target_dollars: float | None = None,
    rsi_period: int | None = None,
    rsi_overbought: float = 70.0,
    use_macd: bool = False,
    fib_lookback: int | None = None,
    fib_level: str = "0.5",
) -> dict:
    """Replay the live spot strategy bar-by-bar over `closes` and return trade stats.

    `trend_sma_period`, `rsi_period`, `use_macd`, and `fib_lookback` each
    independently enable the matching optional filter in
    strategy.generate_spot_signal - the same filters the live bot can apply.

    Crossover detection uses generate_spot_signal's persisted
    `state["was_above_sma"]` mechanism, same as live - since this loop visits
    every candle in order, that's equivalent to a plain adjacent-candle
    comparison here, but it's the same code path the live bot runs.
    """
    sma_series = closes.rolling(window=sma_period).mean()
    trend_sma_series = closes.rolling(window=trend_sma_period).mean() if trend_sma_period else None
    rsi_series = _rsi_series(closes, rsi_period) if rsi_period else None
    macd_series = _macd_histogram_series(closes) if use_macd else None
    fib_series = _fib_support_series(closes, fib_lookback, fib_level) if fib_lookback else None

    state = {"in_position": False}
    trades = []
    open_trade = None

    for i in range(sma_period, len(closes)):
        sma = sma_series.iloc[i]
        if pd.isna(sma):
            continue

        trend_sma = None
        if trend_sma_series is not None:
            if pd.isna(trend_sma_series.iloc[i]):
                continue
            trend_sma = float(trend_sma_series.iloc[i])

        rsi = None
        if rsi_series is not None:
            if pd.isna(rsi_series.iloc[i]):
                continue
            rsi = float(rsi_series.iloc[i])

        macd_histogram = None
        if macd_series is not None:
            if pd.isna(macd_series.iloc[i]):
                continue
            macd_histogram = float(macd_series.iloc[i])

        fib_support_level = None
        if fib_series is not None:
            if pd.isna(fib_series.iloc[i]):
                continue
            fib_support_level = float(fib_series.iloc[i])

        current_price = float(closes.iloc[i])

        result = strategy.generate_spot_signal(
            ticker=ticker,
            current_price=current_price,
            sma=float(sma),
            state=state,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            trend_sma=trend_sma,
            profit_target_dollars=profit_target_dollars,
            rsi=rsi,
            rsi_overbought=rsi_overbought,
            macd_histogram=macd_histogram,
            fib_support_level=fib_support_level,
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
    filters = []
    if config.get("trend_sma_period"):
        filters.append(f"trend_sma={config['trend_sma_period']}")
    if config.get("rsi_period"):
        filters.append(f"rsi={config['rsi_period']}(<{config['rsi_overbought']})")
    if config.get("use_macd"):
        filters.append("macd")
    if config.get("fib_lookback"):
        filters.append(f"fib={config['fib_lookback']}@{config['fib_level']}")
    filters_str = "  " + "  ".join(filters) if filters else ""
    print(
        f"ticker={config['yfinance_ticker']}  interval={config['interval']}  days={config['days']}  "
        f"sma_period={config['sma_period']}  target={config['profit_target_pct']:.2%}  "
        f"stop_loss={config['stop_loss_pct']:.2%}{filters_str}"
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
    parser.add_argument("--instrument", choices=["NIFTY", "BTC", "USDINR", "BOTH"], default="BOTH")
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
    parser.add_argument(
        "--target-dollars",
        type=float,
        default=None,
        help="Flat dollar profit target instead of --target-pct (spot instruments only, e.g. BTC)",
    )
    parser.add_argument(
        "--rsi-period",
        type=int,
        default=None,
        help="Reject BUY signals when RSI is at/above --rsi-overbought (momentum filter)",
    )
    parser.add_argument("--rsi-overbought", type=float, default=70.0)
    parser.add_argument(
        "--macd",
        action="store_true",
        help="Reject BUY signals unless the MACD histogram is positive (bullish momentum)",
    )
    parser.add_argument(
        "--fib-lookback",
        type=int,
        default=None,
        help="Reject BUY signals when price is below the Fibonacci support level over this many candles",
    )
    parser.add_argument(
        "--fib-level",
        choices=list(_FIB_FRACTIONS.keys()),
        default="0.5",
        help="Which Fibonacci retracement level counts as support (default 0.5)",
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
            "rsi_period": args.rsi_period,
            "rsi_overbought": args.rsi_overbought,
            "use_macd": args.macd,
            "fib_lookback": args.fib_lookback,
            "fib_level": args.fib_level,
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
                profit_target_dollars=args.target_dollars,
                rsi_period=args.rsi_period,
                rsi_overbought=args.rsi_overbought,
                use_macd=args.macd,
                fib_lookback=args.fib_lookback,
                fib_level=args.fib_level,
            )
            print_report(instrument, config, stats)
        except Exception as exc:  # noqa: BLE001
            print(f"\n=== {instrument} backtest FAILED: {exc} ===")


if __name__ == "__main__":
    main()
