from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from .admin import admin_command, build_admin_broadcast_handler, on_admin_back, on_admin_stats, on_admin_toggle_restrict, on_admin_toggle_proxy, on_admin_test_proxy

from .api_client import QuotaApiClient
from .auto_registration import build_verifyotp_handler, manual_captcha_reply_handler
from .db import init_db
from .handlers import BOT_COMMANDS, change, checkprofile, fetchinfo, help_command, on_check_profile, on_wilaya_selected, start, status, stop, test_captcha_solvers
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


def _configure_logging() -> None:
    level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_str, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


async def _load_wilayas(api: QuotaApiClient) -> list[tuple[str, str]]:
    statuses = await api.fetch_wilaya_quotas()
    items = [(s.wilaya_code, s.wilaya_name) for s in statuses.values()]
    items.sort(key=lambda t: (t[0], t[1]))
    return items


async def _post_init(app: Application) -> None:
    logger = logging.getLogger(__name__)

    base_url = os.getenv("QUOTA_API_BASE_URL", "https://adhahi.dz")
    api_key = os.getenv("QUOTA_API_KEY") or None
    interval_s = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
    confirm_fetches = int(os.getenv("CONFIRM_FETCHES", "2"))
    confirm_delay_s = float(os.getenv("CONFIRM_DELAY_SECONDS", "1"))
    db_path = os.getenv("DATABASE_PATH", "/data/subscriptions.db")
    timeout_s = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))
    max_concurrent = int(os.getenv("MAX_CONCURRENT_SESSIONS", "50"))

    await init_db(db_path)

    api = QuotaApiClient(base_url=base_url, api_key=api_key, timeout_s=timeout_s)
    app.bot_data["db_path"] = db_path
    app.bot_data["api_client"] = api
    app.bot_data["max_concurrent_sessions"] = max_concurrent
    
    # Global semaphore for auto-registration connections
    import asyncio
    app.bot_data["concurrency_semaphore"] = asyncio.Semaphore(max_concurrent)

    try:
        app.bot_data["wilayas"] = await _load_wilayas(api)
    except Exception:
        logger.exception("Failed to load wilaya list from API at startup")
        app.bot_data["wilayas"] = []

    scheduler = start_scheduler(
        app=app,
        db_path=db_path,
        api_client=api,
        interval_s=interval_s,
        confirm_fetches=confirm_fetches,
        confirm_delay_s=confirm_delay_s,
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

    logger.info(
        "Bot started. Interval=%ss ConfirmFetches=%s ConfirmDelay=%ss BaseURL=%s DB=%s",
        interval_s,
        confirm_fetches,
        confirm_delay_s,
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


def main() -> None:
    load_dotenv()
    _configure_logging()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

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
    app.add_handler(CallbackQueryHandler(on_admin_stats, pattern=r"^admin:stats$"))
    app.add_handler(CallbackQueryHandler(on_admin_back, pattern=r"^admin:back$"))
    app.add_handler(CallbackQueryHandler(on_admin_toggle_restrict, pattern=r"^admin:toggle_restrict$"))
    app.add_handler(CallbackQueryHandler(on_admin_toggle_proxy, pattern=r"^admin:toggle_proxy$"))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(on_wilaya_selected, pattern=r"^wilaya:"))
    app.add_handler(CallbackQueryHandler(on_check_profile, pattern=r"^chk_prof:"))
    app.add_handler(CallbackQueryHandler(on_delete_profile, pattern=r"^del_prof:"))
    app.add_handler(CallbackQueryHandler(on_edit_profile_select, pattern=r"^edit_prof:"))
    app.add_handler(CallbackQueryHandler(on_view_profile, pattern=r"^view_prof:"))
    app.add_handler(CallbackQueryHandler(on_menu_nav, pattern=r"^menu:nav:"))
    app.add_handler(CallbackQueryHandler(on_menu_cmd, pattern=r"^menu:cmd:"))

    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
