You are an expert quantitative developer. Your task is to build a modular, Python-based Options Trading Signal Agent from scratch. 

## Project Objective
Create a bot that monitors [ENTER TICKER, e.g., SPY or AAPL], analyzes its options chain, and sends real-time Buy/Sell alerts to [ENTER ALERTS, e.g., Discord / Telegram] based on technical indicators and Options Greeks.

## Tech Stack
*   **Language:** Python 3.11+
*   **Market Data:** `yfinance` (for free historical price and options chain data)
*   **Greeks/Math:** `py_vollib` (for Delta/Theta calculations), `pandas`, `numpy`
*   **Networking:** `requests` (for webhook alerts)
*   **Environment:** `python-dotenv` (for managing API keys)

## Architecture & Files
Please create the following directory structure and populate the files:
*   `main.py`: The entry point that schedules and runs the bot loop.
*   `data_client.py`: Fetches underlying stock prices and options chain data.
*   `analyzer.py`: Calculates a 20-period Simple Moving Average (SMA), Implied Volatility, and Greeks (Delta/Theta).
*   `strategy.py`: Contains the logic to trigger a signal.
*   `alerter.py`: Formats the Buy/Sell signal and pushes it to the webhook.
*   `.env.example`: Template for configuration variables.
*   `requirements.txt`: Project dependencies.
*   `README.md`: Instructions on how to run the bot.

## Execution Steps
1.  **Initialize:** Scaffold the files and create the `requirements.txt`. 
2.  **Data Layer:** In `data_client.py`, write a function to pull the options chain expiring between 7 and 30 days from now. 
3.  **Strategy Logic:** In `strategy.py`, write a rule that triggers a "BUY CALL" alert if:
    *   The underlying price crosses ABOVE the 20 SMA.
    *   Find an At-The-Money (ATM) call option where Delta is approximately 0.50.
    *   Write a rule for "SELL" if the option reaches a 20% profit target or the underlying crosses below the SMA.
4.  **Alerting:** In `alerter.py`, create a function that accepts the signal dictionary and sends a cleanly formatted markdown message to the webhook.
5.  **Main Loop:** In `main.py`, tie these modules together so the bot runs a check every 5 minutes.

## Constraints
*   Do not leave placeholder functions; write fully functional code for the yfinance and webhook integrations.
*   Write defensively: include error handling (try/except blocks) for API timeouts or missing options chain data.
*   Document the code with clear Python docstrings.