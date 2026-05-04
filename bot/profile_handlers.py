"""Profile management commands: add, list, edit, delete, reorder."""
from __future__ import annotations

import logging
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
from .registration import _validate_password

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
    context.user_data["add_profile"] = {}
    await update.effective_message.reply_text(
        "📋 *Add Registration Profile*\n\n"
        "Step 1/9 — Enter a short *Name* for this profile (e.g. 'Dad', 'My Profile'):",
        parse_mode="Markdown",
    )
    return AP_NAME


async def ap_collect_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("❌ Name cannot be empty. Try again:")
        return AP_NAME
    _ap_state(context)["name"] = text
    await update.message.reply_text(
        f"✅ Name '{text}' recorded.\n\nStep 2/9 — Enter the *NIN* (18 digits):",
        parse_mode="Markdown",
    )
    return AP_NIN


async def ap_collect_nin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or len(text) != 18:
        await update.message.reply_text(
            "❌ NIN must be exactly *18 digits*. Try again:",
            parse_mode="Markdown",
        )
        return AP_NIN
    _ap_state(context)["nin"] = text
    await update.message.reply_text(
        "✅ NIN recorded.\n\nStep 3/9 — Enter the *CNIBE* (9 digits):",
        parse_mode="Markdown",
    )
    return AP_CNIBE


async def ap_collect_cnibe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or len(text) != 9:
        await update.message.reply_text(
            "❌ CNIBE must be exactly *9 digits*. Try again:",
            parse_mode="Markdown",
        )
        return AP_CNIBE
    _ap_state(context)["cnibe"] = text
    await update.message.reply_text(
        "✅ CNIBE recorded.\n\nStep 4/9 — Enter the *phone number* (10 digits, starts with 0):",
        parse_mode="Markdown",
    )
    return AP_PHONE


async def ap_collect_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or len(text) != 10 or not text.startswith("0"):
        await update.message.reply_text(
            "❌ Phone must be exactly *10 digits* starting with *0*. Try again:",
            parse_mode="Markdown",
        )
        return AP_PHONE
    _ap_state(context)["phone"] = text
    await update.message.reply_text(
        "✅ Phone recorded.\n\n"
        "Step 5/9 — Enter a *password* for the adhahi.dz account:\n"
        "_(≥6 chars, digit, lowercase, uppercase, special char, no spaces)_",
        parse_mode="Markdown",
    )
    return AP_PASSWORD


async def ap_collect_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    errors = _validate_password(text)
    if errors:
        bullet_list = "\n".join(f"  • {e}" for e in errors)
        await update.message.reply_text(f"❌ Invalid password:\n{bullet_list}\n\nTry again:")
        return AP_PASSWORD

    _ap_state(context)["password"] = text

    wilayas: list[tuple[str, str]] = list(
        context.application.bot_data.get("wilayas", [])
    )
    if not wilayas:
        await update.message.reply_text(
            "⚠️ Wilaya list not available. Try /addprofile later."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "✅ Password recorded.\n\nStep 6/9 — Select the *Wilaya*:",
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
        f"✅ Wilaya *{wilaya_name}* selected.\n\n⏳ Fetching communes…",
        parse_mode="Markdown",
    )

    # Fetch communes
    try:
        from .registration import _fetch_communes
        communes = await _fetch_communes(context, int(code))
    except Exception as exc:
        logger.exception("Failed to fetch communes for wilaya %s", code)
        await query.edit_message_text(f"❌ Failed to fetch communes: {exc}")
        return ConversationHandler.END

    active = [c for c in communes if c.get("isActive")]
    if not active:
        await query.edit_message_text("⚠️ No active communes. Try a different wilaya with /addprofile.")
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
        "Step 7/9 — Select the *Commune*:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return AP_COMMUNE


async def ap_on_commune(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
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
        f"✅ Commune *{commune_name}* selected.\n\n"
        "Step 8/9 — Select a *payment method*:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(pm_rows),
    )
    return AP_PAYMENT_METHOD


async def ap_on_payment_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return AP_PAYMENT_METHOD
    await query.answer()

    method = (query.data or "").split(":", 1)[1]
    if method not in _PAYMENT_METHODS:
        await query.edit_message_text("❌ Invalid payment method. Try again.")
        return AP_PAYMENT_METHOD

    state = _ap_state(context)
    state["payment_method"] = method

    label = _PAYMENT_METHODS[method]
    await query.edit_message_text(
        f"✅ Payment method *{label}* selected.\n\n"
        "Step 9/9 — Enter an *email* (optional, send `-` to skip):",
        parse_mode="Markdown",
    )
    return AP_EMAIL


