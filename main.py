"""Entry point: wires together data_client, analyzer, strategy, and alerter
into a loop that checks each enabled instrument for a signal every
CHECK_INTERVAL_SECONDS.
"""

import datetime as dt
import logging
import os
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

    return {
        "discord_webhook_url": _get("DISCORD_WEBHOOK_URL", ""),
        "nse_symbol": _get("NSE_SYMBOL", "NIFTY"),
        "yfinance_ticker": _get("YFINANCE_TICKER", "^NSEI"),
        "btc_yfinance_ticker": _get("BTC_YFINANCE_TICKER", "BTC-USD"),
        "enable_nifty": _get_bool("ENABLE_NIFTY", True),
        "enable_btc": _get_bool("ENABLE_BTC", True),
        "risk_free_rate": float(_get("RISK_FREE_RATE", "0.07")),
        "check_interval_seconds": int(_get("CHECK_INTERVAL_SECONDS", "300")),
        "min_days_to_expiry": int(_get("MIN_DAYS_TO_EXPIRY", "7")),
        "max_days_to_expiry": int(_get("MAX_DAYS_TO_EXPIRY", "30")),
        "profit_target_pct": float(_get("PROFIT_TARGET_PCT", "0.20")),
        "stop_loss_pct": float(_get("STOP_LOSS_PCT", "0.30")),
        "btc_profit_target_pct": float(_get("BTC_PROFIT_TARGET_PCT", "0.03")),
        "btc_stop_loss_pct": float(_get("BTC_STOP_LOSS_PCT", "0.02")),
        "sma_period": int(_get("SMA_PERIOD", "20")),
        "trend_sma_period": int(_get("TREND_SMA_PERIOD", "50")),
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


def run_check(config: dict, instrument: dict) -> None:
    """Run a single fetch -> analyze -> strategy -> alert cycle for one instrument."""
    state = strategy.load_state(instrument["state_file"])
    profit_target_pct = config[instrument["profit_target_pct_key"]]
    stop_loss_pct = config[instrument["stop_loss_pct_key"]]

    if instrument["mode"] == "options":
        history, option_data = instrument["fetch"](config)
        closes = history["Close"]
        sma = analyzer.calculate_sma(closes, period=config["sma_period"])
        prev_sma = analyzer.calculate_sma(closes.iloc[:-1], period=config["sma_period"])
        current_price = float(closes.iloc[-1])
        prev_price = float(closes.iloc[-2])

        enriched_chain = analyzer.enrich_chain_with_greeks(
            option_data["chain"],
            spot_price=current_price,
            days_to_expiry=option_data["days_to_expiry"],
            risk_free_rate=config[instrument["risk_free_rate_key"]],
        )
        atm_call = analyzer.find_atm_call(enriched_chain, spot_price=current_price)
        trend_sma = _trend_sma(closes, config["trend_sma_period"], instrument["key"])

        result = strategy.generate_signal(
            ticker=instrument["key"],
            current_price=current_price,
            sma=sma,
            prev_price=prev_price,
            prev_sma=prev_sma,
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
        prev_sma = analyzer.calculate_sma(closes.iloc[:-1], period=config["sma_period"])
        current_price = float(closes.iloc[-1])
        prev_price = float(closes.iloc[-2])
        trend_sma = _trend_sma(closes, config["trend_sma_period"], instrument["key"])

        result = strategy.generate_spot_signal(
            ticker=instrument["key"],
            current_price=current_price,
            sma=sma,
            prev_price=prev_price,
            prev_sma=prev_sma,
            state=state,
            profit_target_pct=profit_target_pct,
            stop_loss_pct=stop_loss_pct,
            currency_symbol=instrument["currency_symbol"],
            trend_sma=trend_sma,
        )

    strategy.save_state(result["state"], instrument["state_file"])

    signal = result["signal"]
    logger.info("[%s] Signal: %s (%s)", instrument["key"], signal["action"], signal.get("reason"))

    if signal["action"] in ("BUY_CALL", "BUY", "SELL"):
        sent = alerter.send_discord_alert(signal, config["discord_webhook_url"])
        if sent:
            logger.info("[%s] Alert sent to Discord", instrument["key"])
        else:
            logger.warning("[%s] Alert was not sent (see previous log line for details)", instrument["key"])


def _run_all_checks(config: dict, active_instruments: list) -> None:
    for instrument in active_instruments:
        try:
            market_open_check = instrument["is_market_open"]
            if market_open_check is None or market_open_check():
                run_check(config, instrument)
            else:
                logger.info("[%s] Market is closed - skipping check", instrument["key"])
        except Exception as exc:  # noqa: BLE001 - keep going across any single-instrument failure
            logger.error("[%s] Check cycle failed: %s", instrument["key"], exc, exc_info=True)


def main() -> None:
    once = "--once" in sys.argv

    config = load_config()
    if not config["discord_webhook_url"]:
        logger.warning("DISCORD_WEBHOOK_URL is not set — alerts will be skipped. Copy .env.example to .env and fill it in.")

    active_instruments = [i for i in INSTRUMENTS if config[i["enabled_config_key"]]]
    if not active_instruments:
        logger.error("No instruments enabled (check ENABLE_NIFTY / ENABLE_BTC in .env) — nothing to do.")
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

    while True:
        _run_all_checks(config, active_instruments)
        time.sleep(config["check_interval_seconds"])


if __name__ == "__main__":
    main()
