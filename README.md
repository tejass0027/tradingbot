# Trading Signal Agent (Nifty 50 Options + Bitcoin Spot)

A modular Python bot that watches Nifty 50 and Bitcoin and pushes Buy/Sell
alerts to a Discord channel via webhook, using a 20-period SMA crossover
strategy for each:

- **Nifty 50**: options-based — finds an at-the-money (ATM) call near 0.50
  Delta and alerts on that option's premium.
- **Bitcoin**: plain spot trading — alerts on BTC's price directly, no
  options chain involved.

**This is an educational/signal-generation tool. It does not place trades.
It is not financial advice.**

## Data sources

| Instrument | Price data | Options chain |
|---|---|---|
| Nifty 50 | `yfinance` (`^NSEI`) | Scraped from NSE's option-chain page via a real browser (see below) |
| Bitcoin | `yfinance` (`BTC-USD`) | None — traded as spot |

`yfinance` doesn't carry Indian options chain data, so Nifty's chain comes
from NSE directly. Bitcoin only needs price history since it trades as spot.
Gold was considered but skipped: there's no free, reliable options-chain
source for it (MCX isn't freely accessible, similar to or worse than NSE).

### Why Nifty's options chain needs a real browser

NSE's site sits behind Akamai Bot Manager, which blocks this in two layered
ways:
1. Headless Chromium/Chrome is fingerprinted and connection-reset before any
   page even loads (`net::ERR_HTTP2_PROTOCOL_ERROR`) — regardless of
   User-Agent or TLS spoofing. The browser must run **headed**
   (non-headless); `data_client._scrape_option_chain` launches it positioned
   off-screen so it doesn't visibly interrupt you.
2. Even from a legitimately loaded page, a script-triggered `fetch()` to the
   JSON API (`/api/option-chain-indices`) is still rejected — Akamai appears
   to detect the CDP-driven script injection Playwright uses to run it. The
   page's own organic internal data load isn't blocked, so instead of
   calling the API, the bot scrapes the already-rendered HTML table
   (`#optionChainTable-indices`) directly.

This means each Nifty check launches a real (off-screen) browser instance
and takes a few extra seconds — expected, not a hang. If NSE tightens
further and even this stops working, the fallback is a broker API (Kite
Connect, Upstox, etc.) instead of free scraping.

## Architecture

| File | Responsibility |
|---|---|
| `main.py` | Loads config, defines the per-instrument pipeline (options vs spot mode), runs the check loop every `CHECK_INTERVAL_SECONDS` |
| `data_client.py` | Fetches price history (yfinance) and Nifty's options chain (NSE browser-scrape) |
| `analyzer.py` | Computes SMA and Black-Scholes Greeks (Delta/Theta) per strike (Nifty only) |
| `strategy.py` | `generate_signal` (options: BUY_CALL/SELL/HOLD) and `generate_spot_signal` (BTC: BUY/SELL/HOLD), each with position state persisted to `state_<instrument>.json` |
| `alerter.py` | Formats signals as Markdown and posts them to a Discord webhook |

### Strategy rules (applied independently per instrument)

**Nifty (options)**
- **BUY_CALL**: not currently in a position, underlying price crosses
  **above** the 20-SMA, and an ATM call with Delta close to 0.50 is found.
  The alert includes a target and stop-loss on the *option premium*,
  computed as `entry * (1 ± pct)` from `PROFIT_TARGET_PCT` (default 20%
  gain) / `STOP_LOSS_PCT` (default 30% loss).
- **SELL**: currently in a position, and any of: the option hits the target,
  hits the stop-loss, or the underlying crosses **below** the 20-SMA.

**Bitcoin (spot)**
- **BUY**: not currently in a position, and BTC's price crosses **above**
  the 20-SMA. The alert includes a target and stop-loss on the *spot price
  itself*, from `BTC_PROFIT_TARGET_PCT` (default 3% gain) /
  `BTC_STOP_LOSS_PCT` (default 2% loss) — kept much tighter than Nifty's,
  since these track the underlying price directly rather than a leveraged
  option premium.
- **SELL**: currently in a position, and any of: price hits the target,
  hits the stop-loss, or crosses **below** the 20-SMA.

**HOLD** (both): neither condition is met.

These percentage defaults are common risk-management conventions, not
personalized advice — adjust them in `.env` to fit your own risk tolerance.

### Trend-confirmation filter

Both BUY/BUY_CALL rules above have one more condition attached:
`TREND_SMA_PERIOD` (default 50, in `.env`) — price must also be above this
longer/slower SMA, not just the 20-SMA, for the signal to fire. This rejects
crossovers that happen against the broader trend (e.g. brief upward blips
during a choppy/sideways or downward market). Set `TREND_SMA_PERIOD=0` to
disable it and go back to a bare 20-SMA crossover.

This was validated with `backtest.py` before being wired in: over the last
60 days of 15-min candles, adding the filter took BTC from a -1.47%
compounded return (361 trades, 26.6% win rate) to +2.74% (214 trades, 29.9%
win rate) with lower drawdown. Nifty's improvement was smaller (-1.29% →
-1.09%) — remember Nifty's backtest trades the index, not the option
premium, so treat that number as a trigger-quality signal, not a return
forecast.

