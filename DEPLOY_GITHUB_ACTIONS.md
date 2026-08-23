# Running the BTC bot on GitHub Actions (free, no card, no server)

This runs the BTC check every 5 minutes on GitHub's own infrastructure —
nothing to host, no VM, no credit/debit card needed. Since
[AlgoBuilds/tradingbot](https://github.com/AlgoBuilds/tradingbot) is a
**public** repo, GitHub Actions minutes are unlimited and free.

## How it works

`.github/workflows/btc-bot.yml` runs on a cron schedule (`*/5 * * * *`).
Each run is a fresh, short-lived machine that:
1. Restores `state_btc.json` (your open-position tracking) from GitHub's
   Actions cache
2. Installs the lean BTC-only dependencies (`requirements-btc.txt` — no
   Playwright/py_vollib needed)
3. Runs `python main.py --once` — a single check-and-exit pass, instead of
   the long-running loop `main.py` normally runs locally
4. Saves the updated `state_btc.json` back to the cache for next time

This works because the bot's state is already just a small JSON file loaded
fresh each cycle — there's no in-memory state that needs a persistent
process, so a new machine every 5 minutes behaves identically to one
long-running process checking every 5 minutes.

## One-time setup

1. Go to the repo on GitHub: **Settings → Secrets and variables → Actions →
   New repository secret**.
2. Name: `DISCORD_WEBHOOK_URL`. Value: your real Discord webhook URL (the
   same one in your local `.env`). This keeps the secret out of the code —
   `main.py` reads it from the environment, and the workflow injects it from
   this secret at run time.
3. That's it — the workflow starts running automatically on its schedule
   once it's on the default branch (already pushed).

## Verifying it's working

- GitHub repo → **Actions** tab → "BTC Trading Bot" workflow → you should
  see runs appearing roughly every 5 minutes, each taking well under a
  minute.
- Click any run → "check" job → "Run one check cycle" step to see the same
  `[BTC] Signal: ...` log line you'd see running locally.
- To trigger one immediately instead of waiting for the schedule: **Actions
  → BTC Trading Bot → Run workflow** (this uses the `workflow_dispatch`
  trigger already in the workflow file).

## Limitations worth knowing

- **Schedule isn't exact.** GitHub explicitly doesn't guarantee scheduled
  workflows fire at the precise time — during high platform load, runs can
  be delayed by several minutes, or occasionally skipped. Fine for a
  5-minute-granularity strategy, but don't treat it as real-time.
- **Auto-disabled after 60 days of repo inactivity.** If the repo gets no
  commits for 60 days, GitHub automatically disables scheduled workflows.
  A commit (even unrelated to the bot) re-enables it. Worth knowing if you
  step away from the project for a while.
- This only runs **BTC**. Nifty still needs `requirements.txt` (Playwright)
  and a real display for the NSE browser-scrape, which doesn't work on
  GitHub's headless runners — that's the same limitation the Docker/VM path
  has today (see `DEPLOY.md`), until Nifty moves to Kite Connect.
