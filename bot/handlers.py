from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from telegram import ChatMember, InlineKeyboardButton, InlineKeyboardMarkup, Update, constants
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from . import db as db_mod
from . import profile_db
from .admin import ADMIN_TELEGRAM_ID, check_restricted, is_admin
from .api_client import QuotaStatus
from .captcha_solver import LocalOcrSolver, TwoCaptchaSolver
from .registration import _build_headers, _get_http_client
from .i18n import t, get_lang
from .notifier import safe_query_answer
import unicodedata
import re

# Canonical list of bot commands — used by the /help handler and set_my_commands.
BOT_COMMANDS = [
    ("start", "Subscribe to wilaya quota notifications"),
    ("menu", "Open the interactive main menu"),
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

    db_path = context.application.bot_data.get("db_path")
    if db_path:
        cached = await db_mod.get_cached_wilayas(db_path)
        if cached:
            context.application.bot_data["wilayas"] = cached
            return cached

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
        
        if db_path and items:
            wilaya_dicts = [{"code": code, "name": name} for code, name in items]
            await db_mod.save_wilayas(db_path, wilaya_dicts)
            
        return items
    except Exception:
        logger.exception("Failed to refresh wilaya list on demand")
        return []


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    lang = await get_lang(context, update.effective_user.id)
    from .menu import get_reply_main_menu_keyboard
    
    # Send the main menu keyboard first
    await update.effective_message.reply_text(
        t(lang, "📱 *Main Menu*"),
        reply_markup=get_reply_main_menu_keyboard(lang),
        parse_mode="Markdown"
    )

    wilayas = await _ensure_wilayas_loaded(context)
    if not wilayas:
        await update.effective_message.reply_text(
            t(lang, "Welcome! I couldn't load the Wilaya list yet (API unavailable). Send /change later to try again.")
        )
        return

    await update.effective_message.reply_text(
        t(lang, "Welcome! Choose your Wilaya to receive quota notifications:"),
        reply_markup=_wilaya_keyboard(wilayas),
    )


async def change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lang = await get_lang(context, update.effective_user.id)
    wilayas = await _ensure_wilayas_loaded(context)
    if not wilayas:
        await update.effective_message.reply_text(t(lang, "Wilaya list not available yet. Please try again later."))
        return

    await update.effective_message.reply_text(t(lang, "Choose your Wilaya:"), reply_markup=_wilaya_keyboard(wilayas))


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_restricted(update, context):
        return
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)
    deleted = await db_mod.delete_subscription(db_path, user_id)
    if deleted:
        await update.effective_message.reply_text(t(lang, "You have been unsubscribed."))
    else:
        await update.effective_message.reply_text(t(lang, "You are not subscribed."))


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await check_restricted(update, context):
        return
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)

    sub = await db_mod.get_subscription(db_path, user_id)
    if not sub:
        await update.effective_message.reply_text(t(lang, "You are not subscribed. Send /start to subscribe."))
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
    if await check_restricted(update, context):
        return
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    lang = await get_lang(context, user_id)

    # --- Last fetch timestamp ---
    raw_ts: str | None = context.application.bot_data.get("last_fetch_ts")
    if raw_ts:
        try:
            dt = datetime.fromisoformat(raw_ts).astimezone(timezone.utc)
            time_str = f"`{dt.strftime('%Y-%m-%d %H:%M:%S')} UTC`"
            fetch_line = t(lang, "🕐 *Last fetch:* {time}").format(time=time_str)
        except ValueError:
            fetch_line = t(lang, "🕐 *Last fetch:* {time}").format(time=f"`{raw_ts}`")
    else:
        fetch_line = t(lang, "🕐 *Last fetch:* {time}").format(time=f"_{t(lang, 'not done yet')}_")

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
        watched_section = t(lang, "_No wilayas are currently being watched._")
    else:
        rows = []
        for code in sorted(all_codes):
            name = wilaya_lookup.get(code, code)
            tags = []
            if code in sub_codes:
                tags.append(t(lang, "📬 subscription"))
            if code in prof_codes:
                tags.append(t(lang, "🤖 auto-reg"))
            rows.append(f"  • *{name}* ({code}) — {', '.join(tags)}")
        watched_section = "\n".join(rows)

    msg = (
        f"{fetch_line}\n\n"
        f"{t(lang, '👁 *Watched wilayas:*\\n')}{watched_section}"
    )
    await update.effective_message.reply_text(msg, parse_mode="Markdown")


