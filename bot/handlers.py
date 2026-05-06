from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from . import db as db_mod
from . import profile_db
from .api_client import QuotaStatus
from .captcha_solver import LocalOcrSolver, TwoCaptchaSolver
from .registration import _build_headers, _get_http_client

# Canonical list of bot commands — used by the /help handler and set_my_commands.
BOT_COMMANDS = [
    ("start", "Subscribe to wilaya quota notifications"),
    ("change", "Change your subscribed wilaya"),
    ("status", "Check your current subscription status"),
    ("stop", "Unsubscribe from notifications"),
    ("fetchinfo", "Last fetch time & watched wilayas"),
    ("register", "Manual adhahi.dz registration flow"),
    ("addprofile", "Add an auto-registration profile"),
    ("profiles", "List your registration profiles"),
    ("editprofile", "Edit a registration profile"),
    ("deleteprofile", "Delete a registration profile"),
    ("viewprofile", "View full profile details (incl. password)"),
    ("reorder", "Change profile priority order"),
    ("verifyotp", "Verify OTP for a submitted profile"),
    ("checkprofile", "Check if a profile NIN is registered on server"),
    ("testcaptchasolvers", "Test both CAPTCHA solvers side-by-side"),
    ("help", "Show all available commands"),
]

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


async def fetchinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the last successful quota fetch time and all watched wilayas."""
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    # --- Last fetch timestamp ---
    raw_ts: str | None = context.application.bot_data.get("last_fetch_ts")
    if raw_ts:
        try:
            dt = datetime.fromisoformat(raw_ts).astimezone(timezone.utc)
            fetch_line = f"🕐 *Last fetch:* `{dt.strftime('%Y-%m-%d %H:%M:%S')} UTC`"
        except ValueError:
            fetch_line = f"🕐 *Last fetch:* `{raw_ts}`"
    else:
        fetch_line = "🕐 *Last fetch:* _not done yet_"

    # --- Wilayas being watched (scoped to the requesting user) ---
    wilaya_lookup = dict(_get_wilaya_list(context))  # code -> name

    # Subscription
    try:
        sub_code = await db_mod.get_user_subscription_wilaya(db_path, user_id)
        sub_codes = {sub_code} if sub_code else set()
    except Exception:
        logger.exception("fetchinfo: failed to load subscription wilaya")
        sub_codes = set()

    # Pending auto-registration profiles
    try:
        prof_codes = set(await profile_db.get_user_profile_wilayas(db_path, user_id))
    except Exception:
        logger.exception("fetchinfo: failed to load profile wilayas")
        prof_codes = set()

    all_codes = sub_codes | prof_codes

    if not all_codes:
        watched_section = "_No wilayas are currently being watched._"
    else:
        rows = []
        for code in sorted(all_codes):
            name = wilaya_lookup.get(code, code)
            tags = []
            if code in sub_codes:
                tags.append("📬 subscription")
            if code in prof_codes:
                tags.append("🤖 auto-reg")
            rows.append(f"  • *{name}* ({code}) — {', '.join(tags)}")
        watched_section = "\n".join(rows)

    msg = (
        f"{fetch_line}\n\n"
        f"👁 *Watched wilayas:*\n{watched_section}"
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def test_captcha_solvers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch 5 CAPTCHAs and solve with both ddddocr and 2captcha for comparison."""
    import base64
    import io
    import os
    import time

    from .registration import _build_headers, _get_http_client

    api_key = os.getenv("TWO_CAPTCHA_API_KEY", "").strip()
    has_2captcha = bool(api_key)

    await update.message.reply_text(
        "🧪 *CAPTCHA Solver Test*\n\n"
        f"Solvers: `ddddocr` {'+ `2captcha`' if has_2captcha else '(2captcha not configured)'}\n"
        "Fetching 5 CAPTCHAs…",
        parse_mode="Markdown",
    )

    # Initialize solvers
    ocr_solver = LocalOcrSolver()
    two_solver = TwoCaptchaSolver(api_key) if has_2captcha else None

    client = _get_http_client(context)
    headers = _build_headers(context)

    for i in range(1, 6):
        try:
            resp = await client.get("/api/v1/captcha/generate", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            captcha_id = data["captchaId"]
            image_uri = data["captchaImage"]
            b64 = image_uri.split(",", 1)[1] if "," in image_uri else image_uri
            image_bytes = base64.b64decode(b64)
        except Exception as exc:
            await update.message.reply_text(f"❌ Failed to fetch CAPTCHA #{i}: `{exc}`", parse_mode="Markdown")
            continue

        # Solve with ddddocr
        try:
            t0 = time.monotonic()
            ocr_answer = await ocr_solver.solve(image_bytes)
            ocr_time = time.monotonic() - t0
            ocr_result = f"`{ocr_answer}` ({ocr_time:.2f}s)"
        except Exception as exc:
            ocr_result = f"❌ error: `{exc}`"

        # Solve with 2captcha
        if two_solver:
            try:
                t0 = time.monotonic()
                two_answer = await two_solver.solve(image_bytes)
                two_time = time.monotonic() - t0
                two_result = f"`{two_answer}` ({two_time:.1f}s)"
            except Exception as exc:
                two_result = f"❌ error: `{exc}`"
        else:
            two_result = "_not configured_"

        caption = (
            f"🔐 *CAPTCHA #{i}*  (`{captcha_id[:8]}…`)\n\n"
            f"🤖 ddddocr: {ocr_result}\n"
            f"👤 2captcha: {two_result}"
        )

        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=io.BytesIO(image_bytes),
            caption=caption,
            parse_mode="Markdown",
        )

    await update.message.reply_text("✅ Test complete! Review the answers above.")


