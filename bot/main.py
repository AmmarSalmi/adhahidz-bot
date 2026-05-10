from __future__ import annotations

import logging
import os
import warnings

# Silence redundant ConversationHandler warnings about per_message settings
# because we use a mix of MessageHandlers and CallbackQueryHandlers.
warnings.filterwarnings("ignore", message=r".*per_message.*")

from dotenv import load_dotenv
from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, ChatMemberHandler, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut
from telegram.request import HTTPXRequest

from .admin import (
    admin_command, build_admin_broadcast_handler, on_admin_back, on_admin_stats, 
    on_admin_toggle_restrict, on_admin_toggle_proxy, on_admin_test_proxy, 
    on_admin_proxy_submenu, on_admin_inbox, on_admin_inbox_view, 
    on_admin_inbox_resolve, on_admin_inbox_filter_level, on_admin_inbox_filter_status,
    on_admin_inbox_filter_date, on_admin_force_check, on_admin_purge_blockers,
    on_admin_notify_invalid_nins, on_admin_sync_orders
)

from .api_client import QuotaApiClient
from .auto_registration import build_verifyotp_handler, manual_captcha_reply_handler
from .db import init_db
from .handlers import BOT_COMMANDS, change, checkprofile, fetchinfo, help_command, on_check_profile, on_my_chat_member_update, on_wilaya_selected, start, status, stop, test_captcha_solvers
from .profile_handlers import (
    build_addprofile_handler,
    build_editprofile_handler,
    build_reorder_handler,
    deleteprofile,
    editprofile,
    list_profiles,
    on_delete_profile,
    on_edit_profile_select,
    on_view_profile,
    viewprofile,
)
from .registration import build_registration_handler
from .scheduler import start_scheduler
from .menu import menu_command, on_menu_nav, on_menu_cmd, handle_reply_menu
from .logging_handler import AdminInboxHandler

# Global reference to the inbox handler to update it later
_inbox_handler: AdminInboxHandler | None = None


def _configure_logging() -> None:
    level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    
    # Suppress verbose HTTP logs from libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    # Also suppress apscheduler if it's too noisy
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    # Register Admin Inbox Handler
    global _inbox_handler
    db_path = os.getenv("DATABASE_PATH", "/data/subscriptions.db")
    _inbox_handler = AdminInboxHandler(db_path)
    _inbox_handler.setLevel(logging.WARNING)
    logging.getLogger().addHandler(_inbox_handler)


async def _load_wilayas(api: QuotaApiClient, db_path: str | None = None) -> list[tuple[str, str]]:
    from . import db as db_mod
    if db_path:
        cached = await db_mod.get_cached_wilayas(db_path)
        if cached:
            return cached

    statuses = await api.fetch_wilaya_quotas()
    items = [(s.wilaya_code, s.wilaya_name) for s in statuses.values()]
    items.sort(key=lambda t: (t[0], t[1]))
    
    if db_path and items:
        wilaya_dicts = [{"code": code, "name": name} for code, name in items]
        try:
            await db_mod.save_wilayas(db_path, wilaya_dicts)
        except Exception:
            logging.getLogger(__name__).exception("Failed to save wilayas to cache")

    return items