async def test_captcha_solvers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch 5 CAPTCHAs and solve with both ddddocr and 2captcha for comparison."""
    if not is_admin(update):
        user = update.effective_user
        lang = await get_lang(context, user.id)
        if ADMIN_TELEGRAM_ID:
            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=f"⚠️ *Unauthorized Access Attempt*\nUser [{user.first_name}](tg://user?id={user.id}) (ID: `{user.id}`) tried to use `/testcaptchasolvers`.",
                parse_mode="Markdown"
            )
        await update.message.reply_text(t(lang, "⛔ This command is restricted to the administrator to prevent resource waste."))
        return

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
    if not is_admin(update):
        user = update.effective_user
        lang = await get_lang(context, user.id)
        if ADMIN_TELEGRAM_ID:
            await context.bot.send_message(
                chat_id=ADMIN_TELEGRAM_ID,
                text=f"⚠️ *Unauthorized Access Attempt*\nUser [{user.first_name}](tg://user?id={user.id}) (ID: `{user.id}`) tried to use `/checkprofile`.",
                parse_mode="Markdown"
            )
        await update.message.reply_text(t(lang, "⛔ This command is restricted to the administrator to prevent resource waste."))
        return

    if await check_restricted(update, context):
        return
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)
    db_path: str = context.application.bot_data["db_path"]

    profiles = await profile_db.get_profiles(db_path, user_id)
    if not profiles:
        await update.message.reply_text(t(lang, "You have no profiles. Use /addprofile first."))
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
    try:
        await safe_query_answer(query)
    except BadRequest:
        logger.debug("Stale callback query in on_check_profile — ignoring.")
        return

    data = query.data or ""
    if not data.startswith("chk_prof:"):
        return

    profile_id = int(data.split(":", 1)[1])
    user_id = update.effective_user.id
    lang = await get_lang(context, user_id)
    db_path: str = context.application.bot_data["db_path"]

    profile = await profile_db.get_profile(db_path, profile_id, user_id)
    if not profile:
        await query.edit_message_text(t(lang, "Profile not found."))
        return

    masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
    await query.edit_message_text(
        f"⏳ Checking profile *{profile.name or masked}*…",
        parse_mode="Markdown",
    )

    from .registration import check_profile_status
    from .proxy import get_proxy_url
    
    api_client = context.application.bot_data.get("api_client")
    use_proxy = context.application.bot_data.get("proxy_checkprof", False)
    proxy_url = get_proxy_url(session_id=profile.nin) if use_proxy else None
    
    status, msg, code = await check_profile_status(
        api_client, 
        profile, 
        proxy_url=proxy_url,
        bot_data=context.application.bot_data
    )

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
    if update.effective_chat.type != constants.ChatType.PRIVATE:
        return
    lang = await get_lang(context, update.effective_user.id)
    lines = [t(lang, "📖 *Available Commands*\n")]
    for cmd, desc in BOT_COMMANDS:
        lines.append(f"/{cmd} — {desc}")
    lines.append(t(lang, "\n/cancel — Cancel an in-progress registration"))
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


async def on_wilaya_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    try:
        await safe_query_answer(query)
    except BadRequest:
        logger.debug("Stale callback query in on_wilaya_selected — ignoring.")
        return

    data = query.data or ""
    if not data.startswith("wilaya:"):
        return

    wilaya_code = data.split(":", 1)[1]
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    await db_mod.set_subscription(db_path, user_id, wilaya_code)

    lang = await get_lang(context, user_id)
    wilaya_name = _lookup_wilaya_name(context, wilaya_code)
    text = t(lang, "You will be notified when quota is available in {wilaya_name}.").format(wilaya_name=wilaya_name)
    await query.edit_message_text(text)


async def on_my_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Detect when a user blocks or unblocks the bot, or when invited to chats."""
    if not update.my_chat_member:
        return

    new_status = update.my_chat_member.new_chat_member.status
    chat = update.my_chat_member.chat
    from_user = update.my_chat_member.from_user

    # --- Leave unauthorized groups/channels ---
    if chat.type in (constants.ChatType.GROUP, constants.ChatType.SUPERGROUP, constants.ChatType.CHANNEL):
        if new_status in (constants.ChatMemberStatus.MEMBER, constants.ChatMemberStatus.ADMINISTRATOR):
            if not from_user or from_user.id != ADMIN_TELEGRAM_ID:
                inviter_name = from_user.full_name if from_user else "Unknown"
                inviter_id = from_user.id if from_user else "Unknown"
                logger.warning(
                    "Bot added to %s '%s' (ID: %s) by unauthorized user %s (ID: %s). Leaving...",
                    chat.type, chat.title, chat.id, inviter_name, inviter_id
                )
                try:
                    await context.bot.leave_chat(chat.id)
                except Exception:
                    logger.exception("Failed to leave chat %s", chat.id)
                return

    # --- Private chat (block/unblock) ---
    if chat.type == constants.ChatType.PRIVATE:
        user_id = update.effective_user.id
        # In private chats, 'kicked' status means the user blocked the bot.
        if new_status == constants.ChatMemberStatus.BANNED:
            logger.warning("User %s blocked the bot. Deleting all data.", user_id)
            db_path = context.application.bot_data.get("db_path")
            if db_path:
                try:
                    await db_mod.delete_user_data(db_path, user_id)
                except Exception:
                    logger.exception("Failed to delete data for blocked user %s", user_id)
        elif new_status == constants.ChatMemberStatus.MEMBER:
            logger.info("User %s (re)started the bot.", user_id)



