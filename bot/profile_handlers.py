"""Profile management commands: add, list, edit, delete, reorder."""
from __future__ import annotations

import logging
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import profile_db
from .admin import check_restricted
from .registration import _validate_password
from .i18n import t, get_lang

logger = logging.getLogger(__name__)

# ─── Add-profile conversation states ──────────────────────────────────────────
(
    AP_NAME,
    AP_NIN,
    AP_CNIBE,
    AP_PHONE,
    AP_PASSWORD,
    AP_WILAYA,
    AP_COMMUNE,
    AP_PAYMENT_METHOD,
    AP_EMAIL,
) = range(9)

# Valid payment methods and their display labels
_PAYMENT_METHODS = {
    "CASH": "💵 Cash",
    "TPE": "💳 Credit Card (TPE)",
    "EN_LIGNE": "🌐 Pay Online",
}


def _ap_state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    if "add_profile" not in context.user_data:
        context.user_data["add_profile"] = {}
    return context.user_data["add_profile"]


# ─── /addprofile ──────────────────────────────────────────────────────────────

async def addprofile_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_restricted(update, context):
        return ConversationHandler.END
        
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)
    
    profiles = await profile_db.get_profiles(db_path, user_id)
    if len(profiles) >= 3:
        await update.effective_message.reply_text(
            t(lang, "⚠️ *Profile Limit Reached*\n\nTo ensure fair access, we are restricting all users to a maximum of 3 profiles. Please delete an existing profile manually using /profiles to add a new one."),
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    context.user_data["add_profile"] = {}
    await update.effective_message.reply_text(
        t(lang, "📋 *Add Registration Profile*\n\nStep 1/9 — Enter a short *Name* for this profile (e.g. 'Dad', 'My Profile'):"),
        parse_mode="Markdown",
    )
    return AP_NAME


async def ap_collect_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    lang = await get_lang(context, update.effective_user.id)
    if not text:
        await update.message.reply_text(t(lang, "❌ Name cannot be empty. Try again:"))
        return AP_NAME
    _ap_state(context)["name"] = text
    await update.message.reply_text(
        t(lang, "✅ Name '{text}' recorded.\n\nStep 2/9 — Enter the *NIN* (18 digits):").format(text=text),
        parse_mode="Markdown",
    )
    return AP_NIN


async def ap_collect_nin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    lang = await get_lang(context, update.effective_user.id)
    if not text.isdigit() or len(text) != 18:
        await update.message.reply_text(
            t(lang, "❌ NIN must be exactly *18 digits*. Try again:"),
            parse_mode="Markdown",
        )
        return AP_NIN
    _ap_state(context)["nin"] = text
    await update.message.reply_text(
        t(lang, "✅ NIN recorded.\n\nStep 3/9 — Enter the *CNIBE* (9 digits):"),
        parse_mode="Markdown",
    )
    return AP_CNIBE


async def ap_collect_cnibe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    lang = await get_lang(context, update.effective_user.id)
    if not text.isdigit() or len(text) != 9:
        await update.message.reply_text(
            t(lang, "❌ CNIBE must be exactly *9 digits*. Try again:"),
            parse_mode="Markdown",
        )
        return AP_CNIBE
    _ap_state(context)["cnibe"] = text
    await update.message.reply_text(
        t(lang, "✅ CNIBE recorded.\n\nStep 4/9 — Enter the *phone number* (10 digits, starts with 0):"),
        parse_mode="Markdown",
    )
    return AP_PHONE


async def ap_collect_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    lang = await get_lang(context, update.effective_user.id)
    if not text.isdigit() or len(text) != 10 or not text.startswith("0"):
        await update.message.reply_text(
            t(lang, "❌ Phone must be exactly *10 digits* starting with *0*. Try again:"),
            parse_mode="Markdown",
        )
        return AP_PHONE
    _ap_state(context)["phone"] = text
    await update.message.reply_text(
        t(lang, "✅ Phone recorded.\n\nStep 5/9 — Enter a *password* for the adhahi.dz account:\n_(8-16 characters, must include upper, lower, digit, and symbol; no dots)_"),
        parse_mode="Markdown",
    )
    return AP_PASSWORD


async def ap_collect_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    lang = await get_lang(context, update.effective_user.id)
    errors = _validate_password(text)
    if errors:
        bullet_list = "\n".join(f"  • {e}" for e in errors)
        await update.message.reply_text(t(lang, "❌ Invalid password:\n{bullet_list}\n\nTry again:").format(bullet_list=bullet_list))
        return AP_PASSWORD

    _ap_state(context)["password"] = text

    wilayas: list[tuple[str, str]] = list(
        context.application.bot_data.get("wilayas", [])
    )
    if not wilayas:
        await update.message.reply_text(
            t(lang, "⚠️ Wilaya list not available. Try /addprofile later.")
        )
        return ConversationHandler.END

    await update.message.reply_text(
        t(lang, "✅ Password recorded.\n\nStep 6/9 — Select the *Wilaya*:"),
        parse_mode="Markdown",
        reply_markup=_wilaya_kb(wilayas),
    )
    return AP_WILAYA


def _wilaya_kb(wilayas: list[tuple[str, str]], *, cols: int = 2) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code, name in wilayas:
        row.append(InlineKeyboardButton(text=name, callback_data=f"ap_w:{code}"))
        if len(row) >= cols:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def ap_on_wilaya(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    lang = await get_lang(context, update.effective_user.id)
    if not query:
        return AP_WILAYA
    await query.answer()

    code = (query.data or "").split(":", 1)[1]
    state = _ap_state(context)
    state["wilaya_id"] = int(code)

    # Look up name
    wilaya_name = code
    for c, n in context.application.bot_data.get("wilayas", []):
        if c == code:
            wilaya_name = n
            break
    state["wilaya_name"] = wilaya_name

    await query.edit_message_text(
        t(lang, "✅ Wilaya *{wilaya_name}* selected.\n\n⏳ Fetching communes…").format(wilaya_name=wilaya_name),
        parse_mode="Markdown",
    )

    # Fetch communes
    try:
        from .registration import _fetch_communes
        communes = await _fetch_communes(context, int(code))
    except Exception as exc:
        logger.exception("Failed to fetch communes for wilaya %s", code)
        await query.edit_message_text(t(lang, "❌ Failed to fetch communes: {exc}").format(exc=exc))
        return ConversationHandler.END

    active = [c for c in communes if c.get("isActive")]
    if not active:
        await query.edit_message_text(t(lang, "⚠️ No active communes. Try a different wilaya with /addprofile."))
        return ConversationHandler.END

    state["_communes"] = active

    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for c in active:
        label = f"{c['name']} ({c['code']})"
        row.append(InlineKeyboardButton(text=label, callback_data=f"ap_c:{c['code']}"))
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    await query.edit_message_text(
        t(lang, "Step 7/9 — Select the *Commune*:"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AP_COMMUNE


async def ap_on_commune(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    lang = await get_lang(context, update.effective_user.id)
    if not query:
        return AP_COMMUNE
    await query.answer()

    commune_code = (query.data or "").split(":", 1)[1]
    state = _ap_state(context)
    state["commune_code"] = commune_code

    commune_name = commune_code
    for c in state.get("_communes", []):
        if c["code"] == commune_code:
            commune_name = c["name"]
            break
    state["commune_name"] = commune_name

    # Show payment method selection
    pm_rows = [
        [InlineKeyboardButton(text=label, callback_data=f"ap_pm:{code}")]
        for code, label in _PAYMENT_METHODS.items()
    ]
    await query.edit_message_text(
        t(lang, "✅ Commune *{commune_name}* selected.\n\nStep 8/9 — Select a *payment method*:").format(commune_name=commune_name),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(pm_rows),
    )
    return AP_PAYMENT_METHOD


async def ap_on_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    lang = await get_lang(context, update.effective_user.id)
    if not query:
        return AP_PAYMENT_METHOD
    await query.answer()

    method = (query.data or "").split(":", 1)[1]
    if method not in _PAYMENT_METHODS:
        await query.edit_message_text(t(lang, "❌ Invalid payment method. Try again."))
        return AP_PAYMENT_METHOD

    state = _ap_state(context)
    state["payment_method"] = method

    label = _PAYMENT_METHODS[method]
    await query.edit_message_text(
        t(lang, "✅ Payment method *{label}* selected.\n\nStep 9/9 — Enter an *email* (optional, send `-` to skip):").format(label=label),
        parse_mode="Markdown",
    )
    return AP_EMAIL


async def ap_collect_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    lang = await get_lang(context, update.effective_user.id)
    state = _ap_state(context)

    # Handle common skip keywords
    if text.lower() in ("-", "skip", "none", "aucun", "no", "لا"):
        state["email"] = ""
    elif not _validate_email(text):
        await update.message.reply_text(
            t(lang, "❌ *Invalid email format.*\n\nPlease enter a valid email address or send `-` to skip:"),
            parse_mode="Markdown"
        )
        return AP_EMAIL
    else:
        state["email"] = text

    # Save to DB
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    try:
        profile_id = await profile_db.add_profile(db_path, user_id, state)
    except Exception as exc:
        logger.exception("Failed to save profile")
        await update.message.reply_text(t(lang, "❌ Failed to save profile: {exc}").format(exc=exc))
        return ConversationHandler.END

    # Clean up temp state
    context.user_data.pop("add_profile", None)

    status = "pending"

    pm_label = _PAYMENT_METHODS.get(state.get('payment_method', 'CASH'), state.get('payment_method', 'CASH'))
    await update.message.reply_text(
        t(lang, "🎉 Profile #{profile_id} ('{name}') saved!\n\nNIN: `{nin}`\nWilaya: {wilaya}\nCommune: {commune}\nPayment: {pm_label}\nStatus: {status}\n\nIt will be auto-registered when quota opens.\nUse /profiles to view all your profiles.").format(
            profile_id=profile_id,
            name=state.get('name', ''),
            nin=f"{state['nin'][:4]}…{state['nin'][-4:]}",
            wilaya=state.get('wilaya_name', state['wilaya_id']),
            commune=state.get('commune_name', state['commune_code']),
            pm_label=pm_label,
            status=status
        ),
        parse_mode="Markdown",
    )
    await _revalidate_and_warn(update, context, profile_id)
    return ConversationHandler.END


async def ap_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("add_profile", None)
    lang = await get_lang(context, update.effective_user.id)
    await update.effective_message.reply_text(t(lang, "Profile creation cancelled."))
    return ConversationHandler.END


def build_addprofile_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("addprofile", addprofile_start),
            CallbackQueryHandler(addprofile_start, pattern=r"^menu:cmd:addprofile$"),
        ],
        states={
            AP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_collect_name)],
            AP_NIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_collect_nin)],
            AP_CNIBE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_collect_cnibe)],
            AP_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_collect_phone)],
            AP_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_collect_password)],
            AP_WILAYA: [CallbackQueryHandler(ap_on_wilaya, pattern=r"^ap_w:")],
            AP_COMMUNE: [CallbackQueryHandler(ap_on_commune, pattern=r"^ap_c:")],
            AP_PAYMENT_METHOD: [CallbackQueryHandler(ap_on_payment_method, pattern=r"^ap_pm:")],
            AP_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, ap_collect_email)],
        },
        fallbacks=[CommandHandler("cancel", ap_cancel)],
        per_user=True,
        per_chat=True,
        per_message=False,
    )


