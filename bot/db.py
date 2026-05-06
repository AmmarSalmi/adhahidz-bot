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
        await db.execute(CREATE_TABLE_SQL)
        # Also create the profiles table (for auto-registration)
        from .profile_db import CREATE_PROFILES_TABLE_SQL
        await db.execute(CREATE_PROFILES_TABLE_SQL)
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