async def ap_collect_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    state = _ap_state(context)

    if text == "-" or text == "":
        state["email"] = ""
    else:
        state["email"] = text

    # Save to DB
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    try:
        profile_id = await profile_db.add_profile(db_path, user_id, state)
    except Exception as exc:
        logger.exception("Failed to save profile")
        await update.message.reply_text(f"❌ Failed to save profile: {exc}")
        return ConversationHandler.END

    # Clean up temp state
    context.user_data.pop("add_profile", None)

    # Auto check status
    from .registration import check_profile_status
    profile = await profile_db.get_profile(db_path, profile_id, user_id)
    if profile:
        status, _, _ = await check_profile_status(context, profile)
        if status in ("pre-registered", "registered", "pending", "ordered"):
            await profile_db.set_profile_status(db_path, profile_id, status)
        else:
            status = "pending"
    else:
        status = "pending"

    pm_label = _PAYMENT_METHODS.get(state.get('payment_method', 'CASH'), state.get('payment_method', 'CASH'))
    await update.message.reply_text(
        f"🎉 Profile #{profile_id} ('{state.get('name', '')}') saved!\n\n"
        f"NIN: `{state['nin'][:4]}…{state['nin'][-4:]}`\n"
        f"Wilaya: {state.get('wilaya_name', state['wilaya_id'])}\n"
        f"Commune: {state.get('commune_name', state['commune_code'])}\n"
        f"Payment: {pm_label}\n"
        f"Status: {status}\n\n"
        "It will be auto-registered when quota opens.\n"
        "Use /profiles to view all your profiles.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def ap_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("add_profile", None)
    await update.effective_message.reply_text("Profile creation cancelled.")
    return ConversationHandler.END


def build_addprofile_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("addprofile", addprofile_start)],
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
    )


# ─── /profiles ────────────────────────────────────────────────────────────────

async def list_profiles(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    profiles = await profile_db.get_profiles(db_path, user_id)

    if not profiles:
        await update.effective_message.reply_text(
            "No profiles found. Use /addprofile to create one."
        )
        return

    lines = ["📋 *Your Profiles*\n"]
    for i, p in enumerate(profiles, 1):
        status_icon = {"pending": "🟡", "pre-registered": "🔵", "registering": "🔄", "registered": "✅", "ordered": "🐑", "failed": "❌"}.get(p.status, "❓")
        masked_nin = f"{p.nin[:4]}…{p.nin[-4:]}"
        lines.append(
            f"*{i}.* `#{p.id}` **{p.name}** {status_icon} {p.status}\n"
            f"   NIN: `{masked_nin}` | Phone: `{p.phone}`\n"
            f"   {p.wilaya_name} → {p.commune_name}\n"
        )
    lines.append("_Use /editprofile, /deleteprofile, /reorder to manage._")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── /viewprofile ─────────────────────────────────────────────────────────────

async def viewprofile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    profiles = await profile_db.get_profiles(db_path, user_id)

    if not profiles:
        await update.effective_message.reply_text("No profiles to view.")
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
        "Select a profile to *view full details*:",
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
    if not profile:
        await query.edit_message_text("❌ Profile not found.")
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
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    profiles = await profile_db.get_profiles(db_path, user_id)

    if not profiles:
        await update.effective_message.reply_text("No profiles to delete.")
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
        "Select a profile to *delete*:",
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
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    profiles = await profile_db.get_profiles(db_path, user_id)

    if not profiles:
        await update.effective_message.reply_text("No profiles to edit.")
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
        "Select a profile to *edit*:",
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

    # Validate based on field
    if field == "name":
        if not text:
            await update.message.reply_text("❌ Name cannot be empty. Try again:")
            return EDIT_WAITING_VALUE
    elif field == "nin":
        if not text.isdigit() or len(text) != 18:
            await update.message.reply_text("❌ NIN must be 18 digits. Try again:")
            return EDIT_WAITING_VALUE
    elif field == "cnibe":
        if not text.isdigit() or len(text) != 9:
            await update.message.reply_text("❌ CNIBE must be 9 digits. Try again:")
            return EDIT_WAITING_VALUE
    elif field == "phone":
        if not text.isdigit() or len(text) != 10 or not text.startswith("0"):
            await update.message.reply_text("❌ Phone must be 10 digits starting with 0. Try again:")
            return EDIT_WAITING_VALUE
    elif field == "password":
        errors = _validate_password(text)
        if errors:
            bullet_list = "\n".join(f"  • {e}" for e in errors)
            await update.message.reply_text(f"❌ Invalid password:\n{bullet_list}\n\nTry again:")
            return EDIT_WAITING_VALUE
    elif field == "email":
        if text == "-":
            text = ""

    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    try:
        await profile_db.update_profile_field(db_path, profile_id, user_id, field, text)
    except Exception as exc:
        logger.exception("Failed to update profile field")
        await update.message.reply_text(f"❌ Failed: {exc}")
        return ConversationHandler.END

    context.user_data.pop("edit_profile_id", None)
    context.user_data.pop("edit_field", None)

    await update.message.reply_text(f"✅ Profile #{profile_id} *{field}* updated.", parse_mode="Markdown")
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

    if method not in _PAYMENT_METHODS:
        await query.edit_message_text("❌ Invalid payment method. Try again.")
        return EDIT_WAITING_VALUE

    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

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
    if status not in valid_statuses:
        await query.edit_message_text("❌ Invalid status. Try again.")
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
        f"✅ Profile #{profile_id} status updated to *{status}*.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def edit_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("edit_profile_id", None)
    context.user_data.pop("edit_field", None)
    await update.effective_message.reply_text("Edit cancelled.")
    return ConversationHandler.END


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
    )


# ─── /reorder ─────────────────────────────────────────────────────────────────

REORDER_WAITING = 200


async def reorder_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    profiles = await profile_db.get_profiles(db_path, user_id)

    if len(profiles) < 2:
        await update.effective_message.reply_text("You need at least 2 profiles to reorder.")
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
        await update.message.reply_text("❌ Enter profile IDs as numbers separated by spaces.")
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
        entry_points=[CommandHandler("reorder", reorder_start)],
        states={
            REORDER_WAITING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reorder_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", reorder_cancel)],
        per_user=True,
        per_chat=True,
    )
