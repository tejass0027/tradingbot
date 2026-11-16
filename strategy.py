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
    profit_target_dollars: float | None = None,
    rsi: float | None = None,
    rsi_overbought: float = 70.0,
    macd_histogram: float | None = None,
    fib_support_level: float | None = None,
    allow_short: bool = False,
    short_trend_sma: float | None = None,
    fib_resistance_level: float | None = None,
) -> dict:
    """Evaluate the strategy rules for plain spot trading (no options chain).

    With `allow_short=True` the mirror-image trade is also taken: a cross
    below the SMA opens a SHORT (target below entry, stop-loss above) when
    the mirrored filters agree - price below `short_trend_sma`, RSI above
    100 - `rsi_overbought`, MACD histogram negative, and price at or below
    `fib_resistance_level`. A position always closes on the opposite SMA
    cross, and if the other side's filters pass at that same cross the
    position flips straight into the new side (the signal returned is the
    new entry, not the exit) - otherwise the cross that closes a trade would
    be consumed and the opposite entry could never fire.

    Rules:
      - BUY: not currently in a position, and price crosses above the SMA.
        The signal carries a target price and a stop-loss price on the
        traded price itself. The stop-loss is always entry * (1 -
        stop_loss_pct). The target is entry + profit_target_dollars when
        `profit_target_dollars` is given (a flat dollar profit regardless of
        entry price), otherwise entry * (1 + profit_target_pct).

        The crossover is only accepted if every filter that's actually
        supplied agrees (each defaults to None/disabled independently):
          - `trend_sma`: price must also be above this longer/slower SMA
            (rejects crossovers against the broader trend).
          - `rsi` + `rsi_overbought`: rejects the entry if RSI is already at
            or above the overbought threshold (momentum may be exhausted).
          - `macd_histogram`: rejects the entry unless positive (MACD line
            above its signal line - bullish momentum agrees with the
            crossover).
          - `fib_support_level`: rejects the entry unless price is at or
            above this Fibonacci retracement level (a common proxy for "the
            recent uptrend structure is still intact").
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

    long_checks = {
        "trend": trend_sma is None or current_price > trend_sma,
        "RSI overbought": rsi is None or rsi < rsi_overbought,
        "MACD bearish": macd_histogram is None or macd_histogram > 0,
        "below Fib support": fib_support_level is None or current_price >= fib_support_level,
    }
    short_checks = {
        "trend": short_trend_sma is None or current_price < short_trend_sma,
        "RSI oversold": rsi is None or rsi > 100.0 - rsi_overbought,
        "MACD bullish": macd_histogram is None or macd_histogram < 0,
        "above Fib resistance": fib_resistance_level is None or current_price <= fib_resistance_level,
    }
    long_ok = all(long_checks.values())
    short_ok = allow_short and all(short_checks.values())

    entry_args = (ticker, current_price, sma, currency_symbol, profit_target_pct, stop_loss_pct, profit_target_dollars)
    signal = {"action": "HOLD", "ticker": ticker, "reason": "No trigger condition met"}

    if not state.get("in_position"):
        if crossed_above and long_ok:
            signal, state = _spot_entry("long", *entry_args)
        elif crossed_below and short_ok:
            signal, state = _spot_entry("short", *entry_args)
        elif crossed_above:
            failed = [name for name, ok in long_checks.items() if not ok]
            signal["reason"] = f"Price crossed above SMA but rejected by filter(s): {', '.join(failed)}"
        elif crossed_below and allow_short:
            failed = [name for name, ok in short_checks.items() if not ok]
            signal["reason"] = f"Price crossed below SMA but rejected by short filter(s): {', '.join(failed)}"
    else:
        side = state.get("side", "long")
        entry_price = state.get("entry_price")
        target_price = state.get("target_price")
        stop_loss_price = state.get("stop_loss_price")
        if side == "long":
            hit_profit_target = target_price is not None and current_price >= target_price
            hit_stop_loss = stop_loss_price is not None and current_price <= stop_loss_price
            sma_reversed = crossed_below
            flip_ok = short_ok
        else:
            hit_profit_target = target_price is not None and current_price <= target_price
            hit_stop_loss = stop_loss_price is not None and current_price >= stop_loss_price
            sma_reversed = crossed_above
            flip_ok = long_ok

        if hit_profit_target or hit_stop_loss or sma_reversed:
            if sma_reversed and flip_ok:
                signal, state = _spot_entry("short" if side == "long" else "long", *entry_args)
            else:
                if hit_profit_target:
                    reason = "Price reached profit target"
                elif hit_stop_loss:
                    reason = "Price hit stop-loss"
                else:
                    reason = "Price crossed below SMA" if side == "long" else "Price crossed above SMA"
                signal = {
                    "action": "SELL" if side == "long" else "COVER",
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


def _spot_entry(
    side: str,
    ticker: str,
    price: float,
    sma: float,
    currency_symbol: str,
    profit_target_pct: float,
    stop_loss_pct: float,
    profit_target_dollars: float | None,
) -> tuple[dict, dict]:
    """Build the entry signal and the position state for a new spot trade.

    A long targets above entry with its stop below; a short is the mirror
    image (target below entry, stop above).
    """
    direction = 1 if side == "long" else -1
    if profit_target_dollars is not None:
        target_price = round(price + direction * profit_target_dollars, 2)
    else:
        target_price = round(price * (1 + direction * profit_target_pct), 2)
    stop_loss_price = round(price * (1 - direction * stop_loss_pct), 2)

    signal = {
        "action": "BUY" if side == "long" else "SHORT",
        "ticker": ticker,
        "currency_symbol": currency_symbol,
        "entry_price": price,
        "target_price": target_price,
        "stop_loss_price": stop_loss_price,
        "sma": sma,
        "reason": "Price crossed above SMA" if side == "long" else "Price crossed below SMA",
    }
    state = {
        "in_position": True,
        "side": side,
        "entry_price": price,
        "target_price": target_price,
        "stop_loss_price": stop_loss_price,
        "entry_time": dt.datetime.now().isoformat(),
    }
    return signal, state