async def checkprofile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all profiles as inline buttons for the user to check registration status."""
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]

    profiles = await profile_db.get_profiles(db_path, user_id)
    if not profiles:
        await update.message.reply_text("You have no profiles. Use /addprofile first.")
        return

    buttons = []
    for p in profiles:
        masked = f"{p.nin[:4]}…{p.nin[-4:]}"
        label = f"{p.name or masked} ({p.phone})"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"chk_prof:{p.id}")])

    await update.message.reply_text(
        "🔍 *Check Profile Registration*\n\nSelect a profile to check:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def on_check_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback handler for chk_prof:<id> buttons."""
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    if not data.startswith("chk_prof:"):
        return

    profile_id = int(data.split(":", 1)[1])
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]

    profile = await profile_db.get_profile(db_path, profile_id, user_id)
    if not profile:
        await query.edit_message_text("Profile not found.")
        return

    masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
    await query.edit_message_text(
        f"⏳ Checking profile *{profile.name or masked}*…",
        parse_mode="Markdown",
    )

    from .registration import check_profile_status
    status, msg, code = await check_profile_status(context, profile)

    if status == "error":
        logger.error("checkprofile network error: %s", msg)
        await query.edit_message_text(
            f"❌ {msg}", parse_mode="Markdown",
        )
        return

    logger.info("checkprofile response: profile=%s status=%s body=%s", profile.id, code, msg)

    # Always update the status in the DB
    await profile_db.set_profile_status(db_path, profile.id, status)

    if status == "pre-registered":
        await query.edit_message_text(
            f"🔵 *Profile registered on server!*\n\n"
            f"Profile: *{profile.name or masked}*\n"
            f"Phone: `{profile.phone}`\n\n"
            "Status updated to *pre-registered*.\n"
            f"{msg}",
            parse_mode="Markdown",
        )
    elif status == "registered":
        await query.edit_message_text(
            f"🟢 *Profile is already Active!*\n\n"
            f"Profile: *{profile.name or masked}*\n\n"
            f"Status updated to *registered*.\n"
            f"{msg}",
            parse_mode="Markdown",
        )
    elif status == "ordered":
        await query.edit_message_text(
            f"🎉 *Profile has a Pending Order!*\n\n"
            f"Profile: *{profile.name or masked}*\n\n"
            f"Status updated to *ordered*.\n"
            f"{msg}",
            parse_mode="Markdown",
        )
    else:
        await query.edit_message_text(
            f"⚪ *Profile NOT registered on server*\n\n"
            f"Profile: *{profile.name or masked}*\n"
            f"Status updated to *pending*.\n"
            f"HTTP {code}: {msg}",
            parse_mode="Markdown",
        )



async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["📖 *Available Commands*\n"]
    for cmd, desc in BOT_COMMANDS:
        lines.append(f"/{cmd} — {desc}")
    lines.append("\n/cancel — Cancel an in-progress registration")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


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
