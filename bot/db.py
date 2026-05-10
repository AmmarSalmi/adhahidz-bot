from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS subscriptions (
  user_id     INTEGER NOT NULL,
  Wilaya_code TEXT    NOT NULL,
  notified    INTEGER NOT NULL DEFAULT 0,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id)
);
CREATE TABLE IF NOT EXISTS user_settings (
  user_id     INTEGER NOT NULL,
  language    TEXT    NOT NULL DEFAULT 'ar',
  PRIMARY KEY (user_id)
);
CREATE TABLE IF NOT EXISTS wilayas (
  code TEXT NOT NULL PRIMARY KEY,
  name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS communes (
  code TEXT NOT NULL PRIMARY KEY,
  wilaya_code TEXT NOT NULL,
  name TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1,
  FOREIGN KEY (wilaya_code) REFERENCES wilayas(code)
);
CREATE TABLE IF NOT EXISTS quota_history (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  wilaya_code TEXT    NOT NULL,
  event_type  TEXT    NOT NULL, -- 'OPEN' or 'CLOSE'
  timestamp   TEXT    NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (wilaya_code) REFERENCES wilayas(code)
);
CREATE TABLE IF NOT EXISTS admin_inbox (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  level       TEXT    NOT NULL, -- 'ERROR' or 'WARNING'
  message     TEXT    NOT NULL,
  stack_trace TEXT,
  status      TEXT    NOT NULL DEFAULT 'unresolved', -- 'unresolved' or 'resolved'
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT,
  is_hidden   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sync_history (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type  TEXT    NOT NULL, -- 'order_found', 'order_blocked'
  profile_id  INTEGER,
  user_id     INTEGER,
  details     TEXT,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS global_settings (
  key         TEXT    PRIMARY KEY,
  value       TEXT
);
"""


@dataclass(frozen=True)
class Subscription:
    user_id: int
    wilaya_code: str
    notified: int
    created_at: str


def _is_locked_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "locked" == msg.strip()


async def _with_retries(fn, *, attempts: int = 3, base_delay_s: float = 0.2):
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except aiosqlite.OperationalError as e:
            last = e
            if not _is_locked_error(e) or i == attempts - 1:
                raise
            delay = base_delay_s * (2**i)
            logger.warning("SQLite locked; retrying in %.2fs", delay)
            await asyncio.sleep(delay)
    if last:
        raise last


async def init_db(db_path: str) -> None:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA busy_timeout=3000;")
        await db.executescript(CREATE_TABLE_SQL)
        # Also create the profiles table (for auto-registration)
        from .profile_db import CREATE_PROFILES_TABLE_SQL
        await db.executescript(CREATE_PROFILES_TABLE_SQL)
        for migration in [
            "ALTER TABLE profiles ADD COLUMN name TEXT NOT NULL DEFAULT '';",
            "ALTER TABLE profiles ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'CASH';",
        ]:
            try:
                await db.execute(migration)
            except aiosqlite.OperationalError:
                pass
        await db.commit()


async def set_subscription(db_path: str, user_id: int, wilaya_code: str) -> None:
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            # UPSERT: if wilaya changes, reset notified
            await db.execute(
                """
                INSERT INTO subscriptions (user_id, Wilaya_code, notified)
                VALUES (?, ?, 0)
                ON CONFLICT(user_id) DO UPDATE SET
                  Wilaya_code=excluded.Wilaya_code,
                  notified=0
                """,
                (user_id, wilaya_code),
            )
            await db.commit()

    await _with_retries(_op)


async def get_subscription(db_path: str, user_id: int) -> Subscription | None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            "SELECT user_id, Wilaya_code, notified, created_at FROM subscriptions WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return Subscription(user_id=int(row[0]), wilaya_code=str(row[1]), notified=int(row[2]), created_at=str(row[3]))


async def delete_subscription(db_path: str, user_id: int) -> bool:
    async def _op() -> bool:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            cur = await db.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
            await db.commit()
            return cur.rowcount > 0

    return bool(await _with_retries(_op))


async def delete_user_data(db_path: str, user_id: int) -> None:
    """Delete all data related to a user (subscriptions, settings, profiles)."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            await db.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM user_settings WHERE user_id=?", (user_id,))
            await db.execute("DELETE FROM profiles WHERE user_id=?", (user_id,))
            await db.commit()

    await _with_retries(_op)


async def get_distinct_wilayas(db_path: str) -> list[str]:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute("SELECT DISTINCT Wilaya_code FROM subscriptions") as cur:
            rows = await cur.fetchall()
            return [str(r[0]) for r in rows]


async def get_user_subscription_wilaya(db_path: str, user_id: int) -> str | None:
    """Return the wilaya code the user is subscribed to, or None."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            "SELECT Wilaya_code FROM subscriptions WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return str(row[0]) if row else None


async def get_subscribers(db_path: str, wilaya_code: str) -> list[int]:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            "SELECT user_id FROM subscriptions WHERE Wilaya_code=?",
            (wilaya_code,),
        ) as cur:
            rows = await cur.fetchall()
            return [int(r[0]) for r in rows]


async def get_subscribers_to_notify(db_path: str, wilaya_code: str) -> list[int]:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            "SELECT user_id FROM subscriptions WHERE Wilaya_code=? AND notified=0",
            (wilaya_code,),
        ) as cur:
            rows = await cur.fetchall()
            return [int(r[0]) for r in rows]


async def get_notified_subscribers(db_path: str, wilaya_code: str) -> list[int]:
    """Return user_ids that have already been notified (notified=1) for *wilaya_code*."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            "SELECT user_id FROM subscriptions WHERE Wilaya_code=? AND notified=1",
            (wilaya_code,),
        ) as cur:
            rows = await cur.fetchall()
            return [int(r[0]) for r in rows]


async def mark_notified(db_path: str, user_ids: list[int], wilaya_code: str) -> None:
    if not user_ids:
        return

    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            await db.executemany(
                "UPDATE subscriptions SET notified=1 WHERE user_id=? AND Wilaya_code=?",
                [(uid, wilaya_code) for uid in user_ids],
            )
            await db.commit()

    await _with_retries(_op)


async def reset_notified_for_wilaya(db_path: str, wilaya_code: str) -> None:
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            await db.execute("UPDATE subscriptions SET notified=0 WHERE Wilaya_code=?", (wilaya_code,))
            await db.commit()

    await _with_retries(_op)


async def get_user_language(db_path: str, user_id: int) -> str:
    """Return user language, defaults to 'ar'."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            "SELECT language FROM user_settings WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return str(row[0]) if row else "ar"


async def set_user_language(db_path: str, user_id: int, language: str) -> None:
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            await db.execute(
                """
                INSERT INTO user_settings (user_id, language)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                  language=excluded.language
                """,
                (user_id, language),
            )
            await db.commit()

    await _with_retries(_op)


async def get_cached_wilayas(db_path: str) -> list[tuple[str, str]]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT code, name FROM wilayas ORDER BY code ASC") as cur:
            rows = await cur.fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]


async def save_wilayas(db_path: str, wilayas: list[dict]) -> None:
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.executemany(
                "INSERT OR REPLACE INTO wilayas (code, name) VALUES (?, ?)",
                [(str(w["code"]), str(w["name"])) for w in wilayas],
            )
            await db.commit()
    await _with_retries(_op)


async def get_cached_communes(db_path: str, wilaya_code: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT code, name, is_active FROM communes WHERE wilaya_code=? ORDER BY name ASC",
            (wilaya_code,),
        ) as cur:
            rows = await cur.fetchall()
            return [{"code": r[0], "name": r[1], "isActive": bool(r[2])} for r in rows]


async def save_communes(db_path: str, wilaya_code: str, communes: list[dict]) -> None:
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.executemany(
                "INSERT OR REPLACE INTO communes (code, wilaya_code, name, is_active) VALUES (?, ?, ?, ?)",
                [(str(c["code"]), str(wilaya_code), str(c["name"]), 1 if c.get("isActive") else 0) for c in communes],
            )
            await db.commit()
    await _with_retries(_op)


async def add_quota_history_entry(db_path: str, wilaya_code: str, event_type: str) -> None:
    """Record an OPEN/CLOSE event for a wilaya."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            await db.execute(
                "INSERT INTO quota_history (wilaya_code, event_type) VALUES (?, ?)",
                (wilaya_code, event_type),
            )
            await db.commit()

    await _with_retries(_op)