def _normalize_wilaya_name(text: str) -> str:
    """Normalize wilaya names for robust lookup (lowercase, no accents, no prefixes)."""
    if not text:
        return ""
    # Lowercase and strip
    text = text.lower().strip()
    # Remove accents (e.g., Tébessa -> Tebessa)
    text = "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )
    # Remove common prefixes
    for prefix in ["wilaya de ", "wilaya ", "ولاية ", "الولاية ", "ال"]:
        if text.startswith(prefix):
            text = text[len(prefix):]
    # Remove all non-alphanumeric characters
    text = re.sub(r"[^a-z0-9\u0621-\u064A]", "", text)
    return text


_WILAYA_NAME_TO_CODE = {
    "adrar": "01", "ادرار": "01",
    "chlef": "02", "شلف": "02",
    "laghouat": "03", "اغواط": "03",
    "oumelbouaghi": "04", "امالبواقي": "04", "oeb": "04",
    "batna": "05", "باتنة": "05", "باتنه": "05",
    "bejaia": "06", "بجاية": "06", "بجايه": "06",
    "biskra": "07", "بسكرة": "07", "بسكره": "07",
    "bechar": "08", "بشار": "08",
    "blida": "09", "بليدة": "09", "بليده": "09",
    "bouira": "10", "بويرة": "10", "بويره": "10",
    "tamanrasset": "11", "تمنراست": "11", "tam": "11", "tamanghasset": "11",
    "tebessa": "12", "تبسة": "12", "تبسه": "12",
    "tlemcen": "13", "تلمسان": "13",
    "tiaret": "14", "تيارت": "14",
    "tiziouzou": "15", "تيزيوزو": "15", "to": "15", "tizi": "15",
    "alger": "16", "جزائر": "16", "algiers": "16", "eldjazair": "16",
    "djelfa": "17", "جلفة": "17",
    "jijel": "18", "جيجل": "18",
    "setif": "19", "سطيف": "19",
    "saida": "20", "سعيدة": "20", "سعيده": "20",
    "skikda": "21", "سكيكدة": "21", "سكيكده": "21",
    "sidibelabbes": "22", "سيديبيلعباس": "22", "sba": "22", "belabbes": "22",
    "annaba": "23", "عنابة": "23", "عنابه": "23",
    "guelma": "24", "قالمة": "24", "قالمه": "24",
    "constantine": "25", "قسنطينة": "25", "قسنطينه": "25",
    "medea": "26", "مدية": "26", "مديه": "26",
    "mostaganem": "27", "مستغانم": "27", "mosta": "27",
    "msila": "28", "مسيلة": "28", "مسيله": "28",
    "mascara": "29", "معسكر": "29",
    "ouargla": "30", "ورقلة": "30", "ورقله": "30",
    "oran": "31", "وهران": "31", "wahran": "31",
    "elbayadh": "32", "بيض": "32",
    "illizi": "33", "اليزي": "33", "ايليزي": "33",
    "bordjbouarreridj": "34", "برجبوعريريج": "34", "bba": "34",
    "boumerdes": "35", "بومرداس": "35",
    "eltarf": "36", "طارف": "36",
    "tindouf": "37", "تندوف": "37",
    "tissemsilt": "38", "تسمسيلت": "38",
    "eloued": "39", "وادي": "39", "souf": "39",
    "khenchela": "40", "خنشلة": "40", "خنشله": "40",
    "soukahras": "41", "سوقاهراس": "41",
    "tipaza": "42", "تيبازة": "42", "تيبازه": "42",
    "mila": "43", "ميلة": "43", "ميله": "43",
    "aindefla": "44", "عينالدفلى": "44", "دفلى": "44",
    "naama": "45", "نعامة": "45", "نعامه": "45",
    "aintemouchent": "46", "عينتموشنت": "46", "تموشنت": "46",
    "ghardaia": "47", "غرداية": "47", "غردايه": "47",
    "relizane": "48", "غليزان": "48",
    "elmghair": "49", "مغير": "49", "mghair": "49", "elmeghaier": "49",
    "elmenia": "50", "منيعة": "50", "منيعه": "50", "elmeniaa": "50",
    "ouleddjellal": "51", "اولادجلال": "51",
    "bordjbajimokhtar": "52", "برجباجيمختار": "52", "bbm": "52",
    "beniabbes": "53", "بنيعباس": "53",
    "timimoun": "54", "تيميمون": "54",
    "touggourt": "55", "تقرت": "55",
    "djanet": "56", "جانت": "56",
    "insalah": "57", "عينصالح": "57", "انصالح": "57",
    "inguezzam": "58", "عينقزام": "58", "انقزام": "58"
}


