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

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
from deep_translator import GoogleTranslator
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, constants
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import Forbidden

from . import profile_db, db as db_mod

logger = logging.getLogger(__name__)

# Conversation states for broadcasting
AWAIT_BROADCAST_MESSAGE = 1
AWAIT_BROADCAST_CONFIRM = 2
AWAIT_PROXY_TEST_CONFIG = 3
AWAIT_CONCURRENCY_LIMIT = 4
AWAIT_WILAYA_INTERVAL = 5
AWAIT_PROFILE_ID_CHECK = 6

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
    
    warning = ""
    host = os.getenv("PROXY_HOST", "gw.databay.co")
    port = os.getenv("PROXY_PORT", "8888")
    is_standard = host in ("gw.databay.co", "eu-gw.databay.co") and port == "8888"
    if not is_standard:
        warning = (
            "\n\n⚠️ *Warning: Proxy Config*\n"
            f"Host `{host}:{port}` deviates from Databay defaults (`gw.databay.co:8888`)."
        )

    await update.effective_message.reply_text(
        f"👑 *Admin Panel*\n\nWelcome back, boss.{warning}",
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
        lines.append(f"  • {status_name.capitalize()}: *{count}*")
    
    lines.append("")
    lines.append("*Compliance Gate Statistics:*")
    valid_count = 0
    invalid_count = 0
    for is_valid, count in stats["profiles_by_validity"]:
        if is_valid == 1:
            valid_count = count
        else:
            invalid_count = count
    
    lines.append(f"  • ✅ Valid (Included): *{valid_count}*")
    lines.append(f"  • ❌ Invalid (Excluded): *{invalid_count}*")

    lines.append("")
    lines.append(f"🕐 Subscriptions today: *{stats['subs_today']}*")
    lines.append(f"📅 Subscriptions this week: *{stats['subs_week']}*")
    lines.append(f"🕐 Profiles created today: *{stats['profiles_today']}*")
    lines.append(f"📅 Profiles created this week: *{stats['profiles_week']}*")

    if stats.get("recent_history"):
        lines.append("")
        lines.append("*Recent Quota Events (last 10):*")
        for code, event, ts in stats["recent_history"]:
            # Format: '2026-05-10 00:17:10' -> '00:17:10'
            short_ts = ts.split(" ")[1] if " " in ts else ts
            emoji = "✅" if event == "OPEN" else "❌"
            lines.append(f"  • `{short_ts}` {emoji} `{code}`")

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
    """Build the admin panel keyboard with current states."""
    restricted = is_restricted_mode(context)
    
    toggle_restrict = "🔓 Unrestrict Users" if restricted else "🔒 Restrict Users"
    
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Force Check Profiles", callback_data="admin:force_check")],
            [InlineKeyboardButton("🤫 Silent Force Check", callback_data="admin:force_check:silent")],
            [InlineKeyboardButton("🆔 Check Profile by ID", callback_data="admin:check_profile_start")],
            [InlineKeyboardButton("📊 User Statistics", callback_data="admin:stats")],
            [InlineKeyboardButton("📢 Message All Users", callback_data="admin:broadcast_start")],
            [InlineKeyboardButton(toggle_restrict, callback_data="admin:toggle_restrict")],
            [InlineKeyboardButton("🌐 Proxy Settings ⚙️", callback_data="admin:proxy_submenu")],
            [InlineKeyboardButton("⚙️ Set Concurrency Limit", callback_data="admin:set_concurrency")],
            [InlineKeyboardButton("🧹 Purge Blocking Users", callback_data="admin:purge_blockers")],
            [InlineKeyboardButton("📥 Error/Warning Inbox", callback_data="admin:inbox:0")],
            [InlineKeyboardButton("🧪 Test Proxy (Custom)", callback_data="admin:test_proxy")],
        ]
    )


def _proxy_submenu_keyboard(context) -> InlineKeyboardMarkup:
    """Build the proxy settings submenu keyboard."""
    p_wilaya = context.application.bot_data.get("proxy_wilaya", False)
    p_autoreg = context.application.bot_data.get("proxy_autoreg", False)
    p_checkprof = context.application.bot_data.get("proxy_checkprof", False)
    
    t_wilaya = f"📊 Quota Check: {'✅ ON' if p_wilaya else '❌ OFF'}"
    t_autoreg = f"🤖 Auto-Reg: {'✅ ON' if p_autoreg else '❌ OFF'}"
    t_checkprof = f"🔍 Profile Check: {'✅ ON' if p_checkprof else '❌ OFF'}"
    
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t_wilaya, callback_data="admin:toggle_proxy:wilaya")],
        [InlineKeyboardButton(t_autoreg, callback_data="admin:toggle_proxy:autoreg")],
        [InlineKeyboardButton(t_checkprof, callback_data="admin:toggle_proxy:checkprof")],
        [InlineKeyboardButton("⬅️ Back", callback_data="admin:back")]
    ])


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