# ─── /profiles ────────────────────────────────────────────────────────────────

async def list_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_restricted(update, context):
        return
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)
    profiles = await profile_db.get_profiles(db_path, user_id)

    if not profiles:
        await update.effective_message.reply_text(
            t(lang, "No profiles found. Use /addprofile to create one.")
        )
        return

    lines = [t(lang, "📋 *Your Profiles*\n")]
    for i, p in enumerate(profiles, 1):
        status_icon = {"pending": "🟡", "pre-registered": "🔵", "registering": "🔄", "registered": "✅", "ordered": "🐑", "failed": "❌"}.get(p.status, "❓")
        masked_nin = f"{p.nin[:4]}…{p.nin[-4:]}"
        lines.append(
            f"*{i}.* `#{p.id}` **{p.name}** {status_icon} {p.status}\n"
            f"   NIN: `{masked_nin}` | Phone: `{p.phone}`\n"
            f"   {p.wilaya_name} → {p.commune_name}\n"
        )
    lines.append(t(lang, "_Use /editprofile, /deleteprofile, /reorder to manage._"))
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── /viewprofile ─────────────────────────────────────────────────────────────

async def viewprofile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_restricted(update, context):
        return
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)
    profiles = await profile_db.get_profiles(db_path, user_id)

    if not profiles:
        await update.effective_message.reply_text(t(lang, "No profiles to view."))
        return

    rows: list[list[InlineKeyboardButton]] = []
    for p in profiles:
        masked = f"{p.nin[:4]}…{p.nin[-4:]}"
        rows.append([
            InlineKeyboardButton(
                text=f"#{p.id} {p.name} ({masked}) — {p.wilaya_name}",
                callback_data=f"view_prof:{p.id}",
            )
        ])
    await update.effective_message.reply_text(
        t(lang, "Select a profile to *view full details*:"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_view_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    profile_id = int((query.data or "").split(":", 1)[1])
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    profile = await profile_db.get_profile(db_path, profile_id, user_id)
    lang = await get_lang(context, user_id)
    if not profile:
        await query.edit_message_text(t(lang, "❌ Profile not found."))
        return

    pm_label = _PAYMENT_METHODS.get(profile.payment_method, profile.payment_method)
    lines = [
        f"📋 *Profile #{profile.id} — {profile.name}*",
        f"Status: {profile.status}",
        "",
        f"*NIN:* `{profile.nin}`",
        f"*CNIBE:* `{profile.cnibe}`",
        f"*Phone:* `{profile.phone}`",
        f"*Password:* `{profile.password}`",
        f"*Email:* `{profile.email or '-'}`",
        f"*Payment:* {pm_label}",
        "",
        f"*Wilaya:* {profile.wilaya_name} ({profile.wilaya_id})",
        f"*Commune:* {profile.commune_name} ({profile.commune_code})"
    ]
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown")


# ─── /deleteprofile ───────────────────────────────────────────────────────────

async def deleteprofile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_restricted(update, context):
        return
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)
    profiles = await profile_db.get_profiles(db_path, user_id)

    if not profiles:
        await update.effective_message.reply_text(t(lang, "No profiles to delete."))
        return

    rows: list[list[InlineKeyboardButton]] = []
    for p in profiles:
        masked = f"{p.nin[:4]}…{p.nin[-4:]}"
        rows.append([
            InlineKeyboardButton(
                text=f"#{p.id} {p.name} ({masked}) — {p.wilaya_name}",
                callback_data=f"del_prof:{p.id}",
            )
        ])
    await update.effective_message.reply_text(
        t(lang, "Select a profile to *delete*:"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_delete_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    profile_id = int((query.data or "").split(":", 1)[1])
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    deleted = await profile_db.delete_profile(db_path, profile_id, user_id)
    if deleted:
        await query.edit_message_text(f"✅ Profile #{profile_id} deleted.")
    else:
        await query.edit_message_text("❌ Profile not found or already deleted.")


# ─── /editprofile ─────────────────────────────────────────────────────────────

async def editprofile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_restricted(update, context):
        return
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)
    profiles = await profile_db.get_profiles(db_path, user_id)

    if not profiles:
        await update.effective_message.reply_text(t(lang, "No profiles to edit."))
        return

    rows: list[list[InlineKeyboardButton]] = []
    for p in profiles:
        masked = f"{p.nin[:4]}…{p.nin[-4:]}"
        rows.append([
            InlineKeyboardButton(
                text=f"#{p.id} {p.name} ({masked}) — {p.wilaya_name}",
                callback_data=f"edit_prof:{p.id}",
            )
        ])
    await update.effective_message.reply_text(
        t(lang, "Select a profile to *edit*:"),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def on_edit_profile_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    profile_id = int((query.data or "").split(":", 1)[1])
    context.user_data["editing_profile_id"] = profile_id

    fields = [
        ("name", "Name"), ("nin", "NIN"), ("cnibe", "CNIBE"), ("phone", "Phone"),
        ("password", "Password"), ("email", "Email"),
        ("payment_method", "Payment Method"), ("status", "Status"),
    ]
    rows = []
    for field, label in fields:
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"edit_field:{profile_id}:{field}",
            )
        ])
    await query.edit_message_text(
        f"Editing profile *#{profile_id}*. Select a field:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# Edit-field conversation states
EDIT_WAITING_VALUE = 100


async def on_edit_field_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    parts = (query.data or "").split(":", 2)
    profile_id = int(parts[1])
    field = parts[2]

    # Status uses inline keyboard, not text input
    if field == "status":
        context.user_data["edit_profile_id"] = profile_id
        context.user_data["edit_field"] = field
        statuses = ["pending", "pre-registered", "registered", "ordered"]
        st_rows = [
            [InlineKeyboardButton(text=st.capitalize(), callback_data=f"edit_st:{profile_id}:{st}")]
            for st in statuses
        ]
        await query.edit_message_text(
            f"Select new *status* for profile #{profile_id}:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(st_rows),
        )
        return EDIT_WAITING_VALUE

    # Payment method uses inline keyboard, not text input
    if field == "payment_method":
        context.user_data["edit_profile_id"] = profile_id
        context.user_data["edit_field"] = field
        pm_rows = [
            [InlineKeyboardButton(text=label, callback_data=f"edit_pm:{profile_id}:{code}")]
            for code, label in _PAYMENT_METHODS.items()
        ]
        await query.edit_message_text(
            f"Select new *payment method* for profile #{profile_id}:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(pm_rows),
        )
        return EDIT_WAITING_VALUE

    context.user_data["edit_profile_id"] = profile_id
    context.user_data["edit_field"] = field

    field_labels = {
        "name": "Profile Name",
        "nin": "NIN (18 digits)",
        "cnibe": "CNIBE (9 digits)",
        "phone": "Phone (10 digits, starts with 0)",
        "password": "Password",
        "email": "Email (or `-` for none)",
    }
    await query.edit_message_text(f"Enter the new value for *{field_labels.get(field, field)}*:", parse_mode="Markdown")
    return EDIT_WAITING_VALUE


async def on_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    field = context.user_data.get("edit_field", "")
    profile_id = context.user_data.get("edit_profile_id", 0)
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)

    # Validate based on field
    if field == "name":
        if not text:
            await update.message.reply_text(t(lang, "❌ Name cannot be empty. Try again:"))
            return EDIT_WAITING_VALUE
    elif field == "nin":
        if not text.isdigit() or len(text) != 18:
            await update.message.reply_text(t(lang, "❌ NIN must be 18 digits. Try again:"))
            return EDIT_WAITING_VALUE
    elif field == "cnibe":
        if not text.isdigit() or len(text) != 9:
            await update.message.reply_text(t(lang, "❌ CNIBE must be 9 digits. Try again:"))
            return EDIT_WAITING_VALUE
    elif field == "phone":
        if not text.isdigit() or len(text) != 10 or not text.startswith("0"):
            await update.message.reply_text(t(lang, "❌ Phone must be 10 digits starting with 0. Try again:"))
            return EDIT_WAITING_VALUE
    elif field == "password":
        errors = _validate_password(text)
        if errors:
            bullet_list = "\n".join(f"  • {e}" for e in errors)
            await update.message.reply_text(t(lang, "❌ Invalid password:\n{bullet_list}\n\nTry again:").format(bullet_list=bullet_list))
            return EDIT_WAITING_VALUE
    elif field == "email":
        if text.lower() in ("-", "skip", "none", "aucun", "no", "لا"):
            text = ""
        elif not _validate_email(text):
            await update.message.reply_text(t(lang, "❌ Invalid email format. Try again or send `-` to skip:"))
            return EDIT_WAITING_VALUE

    db_path: str = context.application.bot_data["db_path"]

    try:
        await profile_db.update_profile_field(db_path, profile_id, user_id, field, text)
    except Exception as exc:
        logger.exception("Failed to update profile field")
        await update.message.reply_text(t(lang, "❌ Failed: {exc}").format(exc=exc))
        return ConversationHandler.END

    context.user_data.pop("edit_profile_id", None)
    context.user_data.pop("edit_field", None)

    await update.message.reply_text(
        t(lang, "✅ Profile #{profile_id} *{field}* updated.").format(profile_id=profile_id, field=field),
        parse_mode="Markdown"
    )
    await _revalidate_and_warn(update, context, profile_id)
    return ConversationHandler.END


