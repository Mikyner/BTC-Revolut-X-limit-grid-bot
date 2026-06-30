"""
Configuration for the BTC/EUR Limit Order Grid Bot.
All values are read from environment variables (see .env.example).
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "bot.db"

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"

PAIR = os.environ.get("TRADING_PAIR", "BTC-EUR")
DISPLAY_PAIR = "BTC/EUR"

REVOLUTX_API_KEY = os.environ.get("REVOLUTX_API_KEY", "")
REVOLUTX_PRIVATE_KEY_PATH = os.environ.get("REVOLUTX_PRIVATE_KEY_PATH", "/app/data/revolutx_private.pem")

GRID_LEVELS = int(os.environ.get("GRID_LEVELS", "41"))
GRID_RANGE_PERCENT = float(os.environ.get("GRID_RANGE_PERCENT", "25"))
GRID_BIAS_PERCENT = float(os.environ.get("GRID_BIAS_PERCENT", "35"))

ORDER_SIZE_EUR = float(os.environ.get("ORDER_SIZE_EUR", "10"))
PAPER_STARTING_BALANCE_EUR = float(os.environ.get("PAPER_STARTING_BALANCE_EUR", "350"))

MAX_PRICE_DEVIATION_PERCENT = float(os.environ.get("MAX_PRICE_DEVIATION_PERCENT", "25"))

# Maker fee je 0% - nemusíme počítat s fee vůbec
# Ale pro paper trading simulujeme 0%
MAKER_FEE_PERCENT = 0.0

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "change-me-in-production")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5051"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_NOTIFY_TRADES = os.environ.get("TELEGRAM_NOTIFY_TRADES", "true").lower() == "true"
TELEGRAM_NOTIFY_PAUSE = os.environ.get("TELEGRAM_NOTIFY_PAUSE", "true").lower() == "true"
TELEGRAM_NOTIFY_DAILY_SUMMARY = os.environ.get("TELEGRAM_NOTIFY_DAILY_SUMMARY", "true").lower() == "true"
TELEGRAM_DAILY_SUMMARY_TIME = os.environ.get("TELEGRAM_DAILY_SUMMARY_TIME", "20:00")
TELEGRAM_PAUSE_REMINDER_MINUTES = int(os.environ.get("TELEGRAM_PAUSE_REMINDER_MINUTES", "60"))
