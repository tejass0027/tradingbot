"""Technical indicators and options Greeks calculations."""

import datetime as dt
import logging

import pandas as pd
from py_vollib.black_scholes.greeks.analytical import delta as bs_delta
from py_vollib.black_scholes.greeks.analytical import theta as bs_theta

logger = logging.getLogger(__name__)


def calculate_sma(prices: pd.Series, period: int = 20) -> float:
    """Calculate the Simple Moving Average of the last `period` closes.

    Raises ValueError if there isn't enough data.
    """
    if len(prices) < period:
        raise ValueError(f"Need at least {period} price points for SMA, got {len(prices)}")
    return float(prices.tail(period).mean())


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
