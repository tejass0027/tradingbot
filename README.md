# Trading Signal Agent (Nifty 50 Options + Bitcoin & USD/INR Spot)

A modular Python bot that watches Nifty 50, Bitcoin, and the USD/INR forex
pair and pushes Buy/Sell alerts to a Discord channel via webhook, using a
20-period SMA crossover strategy for each:

- **Nifty 50**: options-based — finds an at-the-money (ATM) call near 0.50
  Delta and alerts on that option's premium.
- **Bitcoin**: plain spot trading — alerts on BTC's price directly, no
  options chain involved.
- **USD/INR**: plain spot trading on the forex rate itself, same as Bitcoin
  — no options chain involved.

**This is an educational/signal-generation tool. It does not place trades.
It is not financial advice.**

## Data sources

| Instrument | Price data | Options chain |
|---|---|---|
| Nifty 50 | `yfinance` (`^NSEI`) | Scraped from NSE's option-chain page via a real browser (see below) |
| Bitcoin | `yfinance` (`BTC-USD`) | None — traded as spot |
| USD/INR | `yfinance` (`USDINR=X`) | None — traded as spot |

`yfinance` doesn't carry Indian options chain data, so Nifty's chain comes
from NSE directly. Bitcoin and USD/INR only need price history since they
trade as spot. Gold was considered but skipped: there's no free, reliable
options-chain source for it (MCX isn't freely accessible, similar to or
worse than NSE) — it could be added as spot/futures-only the same way BTC
and USD/INR were, if wanted later.

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
| `strategy.py` | `generate_signal` (options: BUY_CALL/SELL/HOLD) and `generate_spot_signal` (BTC/USD-INR: BUY/SELL/HOLD), each with position state persisted to `state_<instrument>.json` |
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
  itself*. Stop-loss is always `BTC_STOP_LOSS_PCT` (default 2% loss) — kept
  much tighter than Nifty's, since it tracks the underlying price directly
  rather than a leveraged option premium. The target is a flat dollar amount
  via `BTC_PROFIT_TARGET_DOLLARS` (default `100` — exit once the position is
  up exactly $100, regardless of entry price) when set; otherwise it falls
  back to `BTC_PROFIT_TARGET_PCT` (default 3% gain). Set
  `BTC_PROFIT_TARGET_DOLLARS` empty/unset in `.env` to use the percentage
  target instead.
- **SELL**: currently in a position, and any of: price hits the target,
  hits the stop-loss, or crosses **below** the 20-SMA.

**USD/INR (spot)**
- Same rules as Bitcoin, but sized for forex's much smaller moves:
  `USDINR_PROFIT_TARGET_PCT` (default 0.4% gain) / `USDINR_STOP_LOSS_PCT`
  (default 0.2% loss), no flat-amount option.
