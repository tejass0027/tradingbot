"""Signal generation logic: decides BUY_CALL / SELL / HOLD.

Position state (whether we're currently "in" a signalled trade) is persisted
to a small JSON file so the bot doesn't forget its position if it restarts.
"""

import datetime as dt
import json
import logging
import os

logger = logging.getLogger(__name__)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_FILE = os.path.join(_BASE_DIR, "state.json")

_FLAT_STATE = {"in_position": False}


def load_state(state_file: str = DEFAULT_STATE_FILE) -> dict:
    """Load the current position state from disk, defaulting to flat/no-position.

    Each tracked instrument (NIFTY, BTC, ...) uses its own state file so
    positions don't collide with each other.
    """
    if not os.path.exists(state_file):
        return dict(_FLAT_STATE)
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read state file %s, starting flat: %s", state_file, exc)
        return dict(_FLAT_STATE)


def save_state(state: dict, state_file: str = DEFAULT_STATE_FILE) -> None:
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
    except OSError as exc:
        logger.warning("Failed to persist state file %s: %s", state_file, exc)


def generate_signal(
    ticker: str,
    current_price: float,
    sma: float,
    atm_call: dict | None,
    expiry_date: dt.date,
    state: dict,
    profit_target_pct: float = 0.20,
    stop_loss_pct: float = 0.30,
    currency_symbol: str = "₹",
    trend_sma: float | None = None,
) -> dict:
    """Evaluate the strategy rules and return a signal dict.

    Rules:
      - BUY_CALL: not currently in a position, price crosses above the SMA,
        and an ATM call (Delta ~= 0.50) is available. The signal carries a
        target price (entry + profit_target_pct) and a stop-loss price
        (entry - stop_loss_pct) for the trader to set on the option premium.
        If `trend_sma` is given, the crossover is only accepted when price is
        also above it (trend-confirmation filter - rejects crossovers that
        happen against the broader trend, e.g. in a choppy/sideways market).
      - SELL: currently in a position, and any of: the option hits the
        profit target, the option hits the stop-loss, or price crosses back
        below the SMA.
      - HOLD: neither condition is met.

    Crossover detection compares the current above/below-SMA state against
    `state["was_above_sma"]` (persisted from the previous check), NOT against
    the previous candle in a freshly fetched price history. This matters
    because checks don't always run exactly on the candle interval (e.g. a
    scheduler like GitHub Actions cron can run 15-25+ minutes apart instead
    of every 5) - comparing only the two most-recently-fetched candles would
    silently miss a crossover that happened between two checks, since by the
    next check a newer candle has already taken its place. Persisted-state
    comparison catches the transition regardless of how much time passed.

    Mutates and returns the updated `state` dict alongside the signal so the
    caller can persist it via save_state().
    """
    is_above_sma = current_price > sma
    was_above_sma = state.get("was_above_sma")
    crossed_above = was_above_sma is False and is_above_sma
    crossed_below = was_above_sma is True and not is_above_sma
    trend_ok = trend_sma is None or current_price > trend_sma

    signal = {"action": "HOLD", "ticker": ticker, "reason": "No trigger condition met"}

    if not state.get("in_position"):
        if crossed_above and atm_call is not None and trend_ok:
            entry_price = atm_call["ltp"]
            target_price = round(entry_price * (1 + profit_target_pct), 2)
            stop_loss_price = round(entry_price * (1 - stop_loss_pct), 2)
            signal = {
                "action": "BUY_CALL",
                "ticker": ticker,
                "currency_symbol": currency_symbol,
                "strike": atm_call["strike"],
                "expiry": expiry_date.isoformat(),
                "delta": atm_call["delta"],
                "theta": atm_call["theta"],
                "iv": atm_call["iv"],
                "entry_price": entry_price,
                "target_price": target_price,
                "stop_loss_price": stop_loss_price,
                "underlying_price": current_price,
                "sma": sma,
                "reason": "Underlying crossed above SMA and found ATM call near 0.50 delta",
            }
            state = {
                "in_position": True,
                "strike": atm_call["strike"],
                "expiry": expiry_date.isoformat(),
                "entry_price": entry_price,
                "target_price": target_price,
                "stop_loss_price": stop_loss_price,
                "entry_time": dt.datetime.now().isoformat(),
            }
        elif crossed_above and not trend_ok:
            signal["reason"] = "Underlying crossed above SMA but rejected by trend filter"
        elif crossed_above and atm_call is None:
            signal["reason"] = "Underlying crossed above SMA but no ATM call near 0.50 delta was found"
    else:
        entry_price = state.get("entry_price")
        current_option_price = atm_call["ltp"] if atm_call is not None else None
        hit_profit_target = (
            current_option_price is not None
            and entry_price
            and current_option_price >= entry_price * (1 + profit_target_pct)
        )
        hit_stop_loss = (
            current_option_price is not None
            and entry_price
            and current_option_price <= entry_price * (1 - stop_loss_pct)
        )

        if hit_profit_target or hit_stop_loss or crossed_below:
            if hit_profit_target:
                reason = "Option reached profit target"
            elif hit_stop_loss:
                reason = "Option hit stop-loss"
            else:
                reason = "Underlying crossed below SMA"
            signal = {
                "action": "SELL",
                "ticker": ticker,
                "currency_symbol": currency_symbol,
                "strike": state.get("strike"),
                "expiry": state.get("expiry"),
                "entry_price": entry_price,
                "target_price": state.get("target_price"),
                "stop_loss_price": state.get("stop_loss_price"),
                "exit_price": current_option_price,
                "underlying_price": current_price,
                "sma": sma,
                "reason": reason,
            }
            state = dict(_FLAT_STATE)

    state = dict(state)
    state["was_above_sma"] = is_above_sma
    return {"signal": signal, "state": state}


