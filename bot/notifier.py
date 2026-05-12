from __future__ import annotations

import asyncio
import logging

from telegram import CallbackQuery
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError, TimedOut

from . import db as db_mod
from .i18n import t
from .db import get_user_language

logger = logging.getLogger(__name__)


async def notify_users(bot, user_ids: list[int], message: str, db_path: str | None = None, format_kwargs: dict = None) -> None:
    for user_id in user_ids:
        delay_s = 1.0
        lang = "en"
        if db_path:
            lang = await get_user_language(db_path, user_id)
        
        localized_message = t(lang, message)
        if format_kwargs:
            localized_message = localized_message.format(**format_kwargs)
            
        for attempt in range(5):
            try:
                await bot.send_message(chat_id=user_id, text=localized_message)
                break
            except RetryAfter as e:
                wait_s = float(getattr(e, "retry_after", 1.0))
                logger.warning("Telegram rate-limited; sleeping %.2fs", wait_s)
                await asyncio.sleep(wait_s)
            except TimedOut:
                logger.warning("Telegram API request timed out for user_id=%s; retrying (attempt %d/5)", user_id, attempt + 1)
                await asyncio.sleep(1.0)
                continue
            except Forbidden:
                logger.info("Bot was blocked by user_id=%s. Deleting user data.", user_id)
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
        logger.info("Bot was blocked by user_id=%s. Deleting user data.", user_id)
        if db_path:
            await db_mod.delete_user_data(db_path, user_id)
        return False
    except TimedOut:
        logger.warning("Telegram API request timed out sending message to user_id=%s", user_id)
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
        logger.info("Bot was blocked by user_id=%s. Deleting user data.", user_id)
        if db_path:
            await db_mod.delete_user_data(db_path, user_id)
        return False
    except TimedOut:
        logger.warning("Telegram API request timed out sending photo to user_id=%s", user_id)
        return False
    except TelegramError:
        logger.exception("Failed sending photo to user_id=%s", user_id)
        return False


async def safe_query_answer(query: CallbackQuery, text: str | None = None) -> bool:
    """Call query.answer() and silently discard stale/expired callback errors.

    Returns True if the answer was sent, False if the query was already expired.
    This prevents 'Query is too old' tracebacks that flood logs after a bot restart.
    """
    try:
        await query.answer(text)
        return True
    except BadRequest as e:
        if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
            logger.info("Stale callback query — ignoring: %s", e)
            return False
        raise  # Re-raise unexpected BadRequest errors
    except TimedOut:
        logger.info("Telegram API request timed out answering callback query. Proceeding anyway.")
        return True
