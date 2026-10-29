"""SQLite connection, schema setup, the demo clock, and the action log."""

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from flatmate import config

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


def connect(path=None):
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row          # rows behave like dicts: row["amount"]
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))  # safe to run every time
    return conn


def now_ts():
    """Real wall-clock time, used for log and record timestamps."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Demo clock -------------------------------------------------------------
# The assignment allows a manually advanced clock. Business dates (expense dates,
# reminder spacing) come from here; timestamps in logs stay real.

def today(conn):
    row = conn.execute("SELECT value FROM meta WHERE key = 'clock_date'").fetchone()
    return date.fromisoformat(row["value"]) if row else date.today()


def set_clock(conn, day):
    day = date.fromisoformat(day)  # raises ValueError on a bad date
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('clock_date', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (day.isoformat(),),
    )
    conn.commit()
    return day


# --- Action log -------------------------------------------------------------

def log(conn, event, *, flat_id=None, cycle_id=None, tool=None, input=None,
        output=None, status="ok", error=None, actor="agent"):
    """Record one event in the audit trail.

    event is one of: tool_call, memory_read, memory_write, llm_decision, error.
    """
    conn.execute(
        "INSERT INTO action_log (ts, flat_id, cycle_id, actor, event, tool, input, output, status, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (now_ts(), flat_id, cycle_id, actor, event, tool,
         json.dumps(input, default=str) if input is not None else None,
         json.dumps(output, default=str) if output is not None else None,
         status, error),
    )
    conn.commit()
