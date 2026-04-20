from __future__ import annotations

import asyncio
import logging

from telegram.error import RetryAfter, TelegramError

logger = logging.getLogger(__name__)


async def notify_users(bot, user_ids: list[int], message: str) -> None:
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
            except TelegramError:
                logger.exception("Failed sending message to user_id=%s", user_id)
                break
            except Exception:
                logger.exception("Unexpected error notifying user_id=%s", user_id)
                break

            await asyncio.sleep(delay_s)
            delay_s = min(delay_s * 2, 30.0)
