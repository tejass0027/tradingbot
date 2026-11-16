"""Entry point: wires together data_client, analyzer, strategy, and alerter
into a loop that checks each enabled instrument for a signal every
CHECK_INTERVAL_SECONDS.
"""

import datetime as dt
import logging
import os
import signal
import sys
import time
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

import alerter
import analyzer
import data_client
import strategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

IST = ZoneInfo("Asia/Kolkata")
NSE_MARKET_OPEN = dt.time(9, 15)
NSE_MARKET_CLOSE = dt.time(15, 30)


def is_nse_market_open(now: dt.datetime | None = None) -> bool:
    """Check whether NSE's cash/derivatives market is currently open.

    Covers weekday + trading-hours (09:15-15:30 IST) only. Does NOT account
    for NSE trading holidays (Diwali, Republic Day, etc.) - on a holiday
    this will still report "open" and the check will just work off whatever
    stale data yfinance/NSE happen to serve, same as any other closed day.
    """
    now = now or dt.datetime.now(IST)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return NSE_MARKET_OPEN <= now.time() <= NSE_MARKET_CLOSE


def is_forex_market_open(now: dt.datetime | None = None) -> bool:
    """Check whether the forex market (USD/INR included) is open.

    Real forex trading runs continuously from Sunday ~17:00 ET to Friday
    ~17:00 ET. This approximates that as "closed all day Saturday and
    Sunday, open otherwise" - same level of simplification already accepted
    for NSE's holiday calendar (see is_nse_market_open) rather than modeling
    the exact open/close instants.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.weekday() < 5  # Saturday=5, Sunday=6


def _fetch_nifty_data(config: dict) -> tuple:
    history = data_client.get_price_history(config["yfinance_ticker"])
    option_data = data_client.get_option_chain(
        symbol=config["nse_symbol"],
        min_days_to_expiry=config["min_days_to_expiry"],
        max_days_to_expiry=config["max_days_to_expiry"],
    )
    return history, option_data


def _fetch_btc_data(config: dict):
    return data_client.get_price_history(config["btc_yfinance_ticker"])


def _fetch_usdinr_data(config: dict):
    return data_client.get_price_history(config["usdinr_yfinance_ticker"])


# Each instrument plugs a data-fetch function and its own state file into the
# shared strategy -> alert pipeline in run_check(). "options" mode looks up
# an ATM call and trades that; "spot" mode trades the underlying price directly.
INSTRUMENTS = [
    {
        "key": "NIFTY",
        "mode": "options",
        "enabled_config_key": "enable_nifty",
        "currency_symbol": "₹",
        "state_file": os.path.join(_BASE_DIR, "state_nifty.json"),
        "fetch": _fetch_nifty_data,
        "is_market_open": is_nse_market_open,
        "risk_free_rate_key": "risk_free_rate",
        "profit_target_pct_key": "profit_target_pct",
        "stop_loss_pct_key": "stop_loss_pct",
    },
    {
        "key": "BTC",
        "mode": "spot",
        "enabled_config_key": "enable_btc",
        "currency_symbol": "$",
        "state_file": os.path.join(_BASE_DIR, "state_btc.json"),
        "fetch": _fetch_btc_data,
        "is_market_open": None,  # crypto trades 24/7
        "profit_target_pct_key": "btc_profit_target_pct",
        "stop_loss_pct_key": "btc_stop_loss_pct",
        "profit_target_dollars_key": "btc_profit_target_dollars",
        "use_macd_filter_key": "btc_use_macd_filter",
        "fib_lookback_key": "btc_fib_lookback",
        "fib_level_key": "btc_fib_level",
        "allow_short_key": "btc_allow_short",
        "short_trend_sma_period_key": "btc_short_trend_sma_period",
    },
    {
        "key": "USDINR",
        "mode": "spot",
        "enabled_config_key": "enable_usdinr",
        "currency_symbol": "₹",
        "state_file": os.path.join(_BASE_DIR, "state_usdinr.json"),
        "fetch": _fetch_usdinr_data,
        "is_market_open": is_forex_market_open,
        "profit_target_pct_key": "usdinr_profit_target_pct",
        "stop_loss_pct_key": "usdinr_stop_loss_pct",
        "trend_sma_period_key": "usdinr_trend_sma_period",
        "use_macd_filter_key": "usdinr_use_macd_filter",
        "fib_lookback_key": "usdinr_fib_lookback",
        "fib_level_key": "usdinr_fib_level",
    },
]


def load_config() -> dict:
    """Load configuration from real environment variables first, falling back
    to a local .env file, then to defaults.

    Checking os.environ first (not just the .env file) matters for Docker
    (`--env-file`) and GitHub Actions (repo secrets) deployments, which
    inject config as real process environment variables rather than writing
    an actual .env file into the runtime.
    """
    env = dotenv_values(".env")

    def _get(key: str, default: str) -> str:
        value = os.environ.get(key) or env.get(key)
        return value if value not in (None, "") else default

    def _get_bool(key: str, default: bool) -> bool:
        return _get(key, str(default)).strip().lower() in ("1", "true", "yes", "on")

    def _get_optional_float(key: str) -> float | None:
        value = os.environ.get(key) or env.get(key)
        return float(value) if value not in (None, "") else None

    return {
        "discord_webhook_url": _get("DISCORD_WEBHOOK_URL", ""),
        "nse_symbol": _get("NSE_SYMBOL", "NIFTY"),
        "yfinance_ticker": _get("YFINANCE_TICKER", "^NSEI"),
        "btc_yfinance_ticker": _get("BTC_YFINANCE_TICKER", "BTC-USD"),
        "usdinr_yfinance_ticker": _get("USDINR_YFINANCE_TICKER", "USDINR=X"),
        "enable_nifty": _get_bool("ENABLE_NIFTY", True),
        "enable_btc": _get_bool("ENABLE_BTC", True),
        "enable_usdinr": _get_bool("ENABLE_USDINR", True),
        "risk_free_rate": float(_get("RISK_FREE_RATE", "0.07")),
        "check_interval_seconds": int(_get("CHECK_INTERVAL_SECONDS", "300")),
        "min_days_to_expiry": int(_get("MIN_DAYS_TO_EXPIRY", "7")),
        "max_days_to_expiry": int(_get("MAX_DAYS_TO_EXPIRY", "30")),
        "profit_target_pct": float(_get("PROFIT_TARGET_PCT", "0.20")),
        "stop_loss_pct": float(_get("STOP_LOSS_PCT", "0.30")),
        "btc_profit_target_pct": float(_get("BTC_PROFIT_TARGET_PCT", "0.03")),
        "btc_stop_loss_pct": float(_get("BTC_STOP_LOSS_PCT", "0.02")),
        "btc_profit_target_dollars": _get_optional_float("BTC_PROFIT_TARGET_DOLLARS"),
        "sma_period": int(_get("SMA_PERIOD", "20")),
        "trend_sma_period": int(_get("TREND_SMA_PERIOD", "50")),
        "heartbeat_interval_seconds": int(_get("HEARTBEAT_INTERVAL_SECONDS", "3600")),
        # Momentum filters for BTC, validated via backtest.py (100 trades over
        # 60 days, 33% win rate, +10.26% compounded return vs. +1.23% baseline,
        # confirmed positive across both halves of the window independently).
        "btc_use_macd_filter": _get_bool("BTC_USE_MACD_FILTER", True),
        "btc_fib_lookback": int(_get("BTC_FIB_LOOKBACK", "50")),
        "btc_fib_level": _get("BTC_FIB_LEVEL", "0.618"),
        # BTC shorts: the mirror-image trade (cross below the SMA). Its trend
        # filter has its own period because the long side's is disabled -
        # the short combo was validated with trend + MACD + Fib together.
        "btc_allow_short": _get_bool("BTC_ALLOW_SHORT", True),
        "btc_short_trend_sma_period": int(_get("BTC_SHORT_TREND_SMA_PERIOD", "50")),
        # USD/INR: same filter stack as BTC (trend SMA + MACD + Fibonacci),
        # validated via backtest.py (21 trades over 60 days, 42.9% win rate,
        # +0.59% compounded return, consistent across both halves of the
        # window - a much smaller/less certain edge than BTC's, since forex
        # moves are far smaller than crypto's, but the bare crossover alone
        # was a loser here).
        "usdinr_profit_target_pct": float(_get("USDINR_PROFIT_TARGET_PCT", "0.004")),
        "usdinr_stop_loss_pct": float(_get("USDINR_STOP_LOSS_PCT", "0.002")),
        "usdinr_trend_sma_period": int(_get("USDINR_TREND_SMA_PERIOD", "50")),
        "usdinr_use_macd_filter": _get_bool("USDINR_USE_MACD_FILTER", True),
        "usdinr_fib_lookback": int(_get("USDINR_FIB_LOOKBACK", "50")),
        "usdinr_fib_level": _get("USDINR_FIB_LEVEL", "0.618"),
    }


def _trend_sma(closes, period: int, instrument_key: str) -> float | None:
    """Compute the trend-confirmation SMA, or None if disabled/not enough history yet."""
    if period <= 0:
        return None
    if len(closes) < period:
        logger.warning(
            "[%s] Only %d price points available, need %d for trend SMA - filter disabled this cycle",
            instrument_key,
            len(closes),
            period,
        )
        return None
    return analyzer.calculate_sma(closes, period=period)


def run_check(config: dict, instrument: dict) -> tuple[str, str]:
    """Run a single fetch -> analyze -> strategy -> alert cycle for one instrument.

    Returns (action, reason) for the caller to fold into a heartbeat summary.
    """
    state = strategy.load_state(instrument["state_file"])
    profit_target_pct = config[instrument["profit_target_pct_key"]]
    stop_loss_pct = config[instrument["stop_loss_pct_key"]]
    trend_sma_period = config[instrument.get("trend_sma_period_key") or "trend_sma_period"]

    if instrument["mode"] == "options":
        history, option_data = instrument["fetch"](config)
        closes = history["Close"]
        sma = analyzer.calculate_sma(closes, period=config["sma_period"])
        current_price = float(closes.iloc[-1])

        enriched_chain = analyzer.enrich_chain_with_greeks(
            option_data["chain"],
            spot_price=current_price,
            days_to_expiry=option_data["days_to_expiry"],
            risk_free_rate=config[instrument["risk_free_rate_key"]],
        )
        atm_call = analyzer.find_atm_call(enriched_chain, spot_price=current_price)
        trend_sma = _trend_sma(closes, trend_sma_period, instrument["key"])

        result = strategy.generate_signal(
            ticker=instrument["key"],
            current_price=current_price,
            sma=sma,
            atm_call=atm_call,
            expiry_date=option_data["expiry_date"],
            state=state,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            currency_symbol=instrument["currency_symbol"],
            trend_sma=trend_sma,
        )
    else:  # spot
        history = instrument["fetch"](config)
        closes = history["Close"]
        sma = analyzer.calculate_sma(closes, period=config["sma_period"])
        current_price = float(closes.iloc[-1])
        trend_sma = _trend_sma(closes, trend_sma_period, instrument["key"])
        profit_target_dollars = config.get(instrument.get("profit_target_dollars_key", ""))

        macd_histogram = None
        if config.get(instrument.get("use_macd_filter_key", "")):
            try:
                macd_histogram = analyzer.calculate_macd(closes)["histogram"]
            except ValueError as exc:
                logger.warning("[%s] MACD filter disabled this cycle: %s", instrument["key"], exc)

        allow_short = bool(config.get(instrument.get("allow_short_key", "")))
        short_trend_sma = None
        if allow_short:
            short_trend_sma = _trend_sma(closes, config[instrument["short_trend_sma_period_key"]], instrument["key"])

        fib_support_level = None
        fib_resistance_level = None
        fib_lookback_key = instrument.get("fib_lookback_key", "")
        if fib_lookback_key and config.get(fib_lookback_key):
            try:
                fib_levels = analyzer.calculate_fibonacci_levels(closes, lookback=config[fib_lookback_key])
                fib_support_level = fib_levels[config[instrument["fib_level_key"]]]
                # Mirror of the support level: the same fraction of the swing
                # range measured up from the low instead of down from the high.
                swing_low, swing_high = fib_levels["swing_low"], fib_levels["swing_high"]
                fib_resistance_level = swing_low + (swing_high - swing_low) * float(config[instrument["fib_level_key"]])
            except ValueError as exc:
                logger.warning("[%s] Fibonacci filter disabled this cycle: %s", instrument["key"], exc)

        result = strategy.generate_spot_signal(
            ticker=instrument["key"],
            current_price=current_price,
            sma=sma,
            state=state,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            currency_symbol=instrument["currency_symbol"],
            trend_sma=trend_sma,
            profit_target_dollars=profit_target_dollars,
            macd_histogram=macd_histogram,
            fib_support_level=fib_support_level,
            allow_short=allow_short,
            short_trend_sma=short_trend_sma,
            fib_resistance_level=fib_resistance_level,
        )

    strategy.save_state(result["state"], instrument["state_file"])

    signal = result["signal"]
    logger.info("[%s] Signal: %s (%s)", instrument["key"], signal["action"], signal.get("reason"))

    # SELL signals still close the tracked position (so the next BUY can fire)
    # but are deliberately not announced - alerts are entries only, each
    # carrying its own target and stop-loss.
    if signal["action"] in ("BUY_CALL", "BUY", "SHORT"):
        sent = alerter.send_discord_alert(signal, config["discord_webhook_url"])
        if sent:
            logger.info("[%s] Alert sent to Discord", instrument["key"])
        else:
            logger.warning("[%s] Alert was not sent (see previous log line for details)", instrument["key"])

    return signal["action"], signal.get("reason", "")


def _run_all_checks(config: dict, active_instruments: list) -> list[str]:
    """Run one round of checks and return a status line per instrument, for
    the heartbeat summary (e.g. "NIFTY: market closed", "BTC: HOLD - ...").
    """
    status_lines = []
    for instrument in active_instruments:
        try:
            market_open_check = instrument["is_market_open"]
            if market_open_check is None or market_open_check():
                action, reason = run_check(config, instrument)
                status_lines.append(f"{instrument['key']}: {action} - {reason}")
            else:
                logger.info("[%s] Market is closed - skipping check", instrument["key"])
                status_lines.append(f"{instrument['key']}: market closed")
        except Exception as exc:  # noqa: BLE001 - keep going across any single-instrument failure
            logger.error("[%s] Check cycle failed: %s", instrument["key"], exc, exc_info=True)
            status_lines.append(f"{instrument['key']}: check failed ({exc})")
    return status_lines


def main() -> None:
    once = "--once" in sys.argv

    config = load_config()
    if not config["discord_webhook_url"]:
        logger.warning("DISCORD_WEBHOOK_URL is not set — alerts will be skipped. Copy .env.example to .env and fill it in.")

    active_instruments = [i for i in INSTRUMENTS if config[i["enabled_config_key"]]]
    if not active_instruments:
        logger.error("No instruments enabled (check ENABLE_NIFTY / ENABLE_BTC / ENABLE_USDINR in .env) — nothing to do.")
        return

    if once:
        # Single-shot mode: run one round of checks and exit, instead of
        # looping forever. Used by schedulers that invoke this process
        # periodically themselves (e.g. a GitHub Actions cron workflow)
        # rather than keeping a long-running process alive.
        logger.info("Running a single check for %s (--once)", ", ".join(i["key"] for i in active_instruments))
        _run_all_checks(config, active_instruments)
        return

    logger.info(
        "Starting options signal agent for %s (checking every %ds)",
        ", ".join(i["key"] for i in active_instruments),
        config["check_interval_seconds"],
    )

    # Route SIGTERM (a normal "stop this process" request, e.g. `kill` without
    # -9, or a service manager stopping the process) through the same clean
    # shutdown path as Ctrl+C, so the offline message gets a chance to send.
    # A forced kill (task manager "End task", `taskkill /F`, power loss)
    # cannot be intercepted by any program - no message can be sent then.
    def _handle_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)

    instrument_names = ", ".join(i["key"] for i in active_instruments)
    if alerter.send_message(f"### \U0001F7E2 Bot is Online\nMonitoring: {instrument_names}", config["discord_webhook_url"]):
        logger.info("Online message sent to Discord")

    last_heartbeat = 0.0
    try:
        while True:
            status_lines = _run_all_checks(config, active_instruments)

            now = time.time()
            if now - last_heartbeat >= config["heartbeat_interval_seconds"]:
                sent = alerter.send_message(alerter.format_heartbeat_message(status_lines), config["discord_webhook_url"])
                if sent:
                    logger.info("Heartbeat sent to Discord")
                last_heartbeat = now

            time.sleep(config["check_interval_seconds"])
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    finally:
        if alerter.send_message("### \U0001F534 Bot is Offline", config["discord_webhook_url"]):
            logger.info("Offline message sent to Discord")


if __name__ == "__main__":
    main()
