"""Technical indicators and options Greeks calculations."""

import datetime as dt
import logging

import pandas as pd

logger = logging.getLogger(__name__)


def calculate_sma(prices: pd.Series, period: int = 20) -> float:
    """Calculate the Simple Moving Average of the last `period` closes.

    Raises ValueError if there isn't enough data.
    """
    if len(prices) < period:
        raise ValueError(f"Need at least {period} price points for SMA, got {len(prices)}")
    return float(prices.tail(period).mean())


def calculate_rsi(prices: pd.Series, period: int = 14) -> float:
    """Calculate the Relative Strength Index (RSI) using Wilder's smoothing.

    RSI ranges 0-100: conventionally >70 is "overbought" (momentum may be
    exhausted), <30 is "oversold". Raises ValueError if there isn't enough data.
    """
    if len(prices) < period + 1:
        raise ValueError(f"Need at least {period + 1} price points for RSI, got {len(prices)}")

    delta = prices.diff()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    last_avg_gain = float(avg_gain.iloc[-1])
    last_avg_loss = float(avg_loss.iloc[-1])
    if last_avg_loss == 0:
        return 100.0 if last_avg_gain > 0 else 50.0

    rs = last_avg_gain / last_avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def calculate_macd(
    prices: pd.Series,
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> dict:
    """Calculate MACD: the difference between a fast and slow EMA, plus a
    signal line (EMA of that difference) and the histogram (their gap).

    Returns {"macd_line": float, "signal_line": float, "histogram": float}.
    A positive histogram (MACD line above signal line) is read as bullish
    momentum; negative is bearish. Raises ValueError if there isn't enough data.
    """
    if len(prices) < slow_period + signal_period:
        raise ValueError(
            f"Need at least {slow_period + signal_period} price points for MACD, got {len(prices)}"
        )

    ema_fast = prices.ewm(span=fast_period, adjust=False).mean()
    ema_slow = prices.ewm(span=slow_period, adjust=False).mean()
    macd_series = ema_fast - ema_slow
    signal_series = macd_series.ewm(span=signal_period, adjust=False).mean()

    macd_line = float(macd_series.iloc[-1])
    signal_line = float(signal_series.iloc[-1])
    return {"macd_line": macd_line, "signal_line": signal_line, "histogram": macd_line - signal_line}


def calculate_fibonacci_levels(prices: pd.Series, lookback: int = 50) -> dict:
    """Calculate Fibonacci retracement levels from the swing high/low over
    the last `lookback` candles.

    Returns the swing high/low plus the standard retracement levels (price
    points between them at 23.6%, 38.2%, 50%, 61.8%, 78.6% retraced from the
    high). These are commonly read as potential support levels in an
    uptrend - price holding above a level is taken as the trend still intact.
    Raises ValueError if there isn't enough data.
    """
    if len(prices) < lookback:
        raise ValueError(f"Need at least {lookback} price points for Fibonacci levels, got {len(prices)}")

    window = prices.tail(lookback)
    swing_high = float(window.max())
    swing_low = float(window.min())
    span = swing_high - swing_low

    return {
        "swing_high": swing_high,
        "swing_low": swing_low,
        "0.236": swing_high - span * 0.236,
        "0.382": swing_high - span * 0.382,
        "0.5": swing_high - span * 0.5,
        "0.618": swing_high - span * 0.618,
        "0.786": swing_high - span * 0.786,
    }


def enrich_chain_with_greeks(
    chain: pd.DataFrame,
    spot_price: float,
    days_to_expiry: int,
    risk_free_rate: float = 0.07,
) -> pd.DataFrame:
    """Add CE_delta and CE_theta columns to the option chain using Black-Scholes,
    driven by each strike's NSE-quoted implied volatility.

    Rows with missing price/IV data (illiquid strikes) get NaN Greeks and are
    left in the frame rather than dropped, so callers can decide how to handle them.
    """
    # Imported lazily so BTC-only deployments (spot trading, no Greeks needed)
    # don't need py_vollib installed at all.
    from py_vollib.black_scholes.greeks.analytical import delta as bs_delta
    from py_vollib.black_scholes.greeks.analytical import theta as bs_theta

    t_years = max(days_to_expiry, 1) / 365.0
    enriched = chain.copy()
    deltas = []
    thetas = []

    for _, row in enriched.iterrows():
        iv_pct = row.get("CE_iv")
        strike = row.get("strikePrice")
        if iv_pct is None or pd.isna(iv_pct) or iv_pct <= 0 or strike is None or pd.isna(strike):
            deltas.append(float("nan"))
            thetas.append(float("nan"))
            continue

        sigma = iv_pct / 100.0
        try:
            d = bs_delta("c", spot_price, strike, t_years, risk_free_rate, sigma)
            th = bs_theta("c", spot_price, strike, t_years, risk_free_rate, sigma)
        except Exception as exc:  # noqa: BLE001 - bad inputs shouldn't kill the whole chain
            logger.debug("Greeks calc failed for strike %s: %s", strike, exc)
            d, th = float("nan"), float("nan")

        deltas.append(d)
        thetas.append(th)

    enriched["CE_delta"] = deltas
    enriched["CE_theta"] = thetas
    return enriched


def find_atm_call(
    chain: pd.DataFrame,
    spot_price: float,
    target_delta: float = 0.50,
    delta_tolerance: float = 0.15,
) -> dict | None:
    """Find the call option whose Delta is closest to `target_delta`.

    `chain` must already have a CE_delta column (see enrich_chain_with_greeks).
    Returns None if no strike within `delta_tolerance` of target_delta has a
    usable quote (LTP > 0), which usually means the chain data was too sparse.
    """
    candidates = chain.dropna(subset=["CE_delta", "CE_ltp"])
    candidates = candidates[candidates["CE_ltp"] > 0]
    if candidates.empty:
        return None

    candidates = candidates.copy()
    candidates["delta_diff"] = (candidates["CE_delta"] - target_delta).abs()
    best = candidates.sort_values("delta_diff").iloc[0]

    if best["delta_diff"] > delta_tolerance:
        return None

    return {
        "strike": float(best["strikePrice"]),
        "ltp": float(best["CE_ltp"]),
        "delta": float(best["CE_delta"]),
        "theta": float(best["CE_theta"]),
        "iv": float(best["CE_iv"]),
    }
