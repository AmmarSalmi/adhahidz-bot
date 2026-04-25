from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from telegram import BotCommand
from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler

from .api_client import QuotaApiClient
from .auto_registration import build_auto_registration_handler
from .db import init_db
from .handlers import BOT_COMMANDS, change, help_command, on_wilaya_selected, start, status, stop
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

    await init_db(db_path)

    api = QuotaApiClient(base_url=base_url, api_key=api_key, timeout_s=timeout_s)
    app.bot_data["db_path"] = db_path
    app.bot_data["api_client"] = api

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
    app.add_handler(build_registration_handler())
    app.add_handler(build_addprofile_handler())
    app.add_handler(build_auto_registration_handler())
    app.add_handler(build_editprofile_handler())
    app.add_handler(build_reorder_handler())

    # Simple command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("change", change))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("profiles", list_profiles))
    app.add_handler(CommandHandler("viewprofile", viewprofile))
    app.add_handler(CommandHandler("deleteprofile", deleteprofile))
    app.add_handler(CommandHandler("editprofile", editprofile))

    # Callback query handlers
    app.add_handler(CallbackQueryHandler(on_wilaya_selected, pattern=r"^wilaya:"))
    app.add_handler(CallbackQueryHandler(on_delete_profile, pattern=r"^del_prof:"))
    app.add_handler(CallbackQueryHandler(on_edit_profile_select, pattern=r"^edit_prof:"))
    app.add_handler(CallbackQueryHandler(on_view_profile, pattern=r"^view_prof:"))

    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