async def on_edit_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle payment method selection during profile edit."""
    query = update.callback_query
    if not query:
        return EDIT_WAITING_VALUE
    await query.answer()

    parts = (query.data or "").split(":", 2)
    profile_id = int(parts[1])
    method = parts[2]

    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)

    if method not in _PAYMENT_METHODS:
        await query.edit_message_text(t(lang, "❌ Invalid payment method. Try again."))
        return EDIT_WAITING_VALUE

    db_path: str = context.application.bot_data["db_path"]
    try:
        await profile_db.update_profile_field(db_path, profile_id, user_id, "payment_method", method)
    except Exception as exc:
        logger.exception("Failed to update payment method")
        await query.edit_message_text(f"❌ Failed: {exc}")
        return ConversationHandler.END

    context.user_data.pop("edit_profile_id", None)
    context.user_data.pop("edit_field", None)

    label = _PAYMENT_METHODS[method]
    await query.edit_message_text(
        f"✅ Profile #{profile_id} payment method updated to *{label}*.",
        parse_mode="Markdown",
    )
    await _revalidate_and_warn(update, context, profile_id)
    return ConversationHandler.END


async def on_edit_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle status selection during profile edit."""
    query = update.callback_query
    if not query:
        return EDIT_WAITING_VALUE
    await query.answer()

    parts = (query.data or "").split(":", 2)
    profile_id = int(parts[1])
    status = parts[2]

    valid_statuses = {"pending", "pre-registered", "registered", "ordered"}
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)
    if status not in valid_statuses:
        await query.edit_message_text(t(lang, "❌ Invalid status. Try again."))
        return EDIT_WAITING_VALUE

    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    try:
        await profile_db.update_profile_field(db_path, profile_id, user_id, "status", status)
    except Exception as exc:
        logger.exception("Failed to update status")
        await query.edit_message_text(f"❌ Failed: {exc}")
        return ConversationHandler.END

    context.user_data.pop("edit_profile_id", None)
    context.user_data.pop("edit_field", None)

    await query.edit_message_text(
        f"✅ Profile #{profile_id} status updated to *{status.capitalize()}*.",
        parse_mode="Markdown",
    )
    await _revalidate_and_warn(update, context, profile_id)
    return ConversationHandler.END


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("edit_profile_id", None)
    context.user_data.pop("edit_field", None)
    lang = await get_lang(context, update.effective_user.id)
    await update.effective_message.reply_text(t(lang, "Edit cancelled."))
    return ConversationHandler.END


