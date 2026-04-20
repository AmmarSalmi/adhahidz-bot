from __future__ import annotations

import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from . import db as db_mod
from .api_client import QuotaStatus

logger = logging.getLogger(__name__)

_WILAYA_REFRESH_MIN_INTERVAL_S = 30


def _wilaya_keyboard(wilayas: list[tuple[str, str]], *, columns: int = 2) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code, name in wilayas:
        row.append(InlineKeyboardButton(text=name, callback_data=f"wilaya:{code}"))
        if len(row) >= columns:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _get_wilaya_list(context: ContextTypes.DEFAULT_TYPE) -> list[tuple[str, str]]:
    return list(context.application.bot_data.get("wilayas", []))


def _lookup_wilaya_name(context: ContextTypes.DEFAULT_TYPE, wilaya_code: str) -> str:
    for code, name in _get_wilaya_list(context):
        if code == wilaya_code:
            return name
    return wilaya_code


async def _ensure_wilayas_loaded(context: ContextTypes.DEFAULT_TYPE) -> list[tuple[str, str]]:
    wilayas = _get_wilaya_list(context)
    if wilayas:
        return wilayas

    last_try = float(context.application.bot_data.get("wilaya_last_fetch_ts", 0.0) or 0.0)
    now = time.time()
    if now - last_try < _WILAYA_REFRESH_MIN_INTERVAL_S:
        return []

    context.application.bot_data["wilaya_last_fetch_ts"] = now
    api = context.application.bot_data.get("api_client")
    if not api:
        return []

    try:
        statuses = await api.fetch_wilaya_quotas()
        items = [(s.wilaya_code, s.wilaya_name) for s in statuses.values()]
        items.sort(key=lambda t: (t[0], t[1]))
        context.application.bot_data["wilayas"] = items
        return items
    except Exception:
        logger.exception("Failed to refresh wilaya list on demand")
        return []


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wilayas = await _ensure_wilayas_loaded(context)
    if not wilayas:
        await update.effective_message.reply_text(
            "Welcome! I couldn't load the Wilaya list yet (API unavailable). Send /change later to try again."
        )
        return

    await update.effective_message.reply_text(
        "Welcome! Choose your Wilaya to receive quota notifications:",
        reply_markup=_wilaya_keyboard(wilayas),
    )


async def change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wilayas = await _ensure_wilayas_loaded(context)
    if not wilayas:
        await update.effective_message.reply_text("Wilaya list not available yet. Please try again later.")
        return

    await update.effective_message.reply_text("Choose your Wilaya:", reply_markup=_wilaya_keyboard(wilayas))


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    deleted = await db_mod.delete_subscription(db_path, user_id)
    if deleted:
        await update.effective_message.reply_text("You have been unsubscribed.")
    else:
        await update.effective_message.reply_text("You are not subscribed.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    sub = await db_mod.get_subscription(db_path, user_id)
    if not sub:
        await update.effective_message.reply_text("You are not subscribed. Send /start to subscribe.")
        return

    wilaya_name = _lookup_wilaya_name(context, sub.wilaya_code)
    last_known: dict[str, QuotaStatus] = context.application.bot_data.get("last_known", {})
    st = last_known.get(sub.wilaya_code)

    if not st:
        await update.effective_message.reply_text(
            f"Subscription: {wilaya_name} ({sub.wilaya_code})\nLast known status: unknown (not checked yet)."
        )
        return

    remaining_txt = "unknown" if st.remaining is None else str(st.remaining)
    avail_txt = "available" if st.available else "not available"
    await update.effective_message.reply_text(
        f"Subscription: {wilaya_name} ({sub.wilaya_code})\nLast known status: {avail_txt}. Remaining: {remaining_txt}."
    )


async def on_wilaya_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    if not data.startswith("wilaya:"):
        return

    wilaya_code = data.split(":", 1)[1]
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    await db_mod.set_subscription(db_path, user_id, wilaya_code)

    wilaya_name = _lookup_wilaya_name(context, wilaya_code)
    await query.edit_message_text(f"You will be notified when quota is available in {wilaya_name}.")
