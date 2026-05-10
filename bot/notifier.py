from __future__ import annotations

import asyncio
import logging

from telegram.error import Forbidden, RetryAfter, TelegramError

from . import db as db_mod

logger = logging.getLogger(__name__)


async def notify_users(bot, user_ids: list[int], message: str, db_path: str | None = None) -> None:
    for user_id in user_ids:
        delay_s = 1.0
        for attempt in range(5):
            try:
                await bot.send_message(chat_id=user_id, text=message)
                break
            except RetryAfter as e:
                wait_s = float(getattr(e, "retry_after", 1.0))
                logger.warning("Telegram rate-limited; sleeping %.2fs", wait_s)
                await asyncio.sleep(wait_s)
            except Forbidden:
                logger.warning("Bot was blocked by user_id=%s. Deleting user data.", user_id)
                if db_path:
                    await db_mod.delete_user_data(db_path, user_id)
                break
            except TelegramError:
                logger.exception("Failed sending message to user_id=%s", user_id)
                break
            except Exception:
                logger.exception("Unexpected error notifying user_id=%s", user_id)
                break

            await asyncio.sleep(delay_s)
            delay_s = min(delay_s * 2, 30.0)


async def safe_send_message(bot, user_id: int, db_path: str | None = None, **kwargs) -> bool:
    """Send a message and delete user data if the bot is blocked."""
    try:
        await bot.send_message(chat_id=user_id, **kwargs)
        return True
    except Forbidden:
        logger.warning("Bot was blocked by user_id=%s. Deleting user data.", user_id)
        if db_path:
            await db_mod.delete_user_data(db_path, user_id)
        return False
    except TelegramError:
        logger.exception("Failed sending message to user_id=%s", user_id)
        return False


async def safe_send_photo(bot, user_id: int, db_path: str | None = None, **kwargs) -> bool:
    """Send a photo and delete user data if the bot is blocked."""
    try:
        await bot.send_photo(chat_id=user_id, **kwargs)
        return True
    except Forbidden:
        logger.warning("Bot was blocked by user_id=%s. Deleting user data.", user_id)
        if db_path:
            await db_mod.delete_user_data(db_path, user_id)
        return False
    except TelegramError:
        logger.exception("Failed sending photo to user_id=%s", user_id)
        return False
