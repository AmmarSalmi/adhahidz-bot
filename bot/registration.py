"""Forced registration multi-step conversation flow.

Steps:
  1. Collect NIN (18 digits)
  2. Collect CNIBE (9 digits)
  3. Collect phone (10 digits, starts with 0)
  4. Wilaya selection
  5. Commune selection
  6. Generate & display CAPTCHA
  7. Collect CAPTCHA answer
  8. Submit registration
  9. Collect OTP
 10. Verify OTP
"""
from __future__ import annotations

import base64
import io
import logging
import time
from typing import Any

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

# Conversation states
(
    ASK_NIN,
    ASK_CNIBE,
    ASK_PHONE,
    ASK_WILAYA,
    ASK_COMMUNE,
    SHOW_CAPTCHA,
    ASK_CAPTCHA,
    ASK_PASSWORD,
    SUBMITTING,
    ASK_OTP,
    VERIFYING_OTP,
) = range(11)

# How long a CAPTCHA is valid (seconds)
_CAPTCHA_TTL_S = 300

# Standard headers used for registration endpoints
_REG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
    "Accept": "application/json",
    "Referer": "https://adhahi.dz/register",
}


def _get_http_client(context: ContextTypes.DEFAULT_TYPE) -> httpx.AsyncClient:
    """Return the bot's existing httpx client."""
    api = context.application.bot_data.get("api_client")
    if api is None:
        raise RuntimeError("API client not initialized")
    return api._client  # noqa: SLF001 — we need the underlying httpx client