Nifty and Bitcoin each track their own position independently (separate
state files), so a Nifty trade doesn't block a Bitcoin signal or vice versa.

## Setup

1. Install dependencies, then download the headless Chromium build Playwright
   needs (one-time, ~150-300MB — used for the Nifty browser-scrape):

   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

2. Copy the environment template and fill in your Discord webhook:

   ```bash
   cp .env.example .env
   ```

   Edit `.env` and set `DISCORD_WEBHOOK_URL` (Discord: Server Settings →
   Integrations → Webhooks → New Webhook → Copy URL). `ENABLE_NIFTY` /
   `ENABLE_BTC` toggle each instrument independently. Adjust the other
   values (risk-free rate, SMA period, expiry window, check interval,
   target/stop-loss percentages) if needed — the defaults are reasonable.

3. Run the bot:

   ```bash
   python main.py
   ```

   It logs each check to the console and posts a message to Discord whenever
   a BUY_CALL/BUY or SELL signal fires for either instrument. Position state
   survives restarts via `state_nifty.json` / `state_btc.json` (created
   automatically, safe to delete to force a flat position on that
   instrument).

## Backtesting

`backtest.py` replays the exact same strategy code the live bot uses
(`strategy.generate_spot_signal`) against historical price data, so results
reflect the real decision logic rather than a reimplementation that could
drift from it.

```bash
python backtest.py --instrument BOTH          # both instruments, defaults
python backtest.py --instrument BTC --days 60 --sma-period 20
python backtest.py --instrument NIFTY --target-pct 0.01 --stop-loss-pct 0.005
```

It prints number of trades, win rate, average win/loss, compounded return,
max drawdown, and the last 5 trades.

**Important limitation for Nifty**: this backtests the underlying *index*
price directly, not the option premium the live bot actually trades. There's
no free historical NSE options-chain data, so the actual option P&L (which
differs from the index due to theta decay and changing IV) can't be
backtested. Treat Nifty's backtest as a check on whether the SMA-crossover
*trigger* is any good, not a return forecast — its default target/stop-loss
percentages are also index-scaled (1%/0.5%), not the option-premium-scaled
ones the live bot uses (`PROFIT_TARGET_PCT`/`STOP_LOSS_PCT` in `.env`).

`yfinance` also caps how far back intraday data goes — 15-minute candles are
only available for the last 60 days; using `--interval` values other than
`15m` changes that limit (`backtest.py`'s `_MAX_INTRADAY_LOOKBACK_DAYS`).

## Notes / limitations

- Nifty's Delta/Theta are computed from NSE's quoted implied volatility per
  strike via Black-Scholes (`py_vollib`).
- The bot only ever tracks **one open position per instrument** at a time.
- A failed fetch for one instrument (e.g. NSE briefly blocking the scrape)
  is logged and skipped for that cycle — it doesn't affect the other
  instrument or crash the bot.
- Nifty checks only run during NSE's trading window (09:15-15:30 IST,
  Mon-Fri) — outside that window it logs "market is closed" and skips,
  rather than repeatedly re-evaluating the same stale end-of-session data
  that `yfinance`/NSE still return when queried while the market is shut.
  This does **not** account for NSE trading holidays (Diwali, Republic Day,
  etc.) — on a holiday it'll still attempt checks and just work off
  whatever stale data comes back. Bitcoin trades 24/7, so its checks always run.