def _validate_email(email: str) -> bool:
    """Return True if email is valid or empty."""
    if not email:
        return True
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))


def build_editprofile_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(on_edit_field_select, pattern=r"^edit_field:"),
        ],
        states={
            EDIT_WAITING_VALUE: [
                CallbackQueryHandler(on_edit_payment_method, pattern=r"^edit_pm:"),
                CallbackQueryHandler(on_edit_status, pattern=r"^edit_st:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, on_edit_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", edit_cancel)],
        per_user=True,
        per_chat=True,
        per_message=False,
    )


# ─── /reorder ─────────────────────────────────────────────────────────────────

REORDER_WAITING = 200


async def reorder_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if await check_restricted(update, context):
        return ConversationHandler.END
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)
    profiles = await profile_db.get_profiles(db_path, user_id)

    if len(profiles) < 2:
        await update.effective_message.reply_text(t(lang, "You need at least 2 profiles to reorder."))
        return ConversationHandler.END

    lines = ["Current order:\n"]
    for i, p in enumerate(profiles, 1):
        masked = f"{p.nin[:4]}…{p.nin[-4:]}"
        lines.append(f"  {i}. `#{p.id}` {p.name} ({masked}) — {p.wilaya_name}")

    context.user_data["reorder_profiles"] = profiles
    lines.append(
        "\nEnter the new order as profile *IDs* separated by spaces.\n"
        f"Example: `{' '.join(str(p.id) for p in reversed(profiles))}`"
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")
    return REORDER_WAITING


async def reorder_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    profiles = context.user_data.get("reorder_profiles", [])
    valid_ids = {p.id for p in profiles}

    try:
        new_order = [int(x) for x in text.split()]
    except ValueError:
        lang = await get_lang(context, update.effective_user.id)
        await update.message.reply_text(t(lang, "❌ Enter profile IDs as numbers separated by spaces."))
        return REORDER_WAITING

    if set(new_order) != valid_ids:
        await update.message.reply_text(
            f"❌ You must include all profile IDs exactly once: {', '.join(str(i) for i in sorted(valid_ids))}"
        )
        return REORDER_WAITING

    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    await profile_db.reorder_profiles(db_path, user_id, new_order)

    context.user_data.pop("reorder_profiles", None)
    await update.message.reply_text("✅ Profiles reordered.")
    return ConversationHandler.END


async def reorder_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reorder_profiles", None)
    await update.effective_message.reply_text("Reorder cancelled.")
    return ConversationHandler.END


def build_reorder_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CommandHandler("reorder", reorder_start),
            CallbackQueryHandler(reorder_start, pattern=r"^menu:cmd:reorder$"),
        ],
        states={
            REORDER_WAITING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reorder_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", reorder_cancel)],
        per_user=True,
        per_chat=True,
        per_message=False,
    )