async def add_inbox_entry(db_path: str, level: str, message: str, stack_trace: str | None) -> int:
    """Insert a new error/warning entry into the admin inbox."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            cur = await db.execute(
                "INSERT INTO admin_inbox (level, message, stack_trace) VALUES (?, ?, ?)",
                (level, message, stack_trace),
            )
            await db.commit()
            return cur.lastrowid

    return await _with_retries(_op)


async def get_inbox_entries(
    db_path: str, 
    level: str | None = None, 
    status: str | None = None, 
    date_filter: str | None = None,
    offset: int = 0, 
    limit: int = 10
) -> list[dict]:
    """Retrieve a paginated list of inbox entries with optional filters."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        query = "SELECT id, level, message, status, created_at, resolved_at FROM admin_inbox"
        params = []
        where_clauses = ["is_hidden = 0"]

        if level:
            where_clauses.append("level = ?")
            params.append(level)
        if status:
            where_clauses.append("status = ?")
            params.append(status)
        
        if date_filter == "today":
            where_clauses.append("created_at >= date('now')")
        elif date_filter == "week":
            where_clauses.append("created_at >= date('now', '-7 days')")

        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)

        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        async with db.execute(query, params) as cur:
            rows = await cur.fetchall()
            return [
                {
                    "id": r[0],
                    "level": r[1],
                    "message": r[2],
                    "status": r[3],
                    "created_at": r[4],
                    "resolved_at": r[5],
                }
                for r in rows
            ]


