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
# xauth is required by the xvfb-run wrapper script itself (separate package
# from xvfb on Debian) - without it xvfb-run fails with "xauth command not
# found" before even attempting to start Xvfb.
RUN apt-get update && apt-get install -y --no-install-recommends xvfb xauth \
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