async def _revalidate_and_warn(update: Update, context: ContextTypes.DEFAULT_TYPE, profile_id: int):
    """Re-check profile validity after an edit and notify the user if it's still invalid."""
    db_path = context.application.bot_data.get("db_path", "")
    user_id = update.effective_user.id
    
    # Reload profile to get current state
    profile = await profile_db.get_profile(db_path, profile_id, user_id)
    if not profile:
        return

    # Validation logic (sync with admin.py and registration.py)
    is_valid_email = _validate_email(profile.email)
    other_errors = []
    if not profile.nin.isdigit() or len(profile.nin) != 18:
        other_errors.append("NIN")
    if not profile.cnibe.isdigit() or len(profile.cnibe) != 9:
        other_errors.append("CNIBE")
    if not profile.phone.isdigit() or len(profile.phone) != 10 or not profile.phone.startswith("0"):
        other_errors.append("Phone")
    
    pw_errs = _validate_password(profile.password)
    if pw_errs:
        other_errors.append("Password")

    conforms = (is_valid_email and not other_errors)
    new_is_valid = 1 if conforms else 0
    
    # Update DB if status changed
    if profile.is_valid != new_is_valid:
        try:
            await profile_db.update_profile_field(db_path, profile.id, user_id, "is_valid", new_is_valid)
        except Exception:
            logger.exception("Failed to update is_valid during re-validation")

    if not conforms:
        msg = (
            "⚠️ *Warning: Profile still invalid*\n\n"
            "This profile is currently **excluded from auto-registration** because it still contains errors:\n"
        )
        if not is_valid_email and profile.email:
            msg += "  • Invalid Email format\n"
        for err in other_errors:
            msg += f"  • Invalid {err}\n"
        
        msg += "\nPlease fix these errors in /profiles to re-enable auto-registration for this profile."
        
        if update.callback_query:
            await update.callback_query.message.reply_text(msg, parse_mode="Markdown")
        else:
            await update.message.reply_text(msg, parse_mode="Markdown")
