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
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.error import Forbidden

from . import db as db_mod
from .proxy import get_proxy_url

logger = logging.getLogger(__name__)

# Conversation states for broadcasting
AWAIT_BROADCAST_MESSAGE = 1
AWAIT_BROADCAST_CONFIRM = 2
AWAIT_PROXY_TEST_CONFIG = 3
AWAIT_CONCURRENCY_LIMIT = 4
AWAIT_WILAYA_INTERVAL = 5

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
        lines.append(f"  • `{status_name}`: {count}")

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
            [InlineKeyboardButton("📊 User Statistics", callback_data="admin:stats")],
            [InlineKeyboardButton("📢 Message All Users", callback_data="admin:broadcast_start")],
            [InlineKeyboardButton(toggle_restrict, callback_data="admin:toggle_restrict")],
            [InlineKeyboardButton("🌐 Proxy Settings ⚙️", callback_data="admin:proxy_submenu")],
            [InlineKeyboardButton("⚙️ Set Concurrency Limit", callback_data="admin:set_concurrency")],
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
            CallbackQueryHandler(on_admin_toggle_proxy, pattern=r"^admin:toggle_proxy:wilaya$")
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
        "profiles_today": profiles_today,
        "profiles_week": profiles_week,
        "recent_history": recent_history,
    }