def _session_cookie(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Extract the session cookie from the httpx client's cookie jar."""
    client = _get_http_client(context)
    cookies = client.cookies
    # Build a cookie header string from whatever cookies exist
    parts = [f"{name}={value}" for name, value in cookies.items()]
    return "; ".join(parts)


def _build_headers(context: ContextTypes.DEFAULT_TYPE) -> dict[str, str]:
    """Build request headers with the session cookie."""
    headers = dict(_REG_HEADERS)
    cookie = _session_cookie(context)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _reg_state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Get or create the per-user registration session state dict."""
    if "reg" not in context.user_data:
        context.user_data["reg"] = {}
    return context.user_data["reg"]


# ─── Entry point ───────────────────────────────────────────────────────────────

async def register_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry: /register command — start the forced registration flow."""
    # Reset any previous state
    context.user_data["reg"] = {}
    await update.effective_message.reply_text(
        "📋 *Forced Registration*\n\n"
        "I'll guide you through the registration process step by step.\n\n"
        "Step 1/9 — Enter your *NIN* (National Identification Number).\n"
        "It must be exactly *18 digits*.",
        parse_mode="Markdown",
    )
    return ASK_NIN


# ─── Step 1: NIN ──────────────────────────────────────────────────────────────

async def collect_nin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or len(text) != 18:
        await update.message.reply_text(
            "❌ Invalid NIN. It must be exactly *18 digits* (numeric only).\nPlease try again:",
            parse_mode="Markdown",
        )
        return ASK_NIN

    state = _reg_state(context)
    state["nin"] = text
    await update.message.reply_text(
        "✅ NIN recorded.\n\n"
        "Step 2/9 — Enter your *CNIBE* (ID card issue number).\n"
        "It must be exactly *9 digits*.",
        parse_mode="Markdown",
    )
    return ASK_CNIBE


# ─── Step 2: CNIBE ────────────────────────────────────────────────────────────

async def collect_cnibe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or len(text) != 9:
        await update.message.reply_text(
            "❌ Invalid CNIBE. It must be exactly *9 digits* (numeric only).\nPlease try again:",
            parse_mode="Markdown",
        )
        return ASK_CNIBE

    state = _reg_state(context)
    state["cnibe"] = text
    await update.message.reply_text(
        "✅ CNIBE recorded.\n\n"
        "Step 3/9 — Enter your *phone number*.\n"
        "It must be exactly *10 digits* and start with *0*.",
        parse_mode="Markdown",
    )
    return ASK_PHONE


# ─── Step 3: Phone ────────────────────────────────────────────────────────────

async def collect_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    if not text.isdigit() or len(text) != 10 or not text.startswith("0"):
        await update.message.reply_text(
            "❌ Invalid phone number. It must be exactly *10 digits* and start with *0*.\nPlease try again:",
            parse_mode="Markdown",
        )
        return ASK_PHONE

    state = _reg_state(context)
    state["phoneNumber"] = text

    # Now show the wilaya selection keyboard (reuse bot's cached list)
    wilayas: list[tuple[str, str]] = list(
        context.application.bot_data.get("wilayas", [])
    )
    if not wilayas:
        await update.message.reply_text(
            "⚠️ Wilaya list is not available. Please try again later with /register."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "✅ Phone number recorded.\n\n"
        "Step 4/9 — Select your *Wilaya*:",
        parse_mode="Markdown",
        reply_markup=_wilaya_keyboard(wilayas),
    )
    return ASK_WILAYA


def _wilaya_keyboard(
    wilayas: list[tuple[str, str]], *, columns: int = 2
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code, name in wilayas:
        row.append(
            InlineKeyboardButton(text=name, callback_data=f"reg_wilaya:{code}")
        )
        if len(row) >= columns:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ─── Step 4: Wilaya selected ──────────────────────────────────────────────────

async def on_wilaya_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query:
        return ASK_WILAYA
    await query.answer()

    data = query.data or ""
    if not data.startswith("reg_wilaya:"):
        return ASK_WILAYA

    wilaya_code = data.split(":", 1)[1]
    state = _reg_state(context)
    state["wilayaId"] = int(wilaya_code)

    # Look up the wilaya name for display
    wilaya_name = wilaya_code
    for code, name in context.application.bot_data.get("wilayas", []):
        if code == wilaya_code:
            wilaya_name = name
            break
    state["wilayaName"] = wilaya_name

    # Fetch communes for this wilaya
    await query.edit_message_text(f"✅ Wilaya *{wilaya_name}* selected.\n\n⏳ Fetching communes…", parse_mode="Markdown")

    try:
        communes = await _fetch_communes(context, int(wilaya_code))
    except Exception as exc:
        logger.exception("Failed to fetch communes for wilaya %s", wilaya_code)
        await query.edit_message_text(
            f"❌ Failed to fetch communes: {exc}\nPlease try again with /register."
        )
        return ConversationHandler.END

    active_communes = [c for c in communes if c.get("isActive")]
    if not active_communes:
        await query.edit_message_text(
            "⚠️ No active communes found for this wilaya. Please try another wilaya with /register."
        )
        return ConversationHandler.END

    state["_communes"] = active_communes

    keyboard = _commune_keyboard(active_communes)
    await query.edit_message_text(
        "Step 5/9 — Select your *Commune*:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    return ASK_COMMUNE


async def _fetch_communes(
    context: ContextTypes.DEFAULT_TYPE, wilaya_id: int
) -> list[dict]:
    client = _get_http_client(context)
    headers = _build_headers(context)
    url = f"/api/v1/locations/wilayas/{wilaya_id}/communes"
    resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


def _commune_keyboard(
    communes: list[dict], *, columns: int = 2
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for c in communes:
        label = f"{c['name']} ({c['code']})"
        row.append(
            InlineKeyboardButton(text=label, callback_data=f"reg_commune:{c['code']}")
        )
        if len(row) >= columns:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ─── Step 5: Commune selected ─────────────────────────────────────────────────

async def on_commune_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query:
        return ASK_COMMUNE
    await query.answer()

    data = query.data or ""
    if not data.startswith("reg_commune:"):
        return ASK_COMMUNE

    commune_code = data.split(":", 1)[1]
    state = _reg_state(context)
    state["communeCode"] = commune_code

    # Find commune name for display
    commune_name = commune_code
    for c in state.get("_communes", []):
        if c["code"] == commune_code:
            commune_name = c["name"]
            break

    await query.edit_message_text(
        f"✅ Commune *{commune_name}* selected.\n\n"
        "Step 6/9 — Enter a *password* for your adhahi.dz account:\n"
        "_(at least 6 characters)_",
        parse_mode="Markdown",
    )
    return ASK_PASSWORD


# ─── Step 6 & 7: CAPTCHA ──────────────────────────────────────────────────────

async def _generate_and_send_captcha(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    edit_message=None,
) -> int:
    state = _reg_state(context)
    try:
        captcha_data = await _fetch_captcha(context)
    except Exception as exc:
        logger.exception("Failed to generate CAPTCHA")
        msg = f"❌ Failed to generate CAPTCHA: {exc}\nPlease try again with /register."
        if edit_message:
            await edit_message.edit_text(msg)
        else:
            await update.effective_message.reply_text(msg)
        return ConversationHandler.END

    state["captchaId"] = captcha_data["captchaId"]
    state["captcha_generated_at"] = time.time()
    expires_in = captcha_data.get("expiresIn", _CAPTCHA_TTL_S)
    logger.info("CAPTCHA generated: id=%s expiresIn=%ss", state["captchaId"], expires_in)

    # Decode the base64 image
    image_data_uri: str = captcha_data["captchaImage"]
    # Strip the data URI prefix
    if "," in image_data_uri:
        b64_payload = image_data_uri.split(",", 1)[1]
    else:
        b64_payload = image_data_uri
    image_bytes = base64.b64decode(b64_payload)

    chat_id = update.effective_chat.id

    if edit_message:
        await edit_message.edit_text("Step 6/9 — Solve the CAPTCHA below:")

    # Send the CAPTCHA image
    await context.bot.send_photo(
        chat_id=chat_id,
        photo=io.BytesIO(image_bytes),
        caption=(
            "🔐 *CAPTCHA*\n\n"
            "Please type your answer below.\n"
            f"_This CAPTCHA expires in {expires_in} seconds._"
        ),
        parse_mode="Markdown",
    )
    return ASK_CAPTCHA


async def _fetch_captcha(context: ContextTypes.DEFAULT_TYPE) -> dict:
    client = _get_http_client(context)
    headers = _build_headers(context)
    resp = await client.get("/api/v1/captcha/generate", headers=headers)
    resp.raise_for_status()
    return resp.json()


async def collect_captcha_answer(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    state = _reg_state(context)

    # Check if CAPTCHA has expired
    generated_at = state.get("captcha_generated_at", 0)
    if time.time() - generated_at > _CAPTCHA_TTL_S:
        await update.message.reply_text(
            "⏰ The CAPTCHA has expired. Generating a new one…"
        )
        return await _generate_and_send_captcha(update, context)

    state["captchaAnswer"] = update.message.text.strip()

    await update.message.reply_text(
        "✅ CAPTCHA answer recorded.\n\n"
        "⏳ Submitting your registration…"
    )
    return await _submit_registration(update, context)


# ─── Step 6b: Password ────────────────────────────────────────────────────────

async def collect_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text.strip()
    errors = _validate_password(text)
    if errors:
        bullet_list = "\n".join(f"  • {e}" for e in errors)
        await update.message.reply_text(
            f"❌ Invalid password:\n{bullet_list}\n\nPlease try again:",
        )
        return ASK_PASSWORD

    state = _reg_state(context)
    state["password"] = text

    await update.message.reply_text(
        "✅ Password recorded.\n\n"
        "⏳ Generating CAPTCHA…"
    )
    return await _generate_and_send_captcha(update, context)


def _validate_password(pw: str) -> list[str]:
    """Return a list of error strings; empty means valid."""
    errors: list[str] = []
    if len(pw) < 6:
        errors.append("Must be at least 6 characters long")
    if not any(c.isdigit() for c in pw):
        errors.append("Must contain at least one digit (0-9)")
    if not any(c.islower() for c in pw):
        errors.append("Must contain at least one lowercase letter (a-z)")
    if not any(c.isupper() for c in pw):
        errors.append("Must contain at least one uppercase letter (A-Z)")
    if not any(c in "!@#$%^&*()_+-=[]{}|;':\",./<>?`~" for c in pw):
        errors.append("Must contain at least one special character")
    if any(c.isspace() for c in pw):
        errors.append("Must not contain whitespace")
    return errors


# ─── Step 8: Submit registration ──────────────────────────────────────────────

async def _submit_registration(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    state = _reg_state(context)

    body = {
        "nin": state["nin"],
        "cnibe": state["cnibe"],
        "phoneNumber": state["phoneNumber"],
        "email": "",
        "password": state["password"],
        "wilayaId": state["wilayaId"],
        "communeCode": state["communeCode"],
        "categoryId": 1,
        "paymentMethod": "CASH",
    }

    headers = _build_headers(context)
    headers["Content-Type"] = "application/json"
    headers["X-Captcha-Id"] = state["captchaId"]
    headers["X-Captcha-Answer"] = state["captchaAnswer"]

    client = _get_http_client(context)

    try:
        resp = await client.post(
            "/api/v2/citizens/register",
            json=body,
            headers=headers,
        )
    except Exception as exc:
        logger.error("Registration request failed (network error): %s", exc)
        await update.effective_message.reply_text(
            f"❌ Registration failed due to a network error:\n`{exc}`\n\n"
            "Please try again with /register.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if 200 <= resp.status_code < 300:
        logger.info(
            "Registration submitted successfully for NIN=%s: status=%s body=%s",
            state["nin"],
            resp.status_code,
            resp.text,
        )
        await update.effective_message.reply_text(
            "✅ Registration submitted!\n\n"
            "Step 7/9 — An OTP has been sent to your phone.\n"
            "Please enter the *OTP* you received:",
            parse_mode="Markdown",
        )
        return ASK_OTP

    # 425 — registration already pending OTP verification
    if resp.status_code == 425:
        logger.warning(
            "Registration already pending OTP for NIN=%s: %s",
            state["nin"],
            resp.text,
        )
        try:
            data = resp.json()
            server_msg = data.get("message", "")
        except Exception:
            server_msg = ""
        await update.effective_message.reply_text(
            "⚠️ *Registration already in progress*\n\n"
            f"{server_msg}\n\n"
            "An OTP should have been sent to your phone.\n"
            "Please enter the *OTP* you received:",
            parse_mode="Markdown",
        )
        return ASK_OTP

    # Any other error
    body_text = resp.text
    logger.error(
        "Registration failed: status=%s body=%s",
        resp.status_code,
        body_text,
    )
    error_detail = _extract_error_message(resp)
    await update.effective_message.reply_text(
        f"❌ Registration failed (HTTP {resp.status_code}).\n\n"
        f"Error: {error_detail}\n\n"
        "Please try again with /register.",
    )
    return ConversationHandler.END


def _extract_error_message(resp: httpx.Response) -> str:
    """Try to pull a readable error message from the response."""
    try:
        data = resp.json()
        for key in ("message", "error", "detail", "errors", "msg"):
            if key in data:
                val = data[key]
                if isinstance(val, list):
                    return "; ".join(str(v) for v in val)
                return str(val)
        return str(data)
    except Exception:
        return resp.text[:500] if resp.text else "No details available"


# ─── Step 9: OTP ──────────────────────────────────────────────────────────────

async def collect_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = _reg_state(context)
    otp = update.message.text.strip()
    state["otp"] = otp

    await update.message.reply_text("⏳ Verifying OTP…")

    return await _verify_otp(update, context)


async def _verify_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = _reg_state(context)

    body = {
        "nin": state["nin"],
        "otp": state["otp"],
    }

    headers = _build_headers(context)
    headers["Content-Type"] = "application/json"
    headers["X-Captcha-Id"] = state["captchaId"]
    headers["X-Captcha-Answer"] = state["captchaAnswer"]

    client = _get_http_client(context)

    try:
        resp = await client.post(
            "/api/v1/citizens/verify-otp",
            json=body,
            headers=headers,
        )
    except Exception as exc:
        logger.error("OTP verification request failed (network error): %s", exc)
        await update.effective_message.reply_text(
            f"❌ OTP verification failed due to a network error:\n`{exc}`\n\n"
            "Please try again with /register.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    if 200 <= resp.status_code < 300:
        logger.info(
            "OTP verification successful for NIN=%s: status=%s body=%s",
            state["nin"],
            resp.status_code,
            resp.text,
        )
        await update.effective_message.reply_text(
            "🎉 *Registration Complete!*\n\n"
            "Congratulations! Your registration has been verified successfully.",
            parse_mode="Markdown",
        )
    else:
        body_text = resp.text
        logger.error(
            "OTP verification failed: status=%s body=%s",
            resp.status_code,
            body_text,
        )
        error_detail = _extract_error_message(resp)
        await update.effective_message.reply_text(
            f"❌ OTP verification failed (HTTP {resp.status_code}).\n\n"
            f"Error: {error_detail}\n\n"
            "Please try again with /register.",
        )

    # Clean up session state
    context.user_data.pop("reg", None)
    return ConversationHandler.END


# ─── Cancel ────────────────────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("reg", None)
    await update.effective_message.reply_text(
        "Registration cancelled. You can start again with /register."
    )
    return ConversationHandler.END


# ─── Build the ConversationHandler ─────────────────────────────────────────────

def build_registration_handler() -> ConversationHandler:
    """Create and return the ConversationHandler for the registration flow."""
    return ConversationHandler(
        entry_points=[CommandHandler("register", register_start)],
        states={
            ASK_NIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_nin)],
            ASK_CNIBE: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_cnibe)],
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_phone)],
            ASK_WILAYA: [
                CallbackQueryHandler(on_wilaya_selected, pattern=r"^reg_wilaya:"),
            ],
            ASK_COMMUNE: [
                CallbackQueryHandler(on_commune_selected, pattern=r"^reg_commune:"),
            ],
            ASK_CAPTCHA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_captcha_answer),
            ],
            ASK_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_password),
            ],
            ASK_OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_otp),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=True,
    )