- Checks are skipped outside forex trading days (approximated as "closed
  Saturday and Sunday" — see Notes/limitations).

**HOLD** (all three): neither condition is met.

These percentage defaults are common risk-management conventions, not
personalized advice — adjust them in `.env` to fit your own risk tolerance.

### Trend-confirmation filter

Every BUY/BUY_CALL rule above has one more condition attached: price must
also be above a longer/slower SMA, not just the 20-SMA, for the signal to
fire. This rejects crossovers that happen against the broader trend (e.g.
brief upward blips during a choppy/sideways or downward market). Each
instrument has its own on/off switch and period, since what worked for one
didn't necessarily work for another:

- Nifty and Bitcoin share `TREND_SMA_PERIOD` (default 50, currently **0 =
  disabled** in `.env` — Bitcoin's edge instead comes from the MACD+Fib
  filters below).
- USD/INR has its own `USDINR_TREND_SMA_PERIOD` (default 50, **enabled**) —
  it needed all three filters together (trend + MACD + Fib) to turn
  profitable at all; see below.

This was validated with `backtest.py` before being wired in for Nifty/BTC:
over the last 60 days of 15-min candles, adding the filter alone took BTC
from a -1.47% compounded return (361 trades, 26.6% win rate) to +2.74% (214
trades, 29.9% win rate) with lower drawdown. Nifty's improvement was smaller
(-1.29% → -1.09%) — remember Nifty's backtest trades the index, not the
option premium, so treat that number as a trigger-quality signal, not a
return forecast.

### Momentum filters (BTC, USD/INR): MACD + Fibonacci retracement

On top of the SMA crossover, a BTC or USD/INR BUY additionally requires:
- **MACD histogram positive** — the MACD line is above its signal line
  (12/26/9 EMA), i.e. bullish momentum agrees with the crossover.
- **Price at/above a Fibonacci retracement level** — computed from the
  swing high/low over the configured lookback (default 50 candles for
  both); price holding above the configured level (default `0.618` for
  both) is read as the recent uptrend structure still being intact.

Controlled per-instrument via `BTC_USE_MACD_FILTER` / `BTC_FIB_LOOKBACK` /
`BTC_FIB_LEVEL` and `USDINR_USE_MACD_FILTER` / `USDINR_FIB_LOOKBACK` /
`USDINR_FIB_LEVEL` in `.env`. Setting either `*_USE_MACD_FILTER=false`
disables both the MACD and Fibonacci checks for that instrument — they were
only validated together, not independently.

**BTC**, validated via `backtest.py --instrument BTC --fib-lookback 50
--fib-level 0.618 --macd`: over a 60-day window, this combo produced 100
trades, 33.0% win rate, +10.26% compounded return, 5.01% max drawdown —
versus the bare crossover's 372 trades / 26.3% win rate / +1.23% return /
9.95% drawdown. Fewer, higher-conviction trades with a much better average
win/loss ratio. Checked across both halves of the window independently too,
to guard against overfitting to one stretch of price action — both halves
were separately profitable (+3.81%/30.0% win rate and +6.81%/36.7% win
rate).

**USD/INR**, validated via `backtest.py --instrument USDINR
--trend-sma-period 50 --macd --fib-lookback 50 --fib-level 0.618`: needed
the trend filter *as well* to be profitable at all — the bare crossover lost
money here (-1.9% over 114 trades), and MACD+Fib alone without the trend
filter was still marginal. All three together: 21 trades, 42.9% win rate,
+0.59% compounded return, 0.27% max drawdown, positive in both halves of the
window independently (+0.35%/44.4% and +0.24%/41.7%). **This is a much
smaller and less certain edge than BTC's** — forex moves are roughly 10x
smaller than crypto's, so there's far less room for a filter to add value,
and the sample size (21 trades) is thinner than BTC's (100). Treat it as
"better than the alternative (a loss)," not as a strong edge.

### Short side (BTC only)

With `BTC_ALLOW_SHORT=true` (default) the bot also alerts a **SHORT** — the
mirror image of a BUY. A cross **below** the 20-SMA opens one when MACD's
histogram is negative, price is at or below the mirrored Fibonacci level
(the same fraction of the swing range measured up from the low, instead of
down from the high), and price is below the `BTC_SHORT_TREND_SMA_PERIOD`
SMA (default 50). Target sits below entry and the stop-loss above it, using
the same `BTC_PROFIT_TARGET_DOLLARS` / `BTC_STOP_LOSS_PCT` as longs. A short
needs futures or margin on an exchange — alerts are signals only.

A position closes internally on target, stop-loss, or the opposite SMA
cross, and if the other side's filters pass at that same cross it flips
straight into the new side (the alert you receive is the new entry; exits are
never alerted).

Backtested with `backtest.py --instrument BTC --shorts
--short-trend-sma-period 50 --macd --fib-lookback 50 --fib-level 0.618`
(3%/2% target/stop, no fees or funding costs modeled): on the latest 60 days
of 15-min candles the shorts alone made +1.71% (52 trades, 36.5% win rate)
while the longs lost -9.98%, so the two together were -8.44%; over ~700 days
of hourly candles the shorts made +36.77% (154 trades) and the longs
-19.55%. BTC mostly fell across that period, so a short side profits from the
trend itself — it would lose if BTC rallied for months. Shorts tested worse
on USD/INR, so they are BTC-only.

An RSI overbought filter (`calculate_rsi` in `analyzer.py`, wired into
`strategy.generate_spot_signal` as `rsi`/`rsi_overbought`) also exists and
is available via `backtest.py --rsi-period` / `--rsi-overbought`, but isn't
part of any live config — it didn't improve on the above combos in
backtesting.

Nifty, Bitcoin, and USD/INR each track their own position independently
(separate state files), so a trade on one doesn't block a signal on either
of the others.

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
   `ENABLE_BTC` / `ENABLE_USDINR` toggle each instrument independently.
   Adjust the other values (risk-free rate, SMA period, expiry window, check
   interval, target/stop-loss percentages) if needed — the defaults are
   reasonable.

3. Run the bot:

   ```bash
   python main.py
   ```

   It logs each check to the console and posts a message to Discord whenever
   a BUY_CALL/BUY/SHORT signal fires for any instrument (exit signals are still
   evaluated internally to close the tracked position, but are not sent as
   alerts - each BUY carries its own target and stop-loss). Position state
   survives restarts via `state_nifty.json` / `state_btc.json` /
   `state_usdinr.json` (created automatically, safe to delete to force a
   flat position on that instrument).

   It also posts a "📡 Status Check" message to Discord every
   `HEARTBEAT_INTERVAL_SECONDS` (default 3600 = 1 hour), separate from real
   trade alerts, summarizing each instrument's current signal (usually
   `HOLD`) - a quick way to confirm the process is actually alive without
   waiting for a real crossover.

   Separately, it posts a one-time "🟢 Bot is Online" message right after
   starting, and a "🔴 Bot is Offline" message on a graceful shutdown (Ctrl+C,
   or a normal `kill`/`taskkill` without `-9`/`/F`). **A forced kill (task
   manager "End task", `taskkill /F`, power loss) can't be intercepted by any
   program, so no offline message is possible in that case** - this is an OS
   limitation, not a bug.

   Both the status-check heartbeat and the online/offline messages only apply
   to the long-running local loop, not `--once` mode (used by GitHub
   Actions) - a fresh process every 5 minutes would otherwise spam an
   online+offline pair every single run.

## Backtesting

`backtest.py` replays the exact same strategy code the live bot uses
(`strategy.generate_spot_signal`) against historical price data, so results
reflect the real decision logic rather than a reimplementation that could
drift from it.

```bash
python backtest.py --instrument BOTH          # Nifty + BTC, defaults
python backtest.py --instrument BTC --days 60 --sma-period 20
python backtest.py --instrument NIFTY --target-pct 0.01 --stop-loss-pct 0.005
python backtest.py --instrument USDINR --trend-sma-period 50 --macd --fib-lookback 50 --fib-level 0.618
```

(`--instrument BOTH` only covers Nifty + BTC for historical reasons; USD/INR
needs to be selected explicitly.)

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
- USD/INR checks are skipped on Saturday/Sunday, approximating real forex
  hours (which actually run Sunday ~17:00 ET to Friday ~17:00 ET) as "closed
  both full weekend days, open otherwise" — same level of simplification as
  Nifty's holiday-blind check above, not an exact open/close model.