async def on_admin_proxy_submenu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show the proxy settings submenu."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return

    keyboard = _proxy_submenu_keyboard(context)
    await query.edit_message_text(
        "🌐 *Proxy Management*\n\n"
        "Configure where the Databay residential proxy should be used:",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


async def on_admin_toggle_proxy(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    """Toggle specific proxy usage settings."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return

    data = query.data or ""
    # Format: admin:toggle_proxy:<key>
    parts = data.split(":")
    if len(parts) < 3:
        # Compatibility with old toggle if any
        return

    key_suffix = parts[2]
    key = f"proxy_{key_suffix}"
    
    current = context.application.bot_data.get(key, False)
    context.application.bot_data[key] = not current
    new_state = not current

    logger.info("Admin toggled %s → %s", key, new_state)
    
    if key_suffix == "wilaya" and new_state:
        current_interval = context.application.bot_data.get("check_interval_seconds", 300)
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="admin:proxy_cancel")]]
        )
        await query.edit_message_text(
            f"🌐 *Proxy Enabled for Quota Checks*\n\n"
            f"Current check interval: `{current_interval}` seconds.\n\n"
            "Please enter the new interval in *seconds* (e.g., `60` for 1 minute):",
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        return AWAIT_WILAYA_INTERVAL

    keyboard = _proxy_submenu_keyboard(context)
    await query.edit_message_text(
        "🌐 *Proxy Management*\n\n"
        "Settings updated successfully.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def on_admin_set_concurrency(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Start the concurrency limit setting flow."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return ConversationHandler.END

    current = context.application.bot_data.get("max_concurrent_sessions", 50)
    
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="admin:broadcast_cancel")]]
    )
    await query.edit_message_text(
        f"⚙️ *Set Concurrency Limit*\n\n"
        f"Current limit: `{current}` connections.\n\n"
        "Please enter a new integer between *1 and 1000*:",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAIT_CONCURRENCY_LIMIT


async def on_admin_concurrency_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the new concurrency limit and update the semaphore."""
    if not is_admin(update):
        return ConversationHandler.END

    msg = update.message
    if not msg or not msg.text:
        return AWAIT_CONCURRENCY_LIMIT

    try:
        val = int(msg.text.strip())
        if not (1 <= val <= 1000):
            raise ValueError("Out of range")
    except ValueError:
        await msg.reply_text("❌ *Invalid Input*\n\nPlease enter an integer between *1 and 1000*.", parse_mode="Markdown")
        return AWAIT_CONCURRENCY_LIMIT

    # Update both the number and the semaphore instance
    context.application.bot_data["max_concurrent_sessions"] = val
    import asyncio
    context.application.bot_data["concurrency_semaphore"] = asyncio.Semaphore(val)

    logger.info("Admin updated concurrency limit to %d", val)

    await msg.reply_text(
        f"✅ *Concurrency Limit Updated*\n\n"
        f"New limit: `{val}` connections.\n"
        "This will take effect for the next registration cycle.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def on_admin_wilaya_interval_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the new wilaya check interval and update the scheduler."""
    if not is_admin(update):
        return ConversationHandler.END

    msg = update.message
    if not msg or not msg.text:
        return AWAIT_WILAYA_INTERVAL

    try:
        val = int(msg.text.strip())
        if not (10 <= val <= 3600):
            raise ValueError("Out of range")
    except ValueError:
        await msg.reply_text(
            "❌ *Invalid Input*\n\nPlease enter an integer between *10 and 3600* seconds.", 
            parse_mode="Markdown"
        )
        return AWAIT_WILAYA_INTERVAL

    # Update data and reschedule
    context.application.bot_data["check_interval_seconds"] = val
    from .scheduler import update_poll_interval
    update_poll_interval(context.application, val)

    logger.info("Admin updated wilaya poll interval to %d seconds", val)

    await msg.reply_text(
        f"✅ *Poll Interval Updated*\n\n"
        f"New interval: `{val}` seconds.\n"
        "The scheduler has been updated.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def on_admin_proxy_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the interval update and return to proxy menu."""
    query = update.callback_query
    if query:
        await query.answer()
        keyboard = _proxy_submenu_keyboard(context)
        await query.edit_message_text(
            "🌐 *Proxy Management*\n\n"
            "Interval update cancelled.",
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    return ConversationHandler.END


async def on_admin_test_proxy(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Start the proxy test configuration flow."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="admin:broadcast_cancel")]]
    )
    await query.edit_message_text(
        "🧪 *Proxy Test Configuration*\n\n"
        "Please enter the test parameters in the format:\n"
        "`TotalAttempts|BatchSize|IntervalSeconds`\n\n"
        "Example: `20|5|60` (20 total, batches of 5, every 60s)",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAIT_PROXY_TEST_CONFIG


async def on_admin_proxy_test_config_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Parse proxy test config and start the task."""
    if not is_admin(update):
        return ConversationHandler.END

    msg = update.message
    if not msg or not msg.text:
        return AWAIT_PROXY_TEST_CONFIG

    try:
        parts = [int(p.strip()) for p in msg.text.split("|")]
        if len(parts) != 3:
            raise ValueError("Need exactly 3 parts")
        total, batch_size, interval = parts
        if total <= 0 or batch_size <= 0 or interval < 0:
            raise ValueError("Values must be positive")
    except ValueError:
        await msg.reply_text("❌ *Invalid Format*\n\nPlease use: `Total|Batch|Interval` (e.g. `10|1|30`)", parse_mode="Markdown")
        return AWAIT_PROXY_TEST_CONFIG

    proxy_url = get_proxy_url()
    if not proxy_url:
        await msg.reply_text("❌ Proxy credentials missing in `.env`.")
        return ConversationHandler.END

    # Start the task
    asyncio.create_task(_run_proxy_test(
        app=context.application,
        user_id=update.effective_user.id,
        proxy_url=proxy_url,
        total=total,
        batch_size=batch_size,
        interval=interval
    ))

    await msg.reply_text(
        f"✅ *Proxy Test Queued*\n\n"
        f"Total: {total}\n"
        f"Batch Size: {batch_size}\n"
        f"Interval: {interval}s",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def _run_proxy_test(
    app, 
    user_id: int, 
    proxy_url: str,
    total: int = 10,
    batch_size: int = 1,
    interval: int = 30
) -> None:
    """Background task with custom concurrency and interval."""
    import time
    
    api_client = app.bot_data.get("api_client")
    if not api_client:
        return

    success_count = 0
    results = []
    
    status_msg = await app.bot.send_message(
        chat_id=user_id,
        text=f"🧪 *Proxy Test Progress: 0/{total}*...",
        parse_mode="Markdown"
    )

    completed = 0
    while completed < total:
        current_batch_size = min(batch_size, total - completed)
        tasks = []
        
        # Prepare batch
        for i in range(current_batch_size):
            attempt_num = completed + i + 1
            tasks.append(_single_proxy_attempt(api_client, proxy_url, attempt_num))
        
        # Execute batch concurrently
        batch_results = await asyncio.gather(*tasks)
        
        for success, text in batch_results:
            if success:
                success_count += 1
            results.append(text)
        
        completed += current_batch_size
        
        # Update progress
        try:
            summary = f"🧪 *Proxy Test Progress: {completed}/{total}*\nSuccesses: {success_count}"
            if results:
                summary += f"\nLast result: {results[-1]}"
            await status_msg.edit_text(summary, parse_mode="Markdown")
        except Exception:
            pass

        if completed < total:
            await asyncio.sleep(interval)

    # Final report
    final_text = [
        f"🧪 *Proxy Test Result: {success_count}/{total} Success*",
        "",
        "\n".join(results[-20:]), # Show last 20 results if too many
    ]
    if len(results) > 20:
        final_text.insert(2, f"_(Showing last 20 of {total} attempts)_")
    
    final_text.append("\n✅ Test complete.")
    
    try:
        await status_msg.edit_text("\n".join(final_text), parse_mode="Markdown")
    except Exception:
        await app.bot.send_message(chat_id=user_id, text="\n".join(final_text), parse_mode="Markdown")


async def _single_proxy_attempt(api_client, proxy_url: str, attempt_num: int) -> tuple[bool, str]:
    """Perform a single proxied request."""
    import time
    try:
        client = api_client.create_session(proxy_url=proxy_url)
        try:
            path = f"/api/v1/public/wilaya-quotas?_t={int(time.time() * 1000)}"
            resp = await client.get(path)
            resp.raise_for_status()
            return True, f"Attempt {attempt_num}: ✅ Success (HTTP {resp.status_code})"
        finally:
            await client.aclose()
    except Exception as e:
        logger.error("Proxy test attempt %d failed: %s", attempt_num, e)
        return False, f"Attempt {attempt_num}: ❌ Failed ({type(e).__name__})"


# ---------------------------------------------------------------------------
# Check Profile Feature
# ---------------------------------------------------------------------------

async def on_admin_check_profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the check profile by ID flow."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="admin:broadcast_cancel")]]
    )
    await query.edit_message_text(
        "🆔 *Check Profile by ID*\n\n"
        "Please enter the profile ID number you want to inspect:",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAIT_PROFILE_ID_CHECK

async def on_admin_check_profile_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the profile ID and display its information."""
    if not is_admin(update):
        return ConversationHandler.END

    msg = update.message
    if not msg or not msg.text:
        return AWAIT_PROFILE_ID_CHECK

    try:
        profile_id = int(msg.text.strip())
    except ValueError:
        await msg.reply_text("❌ *Invalid Input*\n\nPlease enter a valid numerical ID.", parse_mode="Markdown")
        return AWAIT_PROFILE_ID_CHECK

    db_path: str = context.application.bot_data.get("db_path", "")
    profile = await profile_db.get_profile_by_id_admin(db_path, profile_id)

    if not profile:
        await msg.reply_text(f"❌ No profile found with ID `{profile_id}`.", parse_mode="Markdown")
        return ConversationHandler.END

    status_emoji = {
        "pending": "⏳",
        "registered": "✅",
        "pre-registered": "📝",
        "failed": "❌",
        "registering": "🔄"
    }.get(profile.status, "ℹ️")

    from .registration import validate_profile_compliance
    err_fields = validate_profile_compliance(profile)
    valid_str = "✅ Yes" if not err_fields else f"❌ No (Errors: {', '.join(err_fields)})"

    text = (
        f"🆔 *Profile Information (ID: {profile.id})*\n\n"
        f"👤 *Owner User ID:* `{profile.user_id}`\n"
        f"📝 *Name:* `{profile.name}`\n"
        f"💳 *NIN:* `{profile.nin}`\n"
        f"🪪 *CNIBE:* `{profile.cnibe}`\n"
        f"📱 *Phone:* `{profile.phone}`\n"
        f"📧 *Email:* `{profile.email or 'N/A'}`\n"
        f"🔑 *Password:* `{profile.password}`\n"
        f"🏙️ *Wilaya:* `{profile.wilaya_name} ({profile.wilaya_id})`\n"
        f"🏘️ *Commune:* `{profile.commune_name} ({profile.commune_code})`\n"
        f"💵 *Payment:* `{profile.payment_method}`\n"
        f"{status_emoji} *Status:* `{profile.status}`\n"
        f"🛡️ *Valid Check:* `{valid_str}`\n"
        f"📅 *Created:* `{profile.created_at}`\n"
    )

    await msg.reply_text(text, parse_mode="Markdown")
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# Broadcast Feature
# ---------------------------------------------------------------------------

async def on_admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the broadcast flow."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="admin:broadcast_cancel")]]
    )
    await query.edit_message_text(
        "📢 *Message All Users*\n\n"
        "Please send the message you want to broadcast to all registered users.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAIT_BROADCAST_MESSAGE


async def on_admin_broadcast_message_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive the broadcast message and ask for confirmation."""
    if not is_admin(update):
        return ConversationHandler.END

    msg = update.message
    if not msg or not msg.text:
        return AWAIT_BROADCAST_MESSAGE

    context.user_data["broadcast_text"] = msg.text

    # Count how many users we will send it to
    db_path: str = context.application.bot_data.get("db_path", "")
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=3000;")
            async with db.execute("SELECT COUNT(*) FROM subscriptions") as cur:
                count = (await cur.fetchone())[0]
    except Exception:
        count = "unknown"

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Send", callback_data="admin:broadcast_confirm_yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="admin:broadcast_cancel")
        ]
    ])

    await msg.reply_text(
        f"📢 *Preview Broadcast*\n\n"
        f"_{msg.text}_\n\n"
        f"Are you sure you want to send this to *{count}* users?",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )
    return AWAIT_BROADCAST_CONFIRM


async def on_admin_broadcast_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Execute or cancel the broadcast."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return ConversationHandler.END

    if query.data == "admin:broadcast_cancel":
        await query.edit_message_text("🚫 Action cancelled.")
        return ConversationHandler.END

    if query.data == "admin:broadcast_confirm_yes":
        broadcast_text = context.user_data.get("broadcast_text", "")
        db_path: str = context.application.bot_data.get("db_path", "")
        
        await query.edit_message_text("⏳ Broadcasting message (with auto-translation)...")
        
        # Pre-translate to support languages
        try:
            # We translate to Arabic and French (fallback to original on error)
            # Run translation in a separate thread so we don't block the async loop
            import asyncio
            loop = asyncio.get_running_loop()
            
            def translate_all(text):
                return {
                    "ar": GoogleTranslator(source='auto', target='ar').translate(text),
                    "fr": GoogleTranslator(source='auto', target='fr').translate(text),
                    "en": GoogleTranslator(source='auto', target='en').translate(text),
                }
            
            translated_msgs = await loop.run_in_executor(None, translate_all, broadcast_text)
        except Exception as e:
            logger.error("Failed to pre-translate broadcast message: %s", e)
            # Fallback
            translated_msgs = {"ar": broadcast_text, "fr": broadcast_text, "en": broadcast_text}
            
        success = 0
        failed = 0
        try:
            async with aiosqlite.connect(db_path) as db:
                await db.execute("PRAGMA busy_timeout=3000;")
                async with db.execute("SELECT user_id FROM subscriptions") as cur:
                    rows = await cur.fetchall()
            
            for (user_id,) in rows:
                user_lang = await db_mod.get_user_language(db_path, user_id)
                msg_text = translated_msgs.get(user_lang, translated_msgs["ar"])
                
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"📢 *Announcement*\n\n{msg_text}",
                        parse_mode="Markdown"
                    )
                    success += 1
                except Forbidden:
                    logger.warning("Bot was blocked by user_id=%s. Deleting user data.", user_id)
                    await db_mod.delete_user_data(db_path, user_id)
                    failed += 1
                except Exception as e:
                    logger.warning("Failed to send broadcast to %s: %s", user_id, e)
                    failed += 1
        except Exception:
            logger.exception("Database error during broadcast")

        await query.edit_message_text(
            f"✅ *Broadcast Complete*\n\n"
            f"Sent to: {success}\n"
            f"Failed: {failed}",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    return ConversationHandler.END


def build_admin_broadcast_handler() -> ConversationHandler:
    """Build the conversation handler for admin broadcasting."""
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(on_admin_broadcast_start, pattern=r"^admin:broadcast_start$"),
            CallbackQueryHandler(on_admin_test_proxy, pattern=r"^admin:test_proxy$"),
            CallbackQueryHandler(on_admin_set_concurrency, pattern=r"^admin:set_concurrency$"),
            CallbackQueryHandler(on_admin_toggle_proxy, pattern=r"^admin:toggle_proxy:wilaya$"),
            CallbackQueryHandler(on_admin_check_profile_start, pattern=r"^admin:check_profile_start$")
        ],
        states={
            AWAIT_BROADCAST_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_broadcast_message_received),
                CallbackQueryHandler(on_admin_broadcast_confirm, pattern=r"^admin:broadcast_cancel$")
            ],
            AWAIT_BROADCAST_CONFIRM: [
                CallbackQueryHandler(on_admin_broadcast_confirm, pattern=r"^admin:broadcast_confirm_yes|admin:broadcast_cancel$")
            ],
            AWAIT_PROXY_TEST_CONFIG: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_proxy_test_config_received),
                CallbackQueryHandler(on_admin_broadcast_confirm, pattern=r"^admin:broadcast_cancel$")
            ],
            AWAIT_CONCURRENCY_LIMIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_concurrency_received),
                CallbackQueryHandler(on_admin_broadcast_confirm, pattern=r"^admin:broadcast_cancel$")
            ],
            AWAIT_WILAYA_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_wilaya_interval_received),
                CallbackQueryHandler(on_admin_proxy_cancel, pattern=r"^admin:proxy_cancel$")
            ],
            AWAIT_PROFILE_ID_CHECK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_admin_check_profile_received),
                CallbackQueryHandler(on_admin_broadcast_confirm, pattern=r"^admin:broadcast_cancel$")
            ],
        },
        fallbacks=[
            CallbackQueryHandler(on_admin_broadcast_confirm, pattern=r"^admin:broadcast_cancel$")
        ],
        per_message=False,
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

        # Profiles by validity
        async with db.execute(
            "SELECT is_valid, COUNT(*) FROM profiles GROUP BY is_valid ORDER BY is_valid DESC"
        ) as cur:
            profiles_by_validity = [(int(r[0]), int(r[1])) for r in await cur.fetchall()]

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

        # Recent quota events
        async with db.execute(
            "SELECT wilaya_code, event_type, timestamp FROM quota_history ORDER BY id DESC LIMIT 10"
        ) as cur:
            recent_history = [(str(r[0]), str(r[1]), str(r[2])) for r in await cur.fetchall()]

    return {
        "total_subscriptions": total_subs,
        "subs_today": subs_today,
        "subs_week": subs_week,
        "total_profiles": total_profiles,
        "profiles_by_status": profiles_by_status,
        "profiles_by_validity": profiles_by_validity,
        "profiles_today": profiles_today,
        "profiles_week": profiles_week,
        "recent_history": recent_history,
    }


# ---------------------------------------------------------------------------
# Inbox Handlers
# ---------------------------------------------------------------------------

async def on_admin_inbox(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the paginated and filterable admin inbox."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return

    # Callback data format: admin:inbox:<offset>[:level][:status][:date]
    parts = query.data.split(":")
    offset = int(parts[2]) if len(parts) > 2 else 0
    level_filter = parts[3] if len(parts) > 3 and parts[3] != "all" else None
    status_filter = parts[4] if len(parts) > 4 and parts[4] != "all" else None
    date_filter = parts[5] if len(parts) > 5 and parts[5] != "all" else None

    db_path = context.application.bot_data.get("db_path", "")
    limit = 5
    
    entries = await db_mod.get_inbox_entries(
        db_path, level=level_filter, status=status_filter, date_filter=date_filter, offset=offset, limit=limit
    )
    total = await db_mod.count_inbox_entries(db_path, level=level_filter, status=status_filter, date_filter=date_filter)

    text = "📥 *Admin Inbox*\n"
    if level_filter or status_filter or date_filter:
        filters_str = []
        if level_filter: filters_str.append(f"Level: `{level_filter}`")
        if status_filter: filters_str.append(f"Status: `{status_filter}`")
        if date_filter: filters_str.append(f"Date: `{date_filter}`")
        text += f"Filters: {' | '.join(filters_str)}\n"
    
    text += f"Showing {offset + 1}-{min(offset + limit, total)} of {total} entries\n\n"

    buttons = []
    if not entries:
        text += "_No entries found._"
    else:
        for entry in entries:
            # Severity emoji
            emoji = "🔴" if entry["level"] == "ERROR" else "⚠️"
            # Status tag
            status_tag = "✅" if entry["status"] == "resolved" else "🆕"
            
            # Message preview
            msg = entry["message"]
            preview = (msg[:40] + "...") if len(msg) > 40 else msg
            
            label = f"{status_tag} {emoji} {preview}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"admin:inbox_view:{entry['id']}")])

    # Pagination buttons
    nav_buttons = []
    cb_suffix = f":{level_filter or 'all'}:{status_filter or 'all'}:{date_filter or 'all'}"
    if offset > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin:inbox:{max(0, offset - limit)}{cb_suffix}"))
    if offset + limit < total:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin:inbox:{offset + limit}{cb_suffix}"))
    if nav_buttons:
        buttons.append(nav_buttons)

    # Filter buttons
    filter_row_1 = [
        InlineKeyboardButton("Level: " + (level_filter or "All"), callback_data=f"admin:inbox_filter_level:{offset}"),
        InlineKeyboardButton("Status: " + (status_filter or "All"), callback_data=f"admin:inbox_filter_status:{offset}"),
    ]
    filter_row_2 = [
        InlineKeyboardButton("Date: " + (date_filter or "All"), callback_data=f"admin:inbox_filter_date:{offset}"),
    ]
    buttons.append(filter_row_1)
    buttons.append(filter_row_2)

    buttons.append([InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="admin:back")])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def on_admin_inbox_filter_level(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show options to filter by level."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    
    offset = query.data.split(":")[2]
    
    buttons = [
        [InlineKeyboardButton("All Levels", callback_data=f"admin:inbox:{offset}:all:all:all")],
        [InlineKeyboardButton("🔴 ERROR Only", callback_data=f"admin:inbox:{offset}:ERROR:all:all")],
        [InlineKeyboardButton("⚠️ WARNING Only", callback_data=f"admin:inbox:{offset}:WARNING:all:all")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"admin:inbox:{offset}")]
    ]
    await query.edit_message_text("Filter by Level:", reply_markup=InlineKeyboardMarkup(buttons))


async def on_admin_inbox_filter_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show options to filter by status."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    
    offset = query.data.split(":")[2]
    
    buttons = [
        [InlineKeyboardButton("All Statuses", callback_data=f"admin:inbox:{offset}:all:all:all")],
        [InlineKeyboardButton("🆕 Unresolved Only", callback_data=f"admin:inbox:{offset}:all:unresolved:all")],
        [InlineKeyboardButton("✅ Resolved Only", callback_data=f"admin:inbox:{offset}:all:resolved:all")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"admin:inbox:{offset}")]
    ]
    await query.edit_message_text("Filter by Status:", reply_markup=InlineKeyboardMarkup(buttons))


async def on_admin_inbox_filter_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show options to filter by date."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    
    offset = query.data.split(":")[2]
    
    buttons = [
        [InlineKeyboardButton("All Time", callback_data=f"admin:inbox:{offset}:all:all:all")],
        [InlineKeyboardButton("📅 Today", callback_data=f"admin:inbox:{offset}:all:all:today")],
        [InlineKeyboardButton("📅 Last 7 Days", callback_data=f"admin:inbox:{offset}:all:all:week")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"admin:inbox:{offset}")]
    ]
    await query.edit_message_text("Filter by Date:", reply_markup=InlineKeyboardMarkup(buttons))


async def on_admin_inbox_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View full details of an inbox entry."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    entry_id = int(query.data.split(":")[2])
    db_path = context.application.bot_data.get("db_path", "")
    
    entry = await db_mod.get_inbox_entry(db_path, entry_id)
    if not entry:
        await query.edit_message_text("❌ Entry not found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin:inbox:0")]]))
        return

    emoji = "🔴 ERROR" if entry["level"] == "ERROR" else "⚠️ WARNING"
    status_tag = "✅ RESOLVED" if entry["status"] == "resolved" else "🆕 UNRESOLVED"
    
    text = (
        f"{emoji} ({status_tag})\n"
        f"📅 *Timestamp:* `{entry['created_at']}`\n"
        f"💬 *Message:*\n`{entry['message']}`\n\n"
    )
    
    if entry["resolved_at"]:
        text += f"✅ *Resolved at:* `{entry['resolved_at']}`\n\n"

    if entry["stack_trace"]:
        # Only show part of stack trace if it's too long
        trace = entry["stack_trace"]
        if len(trace) > 1000:
            trace = trace[:1000] + "\n... (truncated)"
        text += f"🔍 *Stack Trace:*\n```python\n{trace}\n```"

    buttons = []
    if entry["status"] == "unresolved":
        buttons.append([InlineKeyboardButton("✅ Mark as Resolved", callback_data=f"admin:inbox_resolve:{entry_id}")])
    
    buttons.append([InlineKeyboardButton("⬅️ Back to Inbox", callback_data="admin:inbox:0")])

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def on_admin_inbox_resolve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark an entry as resolved and return to view."""
    query = update.callback_query
    if not query:
        return
    
    entry_id = int(query.data.split(":")[2])
    db_path = context.application.bot_data.get("db_path", "")
    
    await db_mod.resolve_inbox_entry(db_path, entry_id)
    await query.answer("✅ Entry marked as resolved.")
    
    # Refresh view
    await on_admin_inbox_view(update, context)


async def on_admin_force_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scan all profiles for invalid data, fix emails, and notify users."""
    from .registration import validate_profile_compliance, validate_email_format
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return

    db_path: str = context.application.bot_data.get("db_path", "")
    is_silent = query.data.endswith(":silent")
    mode_txt = " (SILENT)" if is_silent else ""
    
    logger.info("Admin %s initiated force check scan%s", update.effective_user.id, mode_txt)
    await query.edit_message_text(f"⏳ Scanning all profiles in database{mode_txt.lower()}...")

    checked_count = 0
    fixed_emails = 0
    invalid_others = 0
    notifications_sent = 0

    try:
        user_profiles = await profile_db.get_all_profiles_grouped_by_user(db_path)
        logger.info("Found %d users with profiles to check", len(user_profiles))
        
        # We'll group notifications by user to avoid spamming
        for user_id, profiles in user_profiles.items():
            user_fixed_count = 0
            user_invalid_fields = [] # [(profile_name, field)]
            
            for p in profiles:
                checked_count += 1
                
                # Unified validation logic
                other_errors = validate_profile_compliance(p)
                conforms = not other_errors
                new_is_valid = 1 if conforms else 0
                
                # Update is_valid in DB if it changed
                if p.is_valid != new_is_valid:
                    try:
                        await profile_db.update_profile_field(db_path, p.id, user_id, "is_valid", new_is_valid)
                    except Exception:
                        logger.exception("Failed to update is_valid for profile %s", p.id)

                if not conforms:
                    invalid_others += 1
                    for field in other_errors:
                        user_invalid_fields.append((p.name or f"#{p.id}", field))
                    
                    # Special handling: if email is the ONLY error, we can fix it automatically
                    # but since we want users to fix their data, we'll just flag it for now.
                    # Previous logic cleared invalid emails:
                    if "Email" in other_errors and len(other_errors) == 1:
                         try:
                             await profile_db.update_profile_field(db_path, p.id, user_id, "email", "")
                             fixed_emails += 1
                             user_fixed_count += 1
                         except Exception:
                             logger.exception("Failed to fix email for profile %s", p.id)

            # Notify user if we fixed something or found errors
            if not is_silent and (user_fixed_count > 0 or user_invalid_fields):
                msg_parts = ["⚠️ *Profile Maintenance Notification*\n"]
                
                if user_fixed_count > 0:
                    msg_parts.append(
                        f"Our routine check detected an **invalid email format** in {user_fixed_count} profile(s). "
                        "We have automatically cleared those emails to ensure your profiles remain compatible.\n"
                    )
                
                if user_invalid_fields:
                    msg_parts.append(
                        "We also found **major errors** in the following profiles. These profiles **have been excluded** from "
                        "auto-registration batches until they are corrected:\n"
                    )
                    for prof_name, field in user_invalid_fields:
                        msg_parts.append(f"  • Profile *{prof_name}*: Invalid **{field}**")
                    
                    msg_parts.append(
                        "\n⚠️ *Action Required:* Please use /profiles to edit and fix them immediately to re-enable them for the next quota window."
                    )

                try:
                    await context.bot.send_message(chat_id=user_id, text="\n".join(msg_parts), parse_mode="Markdown")
                    notifications_sent += 1
                    await asyncio.sleep(0.05) 
                except Exception as e:
                    logger.warning("Could not notify user %s: %s", user_id, e)

        mode_txt = " (SILENT)" if is_silent else ""
        summary_text = (
            f"✅ *Database Check Complete{mode_txt}*\n\n"
            f"Profiles checked: `{checked_count}`\n"
            f"Invalid emails fixed: `{fixed_emails}`\n"
            f"Other invalid fields detected: `{invalid_others}` (NIN/CNIBE/PW)\n"
            f"User notifications sent: `{notifications_sent}`"
        )
        
        logger.info("Force check complete: %d checked, %d fixed, %d invalid, %d notified", 
                    checked_count, fixed_emails, invalid_others, notifications_sent)

        # Try to edit the original message, but if it fails (e.g. timeout), send a new one
        try:
            await query.edit_message_text(summary_text, reply_markup=_admin_keyboard(context), parse_mode="Markdown")
        except Exception:
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text=summary_text, 
                reply_markup=_admin_keyboard(context), 
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.exception("Force check failed")
        await context.bot.send_message(
            chat_id=update.effective_chat.id, 
            text=f"❌ *Force Check Failed*\n\nError: `{e}`", 
            parse_mode="Markdown"
        )


async def on_admin_purge_blockers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger to scan all users and delete those who blocked the bot."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_admin(update):
        await query.edit_message_text("⛔ Access denied.")
        return

    db_path = context.application.bot_data.get("db_path")
    if not db_path:
        await query.edit_message_text("❌ Database path not configured.")
        return

    # Run the purge in a background task to avoid blocking the UI
    asyncio.create_task(_run_purge_task(context.application, update.effective_user.id, db_path))
    
    await query.edit_message_text(
        "🧹 *Purge Started*\n\n"
        "I'm scanning all users to detect and remove blockers. "
        "This may take a while depending on the user count.\n\n"
        "You will receive a summary report once finished.",
        parse_mode="Markdown"
    )


async def _run_purge_task(app, admin_id: int, db_path: str) -> None:
    """Background task to iterate over users and purge blockers."""
    logger.info("Starting manual purge of blocking users...")
    
    try:
        user_ids = await db_mod.get_all_user_ids(db_path)
    except Exception as e:
        logger.exception("Failed to get user IDs for purge")
        await app.bot.send_message(chat_id=admin_id, text=f"❌ *Purge Failed*\nCould not retrieve user list: `{e}`", parse_mode="Markdown")
        return

    total = len(user_ids)
    purged = 0
    active = 0
    
    # Process users one by one
    for i, user_id in enumerate(user_ids):
        # Admin is always active
        if user_id == admin_id:
            active += 1
            continue

        is_blocked = False
        try:
            # We use send_chat_action as an invisible check.
            # It returns Forbidden if the bot is blocked.
            await app.bot.send_chat_action(chat_id=user_id, action=constants.ChatAction.TYPING)
            active += 1
        except Forbidden:
            is_blocked = True
        except Exception as e:
            msg = str(e).lower()
            # If the chat is gone or the user is invalid, we treat them as blockers to clean up
            if any(err in msg for err in ["chat not found", "user not found", "peer_id_invalid", "bot was blocked"]):
                is_blocked = True
            else:
                # Network error or temporary issue, skip this user
                active += 1
                continue

        if is_blocked:
            try:
                # Reuse existing deletion logic
                await db_mod.delete_user_data(db_path, user_id)
                purged += 1
            except Exception:
                logger.info("Failed to delete data for user %s during purge", user_id)

        # Periodic log update
        if (i + 1) % 50 == 0:
            logger.info("Purge progress: %d/%d checked...", i + 1, total)
        
        # Small delay to respect Telegram flood limits (approx 20-30 messages per second)
        await asyncio.sleep(0.05)

    report = (
        "🧹 *Purge Complete*\n\n"
        f"Total users checked: *{total}*\n"
        f"Blocked and purged: *{purged}*\n"
        f"Active users kept: *{active}*"
    )
    
    logger.info("Purge complete: %d checked, %d purged, %d kept", total, purged, active)
    
    try:
        await app.bot.send_message(chat_id=admin_id, text=report, parse_mode="Markdown")
    except Exception:
        logger.exception("Failed to send purge report to admin")