def generate_spot_signal(
    ticker: str,
    current_price: float,
    sma: float,
    state: dict,
    profit_target_pct: float,
    stop_loss_pct: float,
    currency_symbol: str = "$",
    trend_sma: float | None = None,
) -> dict:
    """Evaluate the strategy rules for plain spot trading (no options chain).

    Rules:
      - BUY: not currently in a position, and price crosses above the SMA.
        The signal carries a target price (entry + profit_target_pct) and a
        stop-loss price (entry - stop_loss_pct) on the traded price itself.
        If `trend_sma` is given, the crossover is only accepted when price is
        also above it (trend-confirmation filter - rejects crossovers that
        happen against the broader trend, e.g. in a choppy/sideways market).
      - SELL: currently in a position, and any of: price hits the target,
        price hits the stop-loss, or price crosses back below the SMA.
      - HOLD: neither condition is met.

    Crossover detection compares the current above/below-SMA state against
    `state["was_above_sma"]` (persisted from the previous check), NOT against
    the previous candle in a freshly fetched price history - see
    generate_signal's docstring for why this matters when checks don't run
    on a tight, regular schedule (e.g. GitHub Actions cron).

    Mutates and returns the updated `state` dict alongside the signal so the
    caller can persist it via save_state().
    """
    is_above_sma = current_price > sma
    was_above_sma = state.get("was_above_sma")
    crossed_above = was_above_sma is False and is_above_sma
    crossed_below = was_above_sma is True and not is_above_sma
    trend_ok = trend_sma is None or current_price > trend_sma

    signal = {"action": "HOLD", "ticker": ticker, "reason": "No trigger condition met"}

    if not state.get("in_position"):
        if crossed_above and trend_ok:
            entry_price = current_price
            target_price = round(entry_price * (1 + profit_target_pct), 2)
            stop_loss_price = round(entry_price * (1 - stop_loss_pct), 2)
            signal = {
                "action": "BUY",
                "ticker": ticker,
                "currency_symbol": currency_symbol,
                "entry_price": entry_price,
                "target_price": target_price,
                "stop_loss_price": stop_loss_price,
                "sma": sma,
                "reason": "Price crossed above SMA",
            }
            state = {
                "in_position": True,
                "entry_price": entry_price,
                "target_price": target_price,
                "stop_loss_price": stop_loss_price,
                "entry_time": dt.datetime.now().isoformat(),
            }
        elif crossed_above and not trend_ok:
            signal["reason"] = "Price crossed above SMA but rejected by trend filter"
    else:
        entry_price = state.get("entry_price")
        target_price = state.get("target_price")
        stop_loss_price = state.get("stop_loss_price")
        hit_profit_target = target_price is not None and current_price >= target_price
        hit_stop_loss = stop_loss_price is not None and current_price <= stop_loss_price

        if hit_profit_target or hit_stop_loss or crossed_below:
            if hit_profit_target:
                reason = "Price reached profit target"
            elif hit_stop_loss:
                reason = "Price hit stop-loss"
            else:
                reason = "Price crossed below SMA"
            signal = {
                "action": "SELL",
                "ticker": ticker,
                "currency_symbol": currency_symbol,
                "entry_price": entry_price,
                "target_price": target_price,
                "stop_loss_price": stop_loss_price,
                "exit_price": current_price,
                "sma": sma,
                "reason": reason,
            }
            state = dict(_FLAT_STATE)

    state = dict(state)
    state["was_above_sma"] = is_above_sma
    return {"signal": signal, "state": state}
