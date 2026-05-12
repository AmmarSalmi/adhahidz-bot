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
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id         INTEGER NOT NULL,
  priority        INTEGER NOT NULL DEFAULT 0,
  name            TEXT NOT NULL DEFAULT '',
  nin             TEXT NOT NULL,
  cnibe           TEXT NOT NULL,
  phone           TEXT NOT NULL,
  password        TEXT NOT NULL,
  wilaya_id       INTEGER NOT NULL,
  wilaya_name     TEXT NOT NULL DEFAULT '',
  commune_code    TEXT NOT NULL,
  commune_name    TEXT NOT NULL DEFAULT '',
  email           TEXT NOT NULL DEFAULT '',
  payment_method  TEXT NOT NULL DEFAULT 'CASH',
  status          TEXT NOT NULL DEFAULT 'pending',
  is_valid        INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
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
    payment_method: str
    status: str
    is_valid: int
    created_at: str
    is_synced: int = 0


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
        payment_method=str(row[13]),
        status=str(row[14]),
        is_valid=int(row[15]),
        created_at=str(row[16]),
        is_synced=int(row[17]) if len(row) > 17 else 0,
    )


_SELECT_COLS = (
    "id, user_id, priority, name, nin, cnibe, phone, password, "
    "wilaya_id, wilaya_name, commune_code, commune_name, email, payment_method, status, is_valid, created_at, is_synced"
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
        # Migrations for columns added after initial schema
        for migration in [
            "ALTER TABLE profiles ADD COLUMN name TEXT NOT NULL DEFAULT '';",
            "ALTER TABLE profiles ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'CASH';",
            "ALTER TABLE profiles ADD COLUMN is_valid INTEGER NOT NULL DEFAULT 1;",
            "ALTER TABLE profiles ADD COLUMN is_synced INTEGER NOT NULL DEFAULT 0;",
        ]:
            try:
                await db.execute(migration)
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
                    wilaya_id, wilaya_name, commune_code, commune_name, email, payment_method)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    data.get("payment_method", "CASH"),
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


async def get_profile_by_id_admin(db_path: str, profile_id: int) -> Profile | None:
    """Get a single profile by id (admin use, no user restriction)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            f"SELECT {_SELECT_COLS} FROM profiles WHERE id=?",
            (profile_id,),
        ) as cur:
            row = await cur.fetchone()
            return _row_to_profile(row) if row else None


async def update_profile_field(
    db_path: str, profile_id: int, user_id: int, field: str, value: Any
) -> bool:
    """Update a single field on a profile. Returns True if updated."""
    allowed_fields = {
        "name", "nin", "cnibe", "phone", "password", "wilaya_id", "wilaya_name",
        "commune_code", "commune_name", "email", "payment_method", "status",
        "is_valid", "priority"
    }
    if field not in allowed_fields:
        raise ValueError(f"Cannot update field: {field}")

    # Fields that affect registration validity — reset sync state so
    # Global Sync re-evaluates this profile with the updated data.
    _SYNC_RESET_FIELDS = {"nin", "password"}

    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            cur = await db.execute(
                f"UPDATE profiles SET {field}=? WHERE id=? AND user_id=?",
                (value, profile_id, user_id),
            )
            if field in _SYNC_RESET_FIELDS and cur.rowcount > 0:
                await db.execute(
                    "UPDATE profiles SET is_synced=0, is_valid=1 WHERE id=? AND user_id=?",
                    (profile_id, user_id),
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
            "WHERE CAST(wilaya_id AS TEXT)=? AND status='pending' AND is_valid=1 "
            "ORDER BY priority",
            (str(wilaya_code),),
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_profile(r) for r in rows]


async def get_profiles_for_wilaya_by_statuses(
    db_path: str, wilaya_code: str, statuses: list[str]
) -> list[Profile]:
    """Find all profiles matching a wilaya code and any of the given statuses."""
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            f"SELECT {_SELECT_COLS} FROM profiles "
            f"WHERE CAST(wilaya_id AS TEXT)=? AND status IN ({placeholders}) "
            "ORDER BY priority",
            (str(wilaya_code), *statuses),
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_profile(r) for r in rows]


async def get_all_profiles_by_status(
    db_path: str, status: str
) -> list[Profile]:
    """Return all profiles with the given status across all users, ordered by user_id then priority."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            f"SELECT {_SELECT_COLS} FROM profiles WHERE status=? ORDER BY user_id, priority",
            (status,),
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_profile(r) for r in rows]


