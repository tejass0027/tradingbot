# Deploying BTC-only to Railway

Runs the BTC bot 24/7 on Railway instead of your laptop. This uses
`Dockerfile.btc-only` (no Playwright/Chromium) since Railway's free-tier
resource limit (0.5GB RAM per service) is too tight to reliably run
Chromium for Nifty's NSE scrape — that risks getting OOM-killed. Nifty stays
on your laptop for now.

## 1. Create the project

1. Sign up / log in at https://railway.app (GitHub login is easiest, and
   also makes step 2 simpler).
2. **New Project → Deploy from GitHub repo → select `AlgoBuilds/tradingbot`**.
   If the repo isn't listed, you'll need to authorize Railway's GitHub App
   for that repo first (Railway prompts for this automatically).

Railway will try to build immediately using auto-detection — that's fine,
you'll fix the build settings in the next step before it matters.

## 2. Point the build at the lean Dockerfile

By default Railway looks for a file literally named `Dockerfile` in the
repo root, which in this repo is the **full** Nifty+BTC image (with
Chromium) — too heavy for the free tier. Point it at the lean one instead:

1. Open the service → **Settings → Build**.
2. Set **Builder** to `Dockerfile` (if not already).
3. Set **Dockerfile Path** to `Dockerfile.btc-only`.
4. Save — this triggers a rebuild using the correct, lightweight image.

## 3. Set environment variables

Service → **Variables** tab → add these (Raw Editor is fastest for pasting
several at once):

```
DISCORD_WEBHOOK_URL=<your real webhook>
ENABLE_NIFTY=false
ENABLE_BTC=true
```

Everything else (SMA period, target/stop-loss percentages, check interval,
etc.) uses the same sensible defaults as running locally — only add more
variables if you want to override something specific (see `.env.example`
for the full list of names).

## 4. Don't expose a public domain

This is a background worker, not a web service — it doesn't listen on any
port. Skip/ignore Railway's "Generate Domain" prompt; leaving the service
without a public domain is correct here, not a misconfiguration.

## 5. (Recommended) Add a volume for state persistence

Without this, `state_btc.json` (open-position tracking) lives on the
container's ephemeral disk — it survives ordinary restarts (e.g. a crash
triggering Railway's restart policy) but gets wiped on every new deploy
(e.g. after a `git push` triggers a rebuild). That's not catastrophic (the
bot just starts flat again), but if you want it to persist across deploys
too:

1. Service → **Settings → Volumes → New Volume**.
2. Mount path: `/app`.
3. Redeploy once after adding it.

## 6. Verify it's working

Service → **Deployments** → open the latest deployment → **View Logs**.
You should see the same `[BTC] Signal: ...` lines and the "Online message
sent to Discord" line you'd see running locally.

## Updating after a code change

Just `git push` to the repo — Railway auto-deploys on every push to the
connected branch by default (check Settings → the "Auto Deploy" toggle if
you ever want to turn that off).

## Notes

- I couldn't test-deploy this myself (no Railway CLI/account available in
  the environment I built this in) - the steps above follow Railway's
  documented Dockerfile-path override and standard GitHub-deploy flow, but
  watch the first build/deploy logs carefully.
- If RuPay was rejected for Oracle's signup, it's worth trying here anyway -
  Railway bills through Stripe, and acceptance can differ by processor/card
  issuer even when both nominally require Visa/Mastercard.
