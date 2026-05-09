"""Admin board — hidden admin panel for bot management.

Entry command: /adminammar (NOT exposed in setMyCommands or /help).
Access is restricted to the Telegram user ID set in ADMIN_TELEGRAM_ID env var.

Restricted mode
---------------
When ``restricted_mode`` is ON (toggled from the admin panel), all bot
features are locked for non-admin users *except*:
  - /start, /change, /help  (subscription & wilaya setup)
  - Automatic quota-open notifications from the scheduler
Any other command or button press from a regular user will reply with a
friendly "access is restricted" message.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Admin identity — loaded once from env at import time
# ---------------------------------------------------------------------------

_raw_admin_id = os.getenv("ADMIN_TELEGRAM_ID", "").strip()

if not _raw_admin_id:
    logger.warning(
        "ADMIN_TELEGRAM_ID is not set — admin panel will be disabled for all users."
    )
    ADMIN_TELEGRAM_ID: int | None = None
else:
    try:
        ADMIN_TELEGRAM_ID = int(_raw_admin_id)
    except ValueError:
        logger.warning(
            "ADMIN_TELEGRAM_ID=%r is not a valid integer — admin panel disabled.",
            _raw_admin_id,
        )
        ADMIN_TELEGRAM_ID = None


# ---------------------------------------------------------------------------
# Guard helper — reusable across all admin handlers
# ---------------------------------------------------------------------------

def is_admin(update: Update) -> bool:
    """Return True if the message/callback originates from the configured admin."""
    if ADMIN_TELEGRAM_ID is None:
        return False
    user = update.effective_user
    if user is None:
        return False
    # Compare as int; Telegram always provides user.id as int, but be safe.
    return int(user.id) == ADMIN_TELEGRAM_ID


# ---------------------------------------------------------------------------
# Restricted-mode helpers
# ---------------------------------------------------------------------------

def is_restricted_mode(context) -> bool:
    """Return True if the bot is currently in restricted mode."""
    return bool(context.application.bot_data.get("restricted_mode", False))


async def check_restricted(update: Update, context) -> bool:
    """Guard for non-admin users when restricted mode is active.

    Returns True if the user is *blocked* (i.e. restricted mode is on
    and the user is not the admin).  The caller should ``return`` early
    when this returns True.
    """
    if not is_restricted_mode(context):
        return False  # not restricted — allow
    if is_admin(update):
        return False  # admin always has access
    # Blocked — inform user
    await update.effective_message.reply_text(
        "🔒 *Access Restricted*\n\n"
        "Bot features are currently restricted by the admin.\n"
        "You can still receive notifications when quota opens, "
        "and use /start or /change to set up your wilaya.\n\n"
        "Please try again later.",
        parse_mode="Markdown",
    )
    return True


# ---------------------------------------------------------------------------
# /adminammar — hidden entry point
# ---------------------------------------------------------------------------

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the admin dashboard when the admin calls /adminammar."""
    if not is_admin(update):
        # Silently ignore non-admin users
        return

    keyboard = _admin_keyboard(context)
    await update.effective_message.reply_text(
        "👑 *Admin Panel*\n\nWelcome back, boss.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Callback: admin:stats — query database for user / profile statistics
# ---------------------------------------------------------------------------

async def on_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the 📊 User Statistics button press."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return

    db_path: str = context.application.bot_data.get("db_path", "")
    if not db_path:
        await query.edit_message_text("❌ Database path not configured.")
        return

    try:
        stats = await _gather_stats(db_path)
    except Exception:
        logger.exception("Failed to gather admin stats")
        await query.edit_message_text("❌ Failed to query statistics.")
        return

    lines = [
        "📊 *User Statistics*\n",
        f"👤 Total registered users (subscriptions): *{stats['total_subscriptions']}*",
        f"📝 Total registration profiles: *{stats['total_profiles']}*",
        "",
        "*Profile breakdown by status:*",
    ]
    for status_name, count in stats["profiles_by_status"]:
        lines.append(f"  • `{status_name}`: {count}")

    lines.append("")
    lines.append(f"🕐 Subscriptions today: *{stats['subs_today']}*")
    lines.append(f"📅 Subscriptions this week: *{stats['subs_week']}*")
    lines.append(f"🕐 Profiles created today: *{stats['profiles_today']}*")
    lines.append(f"📅 Profiles created this week: *{stats['profiles_week']}*")

    # Back button
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data="admin:back")]]
    )

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def on_admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the ⬅️ Back button — return to admin dashboard."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return

    keyboard = _admin_keyboard(context)
    await query.edit_message_text(
        "👑 *Admin Panel*\n\nWelcome back, boss.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Admin keyboard builder
# ---------------------------------------------------------------------------

def _admin_keyboard(context) -> InlineKeyboardMarkup:
    """Build the admin panel keyboard with the current restricted-mode state."""
    restricted = is_restricted_mode(context)
    toggle_label = "🔓 Unrestrict Users" if restricted else "🔒 Restrict Users"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 User Statistics", callback_data="admin:stats")],
            [InlineKeyboardButton(toggle_label, callback_data="admin:toggle_restrict")],
        ]
    )