async def _post_init(app: Application) -> None:
    logger = logging.getLogger(__name__)

    base_url = os.getenv("QUOTA_API_BASE_URL", "https://adhahi.dz")
    api_key = os.getenv("QUOTA_API_KEY") or None
    interval_s = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
    db_path = os.getenv("DATABASE_PATH", "/data/subscriptions.db")
    timeout_s = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))
    max_concurrent = int(os.getenv("MAX_CONCURRENT_SESSIONS", "50"))

    await init_db(db_path)
    from . import profile_db
    reset_count = await profile_db.reset_registering_profiles(db_path)
    if reset_count > 0:
        logger.info("Reset %d profiles from 'registering' to 'pending' at startup.", reset_count)

    api = QuotaApiClient(base_url=base_url, api_key=api_key, timeout_s=timeout_s)
    from .admin import ADMIN_TELEGRAM_ID
    app.bot_data["db_path"] = db_path
    app.bot_data["api_client"] = api
    app.bot_data["admin_id"] = ADMIN_TELEGRAM_ID
    app.bot_data["max_concurrent_sessions"] = max_concurrent
    app.bot_data["check_interval_seconds"] = interval_s
    
    # Global semaphore for auto-registration connections
    import asyncio
    app.bot_data["concurrency_semaphore"] = asyncio.Semaphore(max_concurrent)

    # Proxy settings initialization
    app.bot_data["proxy_wilaya"] = os.getenv("PROXY_WILAYA", "false").lower() == "true"
    app.bot_data["proxy_autoreg"] = os.getenv("PROXY_AUTOREG", "false").lower() == "true"
    app.bot_data["proxy_checkprof"] = os.getenv("PROXY_CHECKPROF", "false").lower() == "true"

    try:
        app.bot_data["wilayas"] = await _load_wilayas(api, db_path)
    except Exception:
        logger.exception("Failed to load wilaya list from API at startup")
        app.bot_data["wilayas"] = []

    scheduler = start_scheduler(
        app=app,
        db_path=db_path,
        api_client=api,
        interval_s=interval_s,
    )
    app.bot_data["scheduler"] = scheduler

    notif_flag_path = os.path.join(os.path.dirname(db_path), ".excess_notified")
    if not os.path.exists(notif_flag_path):
        import asyncio
        async def notify_excess_profiles(app_ref, path, flag_path):
            from . import profile_db
            try:
                user_profiles = await profile_db.get_all_profiles_grouped_by_user(path)
                for user_id, profiles in user_profiles.items():
                    if len(profiles) > 3:
                        excess_count = len(profiles) - 3
                        try:
                            await app_ref.bot.send_message(
                                chat_id=user_id,
                                text=(
                                    f"⚠️ *Profile Limit Update*\n\n"
                                    f"To ensure fair access, we are restricting all users to a maximum of 3 profiles. "
                                    f"You currently have {len(profiles)} profiles.\n\n"
                                    f"Please delete your excess profiles manually using /profiles.\n"
                                    f"If you do not remove them, your {excess_count} lowest priority profile(s) (at the bottom of your list) "
                                    f"will be automatically removed tomorrow at 10:00 AM Algeria Time."
                                ),
                                parse_mode="Markdown",
                            )
                        except Exception:
                            logger.exception("Failed to send notice to user %s", user_id)
                with open(flag_path, "w") as f:
                    f.write("done")
            except Exception:
                logger.exception("Failed to notify excess profiles")
        
        asyncio.create_task(notify_excess_profiles(app, db_path, notif_flag_path))

    # Register the bot menu button with Telegram
    await app.bot.set_my_commands(
        [BotCommand(cmd, desc) for cmd, desc in BOT_COMMANDS]
    )

    # Update inbox handler with bot details for notifications
    if _inbox_handler and ADMIN_TELEGRAM_ID:
        _inbox_handler.set_bot_details(app.bot, ADMIN_TELEGRAM_ID)

    logger.info(
        "Bot started. Interval=%ss BaseURL=%s DB=%s",
        interval_s,
        base_url,
        db_path,
    )


