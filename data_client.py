"""Market data access layer.

Fetches spot/history for Nifty 50 and Bitcoin via yfinance, and the live
Nifty 50 options chain by driving a real browser against NSE India's
option-chain page (yfinance does not carry Indian options chain data).
Bitcoin trades as plain spot in this bot (no options), so it only needs
price history.

NSE sits behind Akamai Bot Manager, which blocks scraping in two separate ways:
  1. Headless Chromium/Chrome is fingerprinted and connection-reset before
     any page even loads (net::ERR_HTTP2_PROTOCOL_ERROR) - so the browser
     must run headed (non-headless). It's launched positioned off-screen so
     it doesn't visibly interrupt the user.
  2. Even from a legitimately loaded page, a script-triggered fetch() to the
     JSON API (`/api/option-chain-indices`) is still rejected - Akamai
     appears to detect the CDP-driven script injection Playwright uses to
     run it. The page's own organic internal data load is not blocked, so
     instead of calling the API we scrape the already-rendered HTML table.
"""

import datetime as dt
import logging
import re

import pandas as pd
import yfinance as yf
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

NSE_OPTION_CHAIN_PAGE = "https://www.nseindia.com/option-chain"

# Column order of <td> cells within a row of #optionChainTable-indices > tbody,
# as rendered by NSE (verified against the live page's DOM).
_TABLE_COLUMNS = [
    "_fav",
    "CE_oi",
    "CE_chng_oi",
    "CE_volume",
    "CE_iv",
    "CE_ltp",
    "CE_chng",
    "CE_bid_qty",
    "CE_bid",
    "CE_ask",
    "CE_ask_qty",
    "strikePrice",
    "PE_bid_qty",
    "PE_bid",
    "PE_ask",
    "PE_ask_qty",
    "PE_chng",
    "PE_ltp",
    "PE_iv",
    "PE_volume",
    "PE_chng_oi",
    "PE_oi",
    "_trail",
]


def _to_number(text: str) -> float | None:
    """Parse an NSE table cell like '3,100.00' or '-' into a float (or None)."""
    text = (text or "").strip()
    if text in ("", "-"):
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def get_price_history(
    yfinance_ticker: str = "^NSEI",
    period: str = "5d",
    interval: str = "15m",
) -> pd.DataFrame:
    """Fetch recent underlying price history for SMA/crossover calculations.

    Returns a DataFrame indexed by timestamp with at least a 'Close' column.
    Raises RuntimeError if no data could be retrieved.
    """
    try:
        ticker = yf.Ticker(yfinance_ticker)
        history = ticker.history(period=period, interval=interval)
    except Exception as exc:
        raise RuntimeError(f"yfinance history fetch failed for {yfinance_ticker}: {exc}") from exc

    if history is None or history.empty:
        raise RuntimeError(f"yfinance returned no price history for {yfinance_ticker}")

    return history


def get_option_chain(
    symbol: str = "NIFTY",
    min_days_to_expiry: int = 7,
    max_days_to_expiry: int = 30,
    max_retries: int = 2,
    timeout_ms: int = 25000,
) -> dict:
    """Fetch the live NSE options chain for `symbol` (currently only NIFTY is
    supported - the option-chain page defaults to it), filtered to the
    nearest expiry within [min_days_to_expiry, max_days_to_expiry] days.

    Returns a dict:
        {
            "expiry_date": datetime.date,
            "days_to_expiry": int,
            "underlying_value": float,
            "chain": pd.DataFrame with columns:
                strikePrice, CE_ltp, CE_iv, CE_oi, PE_ltp, PE_iv, PE_oi
        }

    Raises RuntimeError if the page can't be loaded/scraped or has no expiry
    in the requested window.
    """
    if symbol != "NIFTY":
        raise ValueError("Only NIFTY is supported (NSE's option-chain page defaults to it)")

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return _scrape_option_chain(min_days_to_expiry, max_days_to_expiry, timeout_ms)
        except Exception as exc:  # noqa: BLE001 - want to retry on any failure
            last_error = exc
            logger.warning("NSE option chain scrape attempt %d/%d failed: %s", attempt, max_retries, exc)

    raise RuntimeError(f"Failed to fetch NSE option chain for {symbol} after {max_retries} attempts: {last_error}")


def _scrape_option_chain(min_days: int, max_days: int, timeout_ms: int) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--window-position=-32000,-32000", "--window-size=1280,900"],
        )
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(NSE_OPTION_CHAIN_PAGE, timeout=timeout_ms, wait_until="networkidle")
            page.wait_for_selector("#expirySelect", timeout=timeout_ms)
            page.wait_for_selector("#optionChainTable-indices tbody tr", timeout=timeout_ms)

            expiry_options = page.eval_on_selector_all(
                "#expirySelect option", "opts => opts.map(o => o.value).filter(v => v)"
            )
            expiry_date, expiry_str, days_to_expiry = _pick_expiry(expiry_options, min_days, max_days)

            page.select_option("#expirySelect", expiry_str)
            page.wait_for_timeout(2500)  # table repopulates asynchronously after expiry change
            page.wait_for_selector("#optionChainTable-indices tbody tr", timeout=timeout_ms)

            body_text = page.inner_text("body")
            underlying_value = _extract_underlying_value(body_text)

            raw_rows = page.eval_on_selector_all(
                "#optionChainTable-indices tbody tr",
                "rows => rows.map(r => Array.from(r.children).map(c => c.textContent.trim()))",
            )
        finally:
            browser.close()

    chain_df = _rows_to_dataframe(raw_rows)
    return {
        "expiry_date": expiry_date,
        "days_to_expiry": days_to_expiry,
        "underlying_value": underlying_value,
        "chain": chain_df,
    }


def _pick_expiry(expiry_options: list[str], min_days: int, max_days: int) -> tuple[dt.date, str, int]:
    today = dt.date.today()
    candidates = []
    for expiry_str in expiry_options:
        try:
            expiry_date = dt.datetime.strptime(expiry_str, "%d-%b-%Y").date()
        except ValueError:
            continue
        days_out = (expiry_date - today).days
        if min_days <= days_out <= max_days:
            candidates.append((days_out, expiry_date, expiry_str))

    if not candidates:
        raise RuntimeError(f"No NSE expiry found between {min_days} and {max_days} days from {today}")

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1], candidates[0][2], candidates[0][0]


def _extract_underlying_value(body_text: str) -> float | None:
    match = re.search(r"NIFTY\s*([\d,]+\.\d+)", body_text)
    return float(match.group(1).replace(",", "")) if match else None


def _rows_to_dataframe(raw_rows: list[list[str]]) -> pd.DataFrame:
    parsed = []
    for cells in raw_rows:
        if len(cells) != len(_TABLE_COLUMNS):
            continue
        row = dict(zip(_TABLE_COLUMNS, cells))
        parsed.append(
            {
                "strikePrice": _to_number(row["strikePrice"]),
                "CE_ltp": _to_number(row["CE_ltp"]),
                "CE_iv": _to_number(row["CE_iv"]),
                "CE_oi": _to_number(row["CE_oi"]),
                "PE_ltp": _to_number(row["PE_ltp"]),
                "PE_iv": _to_number(row["PE_iv"]),
                "PE_oi": _to_number(row["PE_oi"]),
            }
        )

    chain_df = pd.DataFrame(parsed).dropna(subset=["strikePrice"])
    if chain_df.empty:
        raise RuntimeError("NSE option chain table had no parseable rows")

    return chain_df.sort_values("strikePrice").reset_index(drop=True)