# ---------------------------------------------------------------------------
# Callback: admin:toggle_restrict — flip restricted mode on/off
# ---------------------------------------------------------------------------

async def on_admin_toggle_restrict(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Toggle restricted mode on or off."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return

    current = is_restricted_mode(context)
    context.application.bot_data["restricted_mode"] = not current
    new_state = not current

    logger.info("Admin toggled restricted_mode → %s", new_state)

    status_emoji = "🔒" if new_state else "🔓"
    status_text = "ON — users are restricted" if new_state else "OFF — users have full access"

    keyboard = _admin_keyboard(context)
    await query.edit_message_text(
        f"👑 *Admin Panel*\n\n"
        f"{status_emoji} Restricted mode: *{status_text}*",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Database queries for statistics
# ---------------------------------------------------------------------------

async def _gather_stats(db_path: str) -> dict:
    """Collect all admin statistics from the database in a single connection."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout=3000;")

        # Total subscriptions
        async with db.execute("SELECT COUNT(*) FROM subscriptions") as cur:
            total_subs = (await cur.fetchone())[0]

        # Subscriptions today
        async with db.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE created_at >= ?",
            (today_start,),
        ) as cur:
            subs_today = (await cur.fetchone())[0]

        # Subscriptions this week
        async with db.execute(
            "SELECT COUNT(*) FROM subscriptions WHERE created_at >= ?",
            (week_start,),
        ) as cur:
            subs_week = (await cur.fetchone())[0]

        # Total profiles
        async with db.execute("SELECT COUNT(*) FROM profiles") as cur:
            total_profiles = (await cur.fetchone())[0]

        # Profiles by status
        async with db.execute(
            "SELECT status, COUNT(*) FROM profiles GROUP BY status ORDER BY status"
        ) as cur:
            profiles_by_status = [(str(r[0]), int(r[1])) for r in await cur.fetchall()]

        # Profiles created today
        async with db.execute(
            "SELECT COUNT(*) FROM profiles WHERE created_at >= ?",
            (today_start,),
        ) as cur:
            profiles_today = (await cur.fetchone())[0]

        # Profiles created this week
        async with db.execute(
            "SELECT COUNT(*) FROM profiles WHERE created_at >= ?",
            (week_start,),
        ) as cur:
            profiles_week = (await cur.fetchone())[0]

    return {
        "total_subscriptions": total_subs,
        "subs_today": subs_today,
        "subs_week": subs_week,
        "total_profiles": total_profiles,
        "profiles_by_status": profiles_by_status,
        "profiles_today": profiles_today,
        "profiles_week": profiles_week,
    }
