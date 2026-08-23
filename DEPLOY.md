# Deploying the BTC bot to a free cloud VM (runs 24/7, independent of your laptop)

This covers a **BTC-only** deployment — no Playwright/browser needed since
BTC trades as plain spot (see `README.md`). Add Nifty back once you're
running it via Kite Connect instead of the NSE scrape, since that removes
the headless-browser display problem entirely.

## 1. Get a free VM (Oracle Cloud Free Tier)

Oracle's free tier is genuinely free forever (not a 12-month trial), which
is why it's the pick here over AWS/GCP's time-limited free tiers.

1. Sign up at https://www.oracle.com/cloud/free/ (requires a card for
   identity verification, but the "Always Free" resources are never billed).
2. Once in the console: **Compute → Instances → Create Instance**.
3. Choose the **Always Free** shape (either the Ampere A1 ARM shape or the
   `VM.Standard.E2.1.Micro` AMD shape — either works fine for this bot).
4. Use **Ubuntu 22.04** as the image.
5. Under "Add SSH keys", either upload your own public key or let Oracle
   generate one and download the private key.
6. Create the instance and note its **public IP address**.

## 2. Connect and install Docker

```bash
ssh -i /path/to/your/key ubuntu@<VM_PUBLIC_IP>

sudo apt-get update
sudo apt-get install -y docker.io git
sudo usermod -aG docker $USER
# log out and back in for the group change to take effect
```

## 3. Get the code onto the VM

```bash
git clone https://github.com/AlgoBuilds/tradingbot.git
cd tradingbot
```

## 4. Configure `.env` for BTC-only

```bash
cp .env.example .env
nano .env
```

Set at minimum:
```
DISCORD_WEBHOOK_URL=<your real webhook>
ENABLE_NIFTY=false
ENABLE_BTC=true
```

Leave the other BTC-related values (`BTC_YFINANCE_TICKER`,
`BTC_PROFIT_TARGET_PCT`, `BTC_STOP_LOSS_PCT`, `TREND_SMA_PERIOD`,
`CHECK_INTERVAL_SECONDS`, `SMA_PERIOD`) as-is unless you want to change them.

**Never commit this `.env` file** — it's already in `.gitignore`.

## 5. Build and run

```bash
docker build -t tradingbot-btc .
docker run -d \
  --name tradingbot-btc \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/state_btc.json:/app/state_btc.json \
  tradingbot-btc
```

- `--restart unless-stopped` makes Docker restart the container automatically
  if it crashes or the VM reboots — this is what gives you "always running."
- The `-v` mount persists `state_btc.json` (your open-position tracking) on
  the VM's disk, so a container restart doesn't lose it. The file is created
  automatically on first run if it doesn't exist yet.

## 6. Verify it's working

```bash
docker logs -f tradingbot-btc
```

You should see the same `[BTC] Signal: ...` log lines as when running
locally. `Ctrl+C` to stop following the logs (the container keeps running).

## Updating after a code change

```bash
cd tradingbot
git pull
docker build -t tradingbot-btc .
docker stop tradingbot-btc && docker rm tradingbot-btc
docker run -d --name tradingbot-btc --restart unless-stopped --env-file .env \
  -v $(pwd)/state_btc.json:/app/state_btc.json tradingbot-btc
```

## Adding Nifty later (once you have Kite Connect)

At that point Nifty won't need the NSE-scrape/Playwright workaround anymore
(Kite Connect is a normal authenticated API), so the "headless server has no
display" problem goes away — swap `data_client.get_option_chain` for a
Kite-based implementation and switch `requirements-btc.txt` back to
`requirements.txt` (or add `kiteconnect` to it) in the Dockerfile.
