from __future__ import annotations

import asyncio
import logging
import traceback
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram import Bot
    from telegram.ext import Application

from . import db as db_mod

class AdminInboxHandler(logging.Handler):
    """
    Custom logging handler that intercepts ERROR and WARNING events,
    persists them to the database, and notifies the admin via Telegram.
    """
    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path
        self.bot: Bot | None = None
        self.admin_id: int | None = None
        self.application: Application | None = None

    def set_bot_details(self, bot: Bot, admin_id: int, application: Application) -> None:
        """Update the handler with bot instance and admin ID for notifications."""
        self.bot = bot
        self.admin_id = admin_id
        self.application = application

    def emit(self, record: logging.LogRecord) -> None:
        # Avoid intercepting logs from the logging system itself or from this module
        if record.name.startswith("bot.logging_handler"):
            return
        
        # Only handle WARNING and above
        if record.levelno < logging.WARNING:
            return

        # Format message and capture stack trace
        message = record.getMessage()
        level = record.levelname
        stack_trace = None
        if record.exc_info:
            stack_trace = "".join(traceback.format_exception(*record.exc_info))
        elif record.stack_info:
            stack_trace = record.stack_info

        # Use the current event loop to schedule the async task
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                loop.create_task(self._async_handle(level, message, stack_trace))
        except RuntimeError:
            # No event loop is running (e.g., during early startup)
            pass

    async def _async_handle(self, level: str, message: str, stack_trace: str | None) -> None:
        """Persist the event and send a notification without blocking."""
        try:
            # 1. Persist to DB
            await db_mod.add_inbox_entry(self.db_path, level, message, stack_trace)

            # 2. Notify Admin via Telegram
            if self.bot and self.admin_id:
                # Check if real-time notifications are enabled
                realtime_enabled = True
                if self.application:
                    realtime_enabled = self.application.bot_data.get("inbox_realtime_enabled", True)
                
                if not realtime_enabled:
                    return

                emoji = "🔴 ERROR" if level == "ERROR" else "⚠️ WARNING"
                
                # Truncate message for the brief notification
                preview = (message[:200] + "...") if len(message) > 200 else message
                
                text = f"{emoji}\n\n{preview}"
                
                try:
                    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                    keyboard = InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔇 Mute Real-time", callback_data="admin:inbox_mute_confirm")]
                    ])
                    await self.bot.send_message(
                        chat_id=self.admin_id,
                        text=text,
                        reply_markup=keyboard,
                    )
                except Exception:
                    # Ignore notification errors to avoid side effects
                    pass
        except Exception:
            # Fail silently to ensure the logging system itself doesn't crash the app
            pass
