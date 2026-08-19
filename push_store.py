# push_store.py — SQLite persistence for web-push reminder subscriptions.
# Lives in the same scat6.db; the table is ignored by everything else, so the
# PWA/push feature can be reverted without touching the database.
from __future__ import annotations
import json, sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from scat6_store import DB_PATH


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint TEXT UNIQUE NOT NULL,
            subscription_json TEXT NOT NULL,
            user_name TEXT,
            reminder_minutes INTEGER DEFAULT 120,
            enabled INTEGER DEFAULT 1,
            last_reminded_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def save_subscription(subscription: Dict, reminder_minutes: int = 120,
                      user_name: str = "") -> None:
    """Upsert by endpoint. last_reminded_at is set to now so the FIRST reminder
    arrives one full interval after enabling, not immediately."""
    endpoint = str(subscription.get("endpoint") or "")
    if not endpoint:
        raise ValueError("subscription has no endpoint")
    conn = _db()
    conn.execute(
        """INSERT INTO push_subscriptions
             (endpoint, subscription_json, user_name, reminder_minutes,
              enabled, last_reminded_at)
           VALUES (?,?,?,?,1,datetime('now'))
           ON CONFLICT(endpoint) DO UPDATE SET
             subscription_json=excluded.subscription_json,
             user_name=excluded.user_name,
             reminder_minutes=excluded.reminder_minutes,
             enabled=1,
             last_reminded_at=datetime('now')""",
        (endpoint, json.dumps(subscription), user_name or "",
         max(1, int(reminder_minutes))))
    conn.commit()
    conn.close()


def delete_subscription(endpoint: str) -> None:
    conn = _db()
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    conn.commit()
    conn.close()


def get_subscription(endpoint: str) -> Optional[Dict]:
    conn = _db()
    cur = conn.execute(
        "SELECT subscription_json, reminder_minutes, enabled, user_name "
        "FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"subscription": json.loads(row[0]), "reminder_minutes": row[1],
            "enabled": bool(row[2]), "user_name": row[3] or ""}


def due_subscriptions() -> List[Dict]:
    """Enabled subscriptions whose interval has elapsed since the last reminder."""
    conn = _db()
    cur = conn.execute(
        """SELECT endpoint, subscription_json, reminder_minutes, user_name
           FROM push_subscriptions
           WHERE enabled = 1
             AND (last_reminded_at IS NULL
                  OR datetime(last_reminded_at, '+' || reminder_minutes || ' minutes')
                     <= datetime('now'))""")
    rows = cur.fetchall()
    conn.close()
    return [{"endpoint": r[0], "subscription": json.loads(r[1]),
             "reminder_minutes": r[2], "user_name": r[3] or ""} for r in rows]


def mark_reminded(endpoint: str) -> None:
    conn = _db()
    conn.execute("UPDATE push_subscriptions SET last_reminded_at = datetime('now') "
                 "WHERE endpoint = ?", (endpoint,))
    conn.commit()
    conn.close()


def count_enabled() -> int:
    conn = _db()
    n = conn.execute("SELECT COUNT(*) FROM push_subscriptions WHERE enabled = 1"
                     ).fetchone()[0]
    conn.close()
    return int(n)
