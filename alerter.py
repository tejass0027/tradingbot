"""Formats trading signals and pushes them to a Discord webhook."""

import logging

import requests

logger = logging.getLogger(__name__)


def format_signal_message(signal: dict) -> str:
    """Render a signal dict as a Discord-flavored Markdown message."""
    action = signal.get("action", "HOLD")
    cur = signal.get("currency_symbol", "₹")

    if action == "BUY_CALL":
        return (
            f"### \U0001F7E2 BUY CALL — {signal['ticker']}\n"
            f"**Strike:** {signal['strike']}  |  **Expiry:** {signal['expiry']}\n"
            f"**Entry Price:** {cur}{signal['entry_price']:.2f}\n"
            f"**\U0001F3AF Target (take-profit):** {cur}{signal['target_price']:.2f}\n"
            f"**\U0001F6D1 Stop Loss:** {cur}{signal['stop_loss_price']:.2f}\n"
            f"**Delta:** {signal['delta']:.3f}  |  **Theta:** {signal['theta']:.3f}  |  **IV:** {signal['iv']:.2f}%\n"
            f"**Underlying:** {cur}{signal['underlying_price']:.2f}  |  **SMA:** {cur}{signal['sma']:.2f}\n"
            f"_{signal['reason']}_"
        )

    if action == "BUY":
        return (
            f"### \U0001F7E2 BUY — {signal['ticker']}\n"
            f"**Entry Price:** {cur}{signal['entry_price']:.2f}\n"
            f"**\U0001F3AF Target (take-profit):** {cur}{signal['target_price']:.2f}\n"
            f"**\U0001F6D1 Stop Loss:** {cur}{signal['stop_loss_price']:.2f}\n"
            f"**SMA:** {cur}{signal['sma']:.2f}\n"
            f"_{signal['reason']}_"
        )

    if action == "SELL" and "strike" in signal:
        exit_price = signal.get("exit_price")
        exit_str = f"{cur}{exit_price:.2f}" if exit_price is not None else "N/A"
        return (
            f"### \U0001F534 SELL — {signal['ticker']}\n"
            f"**Strike:** {signal['strike']}  |  **Expiry:** {signal['expiry']}\n"
            f"**Entry Price:** {cur}{signal['entry_price']:.2f}  |  **Exit Price:** {exit_str}\n"
            f"**Target was:** {cur}{signal['target_price']:.2f}  |  **Stop Loss was:** {cur}{signal['stop_loss_price']:.2f}\n"
            f"**Underlying:** {cur}{signal['underlying_price']:.2f}  |  **SMA:** {cur}{signal['sma']:.2f}\n"
            f"_{signal['reason']}_"
        )

    if action == "SELL":
        exit_price = signal.get("exit_price")
        exit_str = f"{cur}{exit_price:.2f}" if exit_price is not None else "N/A"
        return (
            f"### \U0001F534 SELL — {signal['ticker']}\n"
            f"**Entry Price:** {cur}{signal['entry_price']:.2f}  |  **Exit Price:** {exit_str}\n"
            f"**Target was:** {cur}{signal['target_price']:.2f}  |  **Stop Loss was:** {cur}{signal['stop_loss_price']:.2f}\n"
            f"**SMA:** {cur}{signal['sma']:.2f}\n"
            f"_{signal['reason']}_"
        )

    return f"**HOLD** — {signal.get('ticker', '')}: {signal.get('reason', '')}"


def send_discord_alert(signal: dict, webhook_url: str, timeout: float = 5.0) -> bool:
    """Send a formatted signal to a Discord incoming webhook.

    Returns True on success, False on failure (never raises, since a failed
    alert shouldn't crash the bot's main loop).
    """
    if not webhook_url:
        logger.warning("No Discord webhook URL configured; skipping alert")
        return False

    message = format_signal_message(signal)
    payload = {"content": message}

    try:
        response = requests.post(webhook_url, json=payload, timeout=timeout)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.error("Failed to send Discord alert: %s", exc)
        return False
