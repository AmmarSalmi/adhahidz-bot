"""Database operations for registration profiles."""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


CREATE_PROFILES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS profiles (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL,
  priority      INTEGER NOT NULL DEFAULT 0,
  name          TEXT NOT NULL DEFAULT '',
  nin           TEXT NOT NULL,
  cnibe         TEXT NOT NULL,
  phone         TEXT NOT NULL,
  password      TEXT NOT NULL,
  wilaya_id     INTEGER NOT NULL,
  wilaya_name   TEXT NOT NULL DEFAULT '',
  commune_code  TEXT NOT NULL,
  commune_name  TEXT NOT NULL DEFAULT '',
  email         TEXT NOT NULL DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'pending',
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@dataclass(frozen=True)
class Profile:
    id: int
    user_id: int
    priority: int
    name: str
    nin: str
    cnibe: str
    phone: str
    password: str
    wilaya_id: int
    wilaya_name: str
    commune_code: str
    commune_name: str
    email: str
    status: str
    created_at: str


def _row_to_profile(row: tuple) -> Profile:
    return Profile(
        id=int(row[0]),
        user_id=int(row[1]),
        priority=int(row[2]),
        name=str(row[3]),
        nin=str(row[4]),
        cnibe=str(row[5]),
        phone=str(row[6]),
        password=str(row[7]),
        wilaya_id=int(row[8]),
        wilaya_name=str(row[9]),
        commune_code=str(row[10]),
        commune_name=str(row[11]),
        email=str(row[12]),
        status=str(row[13]),
        created_at=str(row[14]),
    )


_SELECT_COLS = (
    "id, user_id, priority, name, nin, cnibe, phone, password, "
    "wilaya_id, wilaya_name, commune_code, commune_name, email, status, created_at"
)


def _is_locked_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "locked" == msg.strip()


async def _with_retries(fn, *, attempts: int = 3, base_delay_s: float = 0.2):
    last: Exception | None = None
    for i in range(attempts):
        try:
            return await fn()
        except aiosqlite.OperationalError as e:
            last = e
            if not _is_locked_error(e) or i == attempts - 1:
                raise
            delay = base_delay_s * (2 ** i)
            logger.warning("SQLite locked; retrying in %.2fs", delay)
            await asyncio.sleep(delay)
    if last:
        raise last


async def init_profiles_table(db_path: str) -> None:
    """Create the profiles table if it doesn't exist."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        await db.execute(CREATE_PROFILES_TABLE_SQL)
        try:
            await db.execute("ALTER TABLE profiles ADD COLUMN name TEXT NOT NULL DEFAULT '';")
        except aiosqlite.OperationalError:
            pass
        await db.commit()


async def add_profile(db_path: str, user_id: int, data: dict[str, Any]) -> int:
    """Insert a new profile and return its id."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            # Set priority to max+1 for this user
            async with db.execute(
                "SELECT COALESCE(MAX(priority), -1) + 1 FROM profiles WHERE user_id=?",
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
                next_priority = int(row[0]) if row else 0

            cursor = await db.execute(
                """
                INSERT INTO profiles (user_id, priority, name, nin, cnibe, phone, password,
                    wilaya_id, wilaya_name, commune_code, commune_name, email)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    next_priority,
                    data.get("name", ""),
                    data["nin"],
                    data["cnibe"],
                    data["phone"],
                    data["password"],
                    data["wilaya_id"],
                    data.get("wilaya_name", ""),
                    data["commune_code"],
                    data.get("commune_name", ""),
                    data.get("email", ""),
                ),
            )
            await db.commit()
            return cursor.lastrowid

    return await _with_retries(_op)


async def get_profiles(db_path: str, user_id: int) -> list[Profile]:
    """Return all profiles for a user, sorted by priority."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            f"SELECT {_SELECT_COLS} FROM profiles WHERE user_id=? ORDER BY priority",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_profile(r) for r in rows]


async def get_profile(db_path: str, profile_id: int, user_id: int) -> Profile | None:
    """Get a single profile by id (scoped to user)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            f"SELECT {_SELECT_COLS} FROM profiles WHERE id=? AND user_id=?",
            (profile_id, user_id),
        ) as cur:
            row = await cur.fetchone()
            return _row_to_profile(row) if row else None


async def update_profile_field(
    db_path: str, profile_id: int, user_id: int, field: str, value: Any
) -> bool:
    """Update a single field on a profile. Returns True if updated."""
    allowed_fields = {
        "name", "nin", "cnibe", "phone", "password", "wilaya_id", "wilaya_name",
        "commune_code", "commune_name", "email", "status",
    }
    if field not in allowed_fields:
        raise ValueError(f"Cannot update field: {field}")

    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            cur = await db.execute(
                f"UPDATE profiles SET {field}=? WHERE id=? AND user_id=?",
                (value, profile_id, user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    return bool(await _with_retries(_op))


async def delete_profile(db_path: str, profile_id: int, user_id: int) -> bool:
    """Delete a profile. Returns True if deleted."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            cur = await db.execute(
                "DELETE FROM profiles WHERE id=? AND user_id=?",
                (profile_id, user_id),
            )
            await db.commit()
            return cur.rowcount > 0

    return bool(await _with_retries(_op))


async def reorder_profiles(db_path: str, user_id: int, id_order: list[int]) -> None:
    """Set new priority order. id_order[0] gets priority 0, etc."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            for priority, profile_id in enumerate(id_order):
                await db.execute(
                    "UPDATE profiles SET priority=? WHERE id=? AND user_id=?",
                    (priority, profile_id, user_id),
                )
            await db.commit()

    await _with_retries(_op)


async def get_pending_profiles_for_wilaya(
    db_path: str, wilaya_code: str
) -> list[Profile]:
    """Find all pending profiles matching a wilaya code, ordered by priority."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            f"SELECT {_SELECT_COLS} FROM profiles "
            "WHERE CAST(wilaya_id AS TEXT)=? AND status='pending' "
            "ORDER BY priority",
            (str(wilaya_code),),
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_profile(r) for r in rows]


async def get_distinct_profile_wilayas(db_path: str) -> list[str]:
    """Return distinct wilaya codes that have at least one pending profile."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            "SELECT DISTINCT CAST(wilaya_id AS TEXT) FROM profiles WHERE status='pending'"
        ) as cur:
            rows = await cur.fetchall()
            return [str(r[0]) for r in rows]


async def set_profile_status(db_path: str, profile_id: int, status: str) -> None:
    """Update profile status."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            await db.execute(
                "UPDATE profiles SET status=? WHERE id=?",
                (status, profile_id),
            )
            await db.commit()

    await _with_retries(_op)
