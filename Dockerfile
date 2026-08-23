FROM python:3.12-slim

WORKDIR /app

COPY requirements-btc.txt .
RUN pip install --no-cache-dir -r requirements-btc.txt

COPY main.py analyzer.py data_client.py strategy.py alerter.py ./

# .env and state_btc.json are provided at runtime (mounted or passed via env),
# not baked into the image - see DEPLOY.md.
CMD ["python", "main.py"]