async def count_inbox_entries(
    db_path: str, 
    level: str | None = None, 
    status: str | None = None,
    date_filter: str | None = None
) -> int:
    """Count total inbox entries matching the filters."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        query = "SELECT COUNT(*) FROM admin_inbox"
        params = []
        where_clauses = ["is_hidden = 0"]

        if level:
            where_clauses.append("level = ?")
            params.append(level)
        if status:
            where_clauses.append("status = ?")
            params.append(status)
        
        if date_filter == "today":
            where_clauses.append("created_at >= date('now')")
        elif date_filter == "week":
            where_clauses.append("created_at >= date('now', '-7 days')")

        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)

        async with db.execute(query, params) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_inbox_entry(db_path: str, entry_id: int) -> dict | None:
    """Retrieve full details for a single inbox entry."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            "SELECT id, level, message, stack_trace, status, created_at, resolved_at FROM admin_inbox WHERE id = ?",
            (entry_id,),
        ) as cur:
            r = await cur.fetchone()
            if not r:
                return None
            return {
                "id": r[0],
                "level": r[1],
                "message": r[2],
                "stack_trace": r[3],
                "status": r[4],
                "created_at": r[5],
                "resolved_at": r[6],
            }


async def resolve_inbox_entry(db_path: str, entry_id: int) -> bool:
    """Mark an inbox entry as resolved."""
    async def _op() -> bool:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            cur = await db.execute(
                "UPDATE admin_inbox SET status = 'resolved', resolved_at = datetime('now') WHERE id = ?",
                (entry_id,),
            )
            await db.commit()
            return cur.rowcount > 0

    return bool(await _with_retries(_op))


async def get_all_user_ids(db_path: str) -> list[int]:
    """Return all unique user IDs across subscriptions, settings, and profiles."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        # Union all tables that contain user_id to find everyone the bot knows about
        query = """
        SELECT user_id FROM subscriptions
        UNION
        SELECT user_id FROM user_settings
        UNION
        SELECT user_id FROM profiles
        """
        async with db.execute(query) as cur:
            rows = await cur.fetchall()
            return [int(r[0]) for r in rows]

async def add_sync_event(db_path: str, event_type: str, profile_id: int | None = None, user_id: int | None = None, details: str | None = None) -> None:
    """Record a sync event for analytics."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            await db.execute(
                "INSERT INTO sync_history (event_type, profile_id, user_id, details) VALUES (?, ?, ?, ?)",
                (event_type, profile_id, user_id, details),
            )
            await db.commit()

    await _with_retries(_op)


async def get_global_setting(db_path: str, key: str, default: str | None = None) -> str | None:
    """Retrieve a global setting value."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute("SELECT value FROM global_settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return str(row[0]) if row else default


async def set_global_setting(db_path: str, key: str, value: str) -> None:
    """Update or insert a global setting."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            await db.execute(
                "INSERT INTO global_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await db.commit()

    await _with_retries(_op)


async def hide_all_inbox_entries(db_path: str) -> int:
    """Soft-delete all current inbox entries by marking them as hidden."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            cur = await db.execute("UPDATE admin_inbox SET is_hidden = 1 WHERE is_hidden = 0")
            await db.commit()
            return cur.rowcount

    return await _with_retries(_op)