async def get_all_profiles_grouped_by_user(db_path: str) -> dict[int, list[Profile]]:
    """Return all profiles grouped by user_id, each list sorted by priority."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            f"SELECT {_SELECT_COLS} FROM profiles ORDER BY user_id, priority"
        ) as cur:
            rows = await cur.fetchall()
            res: dict[int, list[Profile]] = {}
            for r in rows:
                p = _row_to_profile(r)
                if p.user_id not in res:
                    res[p.user_id] = []
                res[p.user_id].append(p)
            return res


async def get_distinct_profile_wilayas(db_path: str) -> list[str]:
    """Return distinct wilaya codes that have at least one pending, registered, or pre-registered profile."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            "SELECT DISTINCT CAST(wilaya_id AS TEXT) FROM profiles "
            "WHERE status IN ('pending', 'registered', 'pre-registered') AND is_valid=1"
        ) as cur:
            rows = await cur.fetchall()
            return [str(r[0]) for r in rows]


async def get_user_profile_wilayas(db_path: str, user_id: int) -> list[str]:
    """Return distinct wilaya codes for a specific user's active profiles."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            "SELECT DISTINCT CAST(wilaya_id AS TEXT) FROM profiles "
            "WHERE user_id=? AND status IN ('pending', 'registered', 'pre-registered') AND is_valid=1",
            (user_id,),
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


async def get_profiles_by_status(
    db_path: str, user_id: int, status: str
) -> list[Profile]:
    """Return all profiles for a user with a given status, sorted by priority."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            f"SELECT {_SELECT_COLS} FROM profiles WHERE user_id=? AND status=? ORDER BY priority",
            (user_id, status),
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_profile(r) for r in rows]


async def get_actionable_profiles_prioritized(
    db_path: str, wilaya_code: str, statuses: list[str], priority_user_id: int | None = None
) -> list[Profile]:
    """
    Find actionable profiles for a wilaya, ordered by priority user, then user seniority then priority.
    User seniority is determined by the creation date of their oldest profile.
    """
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    
    # We use a CTE to find the 'seniority' of each user (their first profile date)
    # then join that back to the profiles to sort them.
    # If priority_user_id is provided, those profiles come first.
    
    order_by_clause = "us.first_profile_at ASC, p.priority ASC"
    params = [str(wilaya_code), *statuses]
    
    if priority_user_id is not None:
        # CASE WHEN p.user_id = ? THEN 0 ELSE 1 END puts the priority user at the top (0 < 1)
        order_by_clause = f"CASE WHEN p.user_id = ? THEN 0 ELSE 1 END, {order_by_clause}"
        params.insert(0, priority_user_id)

    query = f"""
    WITH UserSeniority AS (
        SELECT user_id, MIN(created_at) as first_profile_at
        FROM profiles
        GROUP BY user_id
    )
    SELECT p.id, p.user_id, p.priority, p.name, p.nin, p.cnibe, p.phone, p.password,
           p.wilaya_id, p.wilaya_name, p.commune_code, p.commune_name, p.email, 
           p.payment_method, p.status, p.is_valid, p.created_at
    FROM profiles p
    JOIN UserSeniority us ON p.user_id = us.user_id
    WHERE CAST(p.wilaya_id AS TEXT) = ? AND p.status IN ({placeholders}) AND p.is_valid = 1
    ORDER BY {order_by_clause}
    """
    
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(query, tuple(params)) as cur:
            rows = await cur.fetchall()
            return [_row_to_profile(r) for r in rows]

async def reset_registering_profiles(db_path: str) -> int:
    """Reset all 'registering' profiles to 'pending' at startup. Returns count."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            cur = await db.execute(
                "UPDATE profiles SET status='pending' WHERE status='registering'"
            )
            await db.commit()
            return cur.rowcount

    return await _with_retries(_op)


async def get_unsynced_profiles(db_path: str) -> list[Profile]:
    """Return all profiles where is_synced=0, ordered by user_id then priority."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute(
            f"SELECT {_SELECT_COLS} FROM profiles WHERE is_synced=0 ORDER BY user_id, priority"
        ) as cur:
            rows = await cur.fetchall()
            return [_row_to_profile(r) for r in rows]


async def get_total_profile_count(db_path: str) -> int:
    """Return total number of profiles in the database."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")
        async with db.execute("SELECT COUNT(*) FROM profiles") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def mark_profile_synced(db_path: str, profile_id: int) -> None:
    """Mark a profile as synced (is_synced=1)."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            await db.execute("UPDATE profiles SET is_synced=1 WHERE id=?", (profile_id,))
            await db.commit()

    await _with_retries(_op)


async def reset_profile_sync_on_edit(db_path: str, profile_id: int) -> None:
    """Reset is_synced=0 and is_valid=1 after user edits NIN or password."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            await db.execute(
                "UPDATE profiles SET is_synced=0, is_valid=1 WHERE id=?",
                (profile_id,),
            )
            await db.commit()

    await _with_retries(_op)


async def set_profile_invalid(db_path: str, profile_id: int) -> None:
    """Mark profile as invalid (is_valid=0) and synced (is_synced=1)."""
    async def _op():
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            await db.execute(
                "UPDATE profiles SET is_valid=0, is_synced=1 WHERE id=?",
                (profile_id,),
            )
            await db.commit()

    await _with_retries(_op)
