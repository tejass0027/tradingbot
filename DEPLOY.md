# Deploying the full bot (Nifty + BTC) to a free cloud VM

Runs both instruments 24/7, independent of your laptop. Nifty's Xvfb
(virtual display) requirement means this needs a real VM — it can't run on
GitHub Actions' headless runners (see `DEPLOY_GITHUB_ACTIONS.md`, which only
covers BTC for that reason).

## 1. Get a free VM (Oracle Cloud Free Tier)

Oracle's free tier is genuinely free forever (not a 12-month trial like
AWS/GCP), which is why it's the pick here.

1. Sign up at https://www.oracle.com/cloud/free/ (a debit or credit card is
   required for identity verification, but the "Always Free" resources are
   never billed).
2. Once in the console: **Compute → Instances → Create Instance**.
3. Choose the **Always Free Ampere A1** shape, and give it at least **2
   OCPUs / 6GB RAM** (out of your total free allocation of up to 4 OCPU/24GB
   — you can split this across multiple instances if you want, but one
   instance sized like this is simplest). Avoid the tiny
   `VM.Standard.E2.1.Micro` (1GB RAM) here — Chromium needs more headroom
   than that to run reliably alongside everything else.
4. Use **Ubuntu 22.04** as the image.
5. Under "Add SSH keys", either upload your own public key or let Oracle
   generate one and download the private key.
6. Create the instance and note its **public IP address**.
7. **Open port 22 (SSH)** if it isn't already — Oracle's default security
   list usually allows it, but double check under the instance's attached
   VCN → Security Lists if `ssh` doesn't connect.

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

## 4. Configure `.env`

```bash
cp .env.example .env
nano .env
```

Set at minimum:
```
DISCORD_WEBHOOK_URL=<your real webhook>
ENABLE_NIFTY=true
ENABLE_BTC=true
```

Leave everything else as-is unless you want to change the strategy
parameters (SMA periods, target/stop-loss percentages, check interval,
etc.) — same meaning as when running locally.

**Never commit this `.env` file** — it's already in `.gitignore`.

## 5. Build and run

```bash
docker build -t tradingbot .
docker run -d \
  --name tradingbot \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/state_nifty.json:/app/state_nifty.json \
  -v $(pwd)/state_btc.json:/app/state_btc.json \
  tradingbot
```

- `--restart unless-stopped` makes Docker restart the container automatically
  if it crashes or the VM reboots — this is what gives you "always running."
- The `-v` mounts persist each instrument's position tracking on the VM's
  disk, so a container restart doesn't lose it. Both files are created
  automatically on first run if they don't exist yet.
- The image build takes noticeably longer than the BTC-only version
  (`Dockerfile.btc-only`) since it also installs Chromium and its OS-level
  dependencies via `playwright install --with-deps chromium` — a few minutes
  is normal, not stuck.

## 6. Verify it's working

```bash
docker logs -f tradingbot
```

You should see the same `[NIFTY] Signal: ...` / `[BTC] Signal: ...` log
lines as when running locally, including the "Online message sent to
Discord" line on startup. `Ctrl+C` to stop following the logs (the container
keeps running).

**If Nifty's check fails with a Playwright/browser error**, it's almost
certainly the virtual display: check `docker logs` for anything mentioning
`Xvfb` or `DISPLAY`, and confirm you see "Starting Xvfb on display :99" /
"Starting bot" from `entrypoint.sh` right after container startup.

Known issues already hit and fixed once (via Railway, same Dockerfile) while
building this out - kept here in case they resurface on a different
platform:
- `xvfb-run: error: xauth command not found` - the original approach used
  the `xvfb-run` wrapper script, which needs a separate `xauth` package.
- Total silence after "Starting Container" with no error at all, even with
  `PYTHONUNBUFFERED=1` - `xvfb-run`'s own readiness-check loop appears to
  hang indefinitely on Railway, for reasons that weren't fully diagnosable
  remotely. Fixed by dropping `xvfb-run` entirely in favor of
  `entrypoint.sh`, which starts `Xvfb` directly and sets `DISPLAY` itself -
  more transparent and far easier to debug when it isn't working.
- Chromium crashing/hanging unpredictably - Docker's default 64MB
  `/dev/shm` is too small for Chromium and can't be raised on a platform
  like Railway; `data_client.py` launches Chromium with
  `--disable-dev-shm-usage` to work around it.

## Updating after a code change

```bash
cd tradingbot
git pull
docker build -t tradingbot .
docker stop tradingbot && docker rm tradingbot
docker run -d --name tradingbot --restart unless-stopped --env-file .env \
  -v $(pwd)/state_nifty.json:/app/state_nifty.json \
  -v $(pwd)/state_btc.json:/app/state_btc.json \
  tradingbot
```

## BTC-only alternative

If you ever want to go back to a lighter BTC-only deployment (no Playwright,
much smaller image, faster builds), `Dockerfile.btc-only` +
`requirements-btc.txt` still work exactly as before — just substitute those
in the build/run commands above and set `ENABLE_NIFTY=false` in `.env`.
