FROM python:3.12-slim

WORKDIR /app

# xvfb provides a virtual display so Playwright's headed Chromium (required
# for the Nifty NSE scrape - see data_client.py's module docstring for why
# headless mode gets blocked) can run on a server with no real display.
RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

COPY main.py analyzer.py data_client.py strategy.py alerter.py ./

# .env and state_*.json are provided at runtime (env vars / volume mounts),
# not baked into the image - see DEPLOY.md.
# xvfb-run wraps the command in a virtual X display (auto-picks a free
# display number via -a) so headed Chromium launches succeed.
CMD ["xvfb-run", "-a", "python", "main.py"]