async def _post_shutdown(app: Application) -> None:
    logger = logging.getLogger(__name__)

    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            logger.exception("Failed shutting down scheduler")

    api = app.bot_data.get("api_client")
    if api:
        try:
            await api.aclose()
        except Exception:
            logger.exception("Failed closing API client")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log the error and send a telegram message to notify the developer."""
    logger = logging.getLogger(__name__)

    # Ignore "Message is not modified" errors - these are harmless and usually
    # caused by double-clicks on inline buttons.
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        return

    # Handle blocked bot errors gracefully
    if isinstance(context.error, Forbidden):
        logger.warning("Bot was blocked by a user (Forbidden error)")
        return

    # Handle timeouts gracefully
    if isinstance(context.error, TimedOut):
        logger.warning("Telegram API request timed out. This usually happens during high activity or network congestion.")
        return

    # Log other errors
    logger.error("Exception while handling an update:", exc_info=context.error)


def main() -> None:
    load_dotenv()
    _configure_logging()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    # Configure Telegram request with longer timeouts to prevent TimedOut errors
    # Default is 20s for read/write, 5s for connect.
    # We'll increase them to handle periods of high API load.
    tg_request = HTTPXRequest(
        connect_timeout=15.0, 
        read_timeout=30.0, 
        write_timeout=30.0,
        pool_timeout=10.0
    )

    app = (
        ApplicationBuilder()
        .token(token)
        .request(tg_request)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    app.add_error_handler(error_handler)

    # Conversation handlers (must be added first for priority)
    app.add_handler(build_admin_broadcast_handler())
    app.add_handler(build_registration_handler())
    app.add_handler(build_addprofile_handler())
    app.add_handler(build_verifyotp_handler())
    app.add_handler(build_editprofile_handler())
    app.add_handler(build_reorder_handler())

    # Message handlers
    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT, manual_captcha_reply_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply_menu))

    # Simple command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("change", change))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("fetchinfo", fetchinfo))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("testcaptchasolvers", test_captcha_solvers))
    app.add_handler(CommandHandler("checkprofile", checkprofile))
    app.add_handler(CommandHandler("profiles", list_profiles))
    app.add_handler(CommandHandler("viewprofile", viewprofile))
    app.add_handler(CommandHandler("deleteprofile", deleteprofile))
    app.add_handler(CommandHandler("editprofile", editprofile))

    # --- Admin-only handlers (hidden, not in setMyCommands) ---
    app.add_handler(CommandHandler("adminammar", admin_command))
    app.add_handler(CallbackQueryHandler(on_admin_force_check, pattern=r"^admin:force_check(:silent)?$"))
    app.add_handler(CallbackQueryHandler(on_admin_stats, pattern=r"^admin:stats$"))
    app.add_handler(CallbackQueryHandler(on_admin_back, pattern=r"^admin:back$"))
    app.add_handler(CallbackQueryHandler(on_admin_purge_blockers, pattern=r"^admin:purge_blockers$"))
    app.add_handler(CallbackQueryHandler(on_admin_toggle_restrict, pattern=r"^admin:toggle_restrict$"))
    app.add_handler(CallbackQueryHandler(on_admin_proxy_submenu, pattern=r"^admin:proxy_submenu$"))
    app.add_handler(CallbackQueryHandler(on_admin_toggle_proxy, pattern=r"^admin:toggle_proxy:"))
    app.add_handler(CallbackQueryHandler(on_admin_inbox, pattern=r"^admin:inbox:"))
    app.add_handler(CallbackQueryHandler(on_admin_inbox_view, pattern=r"^admin:inbox_view:"))
    app.add_handler(CallbackQueryHandler(on_admin_inbox_resolve, pattern=r"^admin:inbox_resolve:"))
    app.add_handler(CallbackQueryHandler(on_admin_inbox_filter_level, pattern=r"^admin:inbox_filter_level:"))
    app.add_handler(CallbackQueryHandler(on_admin_inbox_filter_status, pattern=r"^admin:inbox_filter_status:"))
    app.add_handler(CallbackQueryHandler(on_admin_inbox_filter_date, pattern=r"^admin:inbox_filter_date:"))
    app.add_handler(CallbackQueryHandler(on_admin_notify_invalid_nins, pattern=r"^admin:notify_invalid_nins$"))
    app.add_handler(CallbackQueryHandler(on_admin_sync_orders, pattern=r"^admin:sync_orders$"))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(on_wilaya_selected, pattern=r"^wilaya:"))
    app.add_handler(CallbackQueryHandler(on_check_profile, pattern=r"^chk_prof:"))
    app.add_handler(CallbackQueryHandler(on_delete_profile, pattern=r"^del_prof:"))
    app.add_handler(CallbackQueryHandler(on_edit_profile_select, pattern=r"^edit_prof:"))
    app.add_handler(CallbackQueryHandler(on_view_profile, pattern=r"^view_prof:"))
    app.add_handler(CallbackQueryHandler(on_menu_nav, pattern=r"^menu:nav:"))
    app.add_handler(CallbackQueryHandler(on_menu_cmd, pattern=r"^menu:cmd:"))
    app.add_handler(ChatMemberHandler(on_my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    app.run_polling(allowed_updates=["message", "callback_query", "my_chat_member"])


if __name__ == "__main__":
    main()
