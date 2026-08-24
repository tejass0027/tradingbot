FROM python:3.12-slim

WORKDIR /app

# Python buffers stdout when it isn't attached to a real terminal (true
# inside Docker/xvfb-run), which can silently swallow log output - and any
# output still sitting in that buffer - if the process gets killed abruptly
# (crash, OOM). Unbuffered output means logs actually reach `docker logs` /
# the platform's log viewer in real time, which matters a lot for debugging
# a container that dies without an obvious error.
ENV PYTHONUNBUFFERED=1

# xvfb provides a virtual display so Playwright's headed Chromium (required
# for the Nifty NSE scrape - see data_client.py's module docstring for why
# headless mode gets blocked) can run on a server with no real display.
RUN apt-get update && apt-get install -y --no-install-recommends xvfb \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium

COPY main.py analyzer.py data_client.py strategy.py alerter.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

# .env and state_*.json are provided at runtime (env vars / volume mounts),
# not baked into the image - see DEPLOY.md.
# entrypoint.sh starts Xvfb directly and sets DISPLAY, rather than going
# through the xvfb-run wrapper script - that wrapper hung with no output at
# all on Railway (even after fixing its xauth dependency and adding
# PYTHONUNBUFFERED), so this manages the virtual display explicitly instead,
# which is both more reliable and far easier to debug when it isn't.
CMD ["./entrypoint.sh"]
