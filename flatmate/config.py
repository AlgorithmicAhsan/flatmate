"""Reads settings from the .env file in the project root."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def get(name, default=""):
    return os.getenv(name, default).strip()


GROQ_API_KEY = get("GROQ_API_KEY")
GROQ_MODEL = get("GROQ_MODEL", "openai/gpt-oss-120b")
LLM_TEMPERATURE = float(get("LLM_TEMPERATURE", "0"))

EMAIL_ADDRESS = get("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = get("EMAIL_APP_PASSWORD").replace(" ", "")
IMAP_HOST = get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(get("IMAP_PORT", "993"))
SMTP_HOST = get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(get("SMTP_PORT", "465"))

DB_PATH = ROOT / get("DB_PATH", "data/flatmate.db")
EXPORT_DIR = ROOT / get("EXPORT_DIR", "exports")

# Policy limits. These are enforced in code, never left to the LLM.
CURRENCY = get("CURRENCY", "Rs")
AUTO_RECORD_LIMIT = int(get("AUTO_RECORD_LIMIT", "5000")) * 100  # stored in paisa
REMINDER_MIN_DAYS = int(get("REMINDER_MIN_DAYS", "3"))
REMINDER_MAX_PER_DEBT = int(get("REMINDER_MAX_PER_DEBT", "3"))

# Set MEMORY_ENABLED=false for the memory on/off comparison.
MEMORY_ENABLED = get("MEMORY_ENABLED", "true").lower() in ("1", "true", "yes")