async def wilaya_lookup_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle messages containing only a wilaya code (1-58)."""
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    wilaya_code = None

    if text.isdigit():
        code_num = int(text)
        if 1 <= code_num <= 58:
            wilaya_code = str(code_num).zfill(2)
    else:
        # Try name lookup
        normalized = _normalize_wilaya_name(text)
        wilaya_code = _WILAYA_NAME_TO_CODE.get(normalized)

    if not wilaya_code:
        return

    db_path = context.application.bot_data["db_path"]

    last_open = await db_mod.get_last_open_time(db_path, wilaya_code)
    wilaya_name = _lookup_wilaya_name(context, wilaya_code)

    if last_open:
        try:
            # SQLite datetime('now') returns 'YYYY-MM-DD HH:MM:SS'
            # But sometimes it might have 'T' or other formats depending on how it was inserted.
            # We'll try to parse it cleanly.
            ts_str = last_open.replace("T", " ")
            dt = datetime.fromisoformat(ts_str)
            formatted_date = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            formatted_date = last_open

        response = (
            f"📍 *{wilaya_name}* ({wilaya_code})\n\n"
            f"📅 آخر مرة فُتحت فيها كانت بتاريخ:\n"
            f"`{formatted_date}`"
        )
    else:
        response = (
            f"📍 *{wilaya_name}* ({wilaya_code})\n\n"
            f"❌ لا يوجد سجل لفتح هذه الولاية في قاعدة البيانات حالياً."
        )

    await message.reply_text(response, parse_mode="Markdown")
