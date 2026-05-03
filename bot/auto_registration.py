"""Auto-registration flow triggered when the scheduler detects available quota.

The bot sends a notification with a "Register Now" button.  When clicked, the
user is walked through:  CAPTCHA #1 → Submit → OTP → CAPTCHA #2 → Verify.
All profile data (NIN, CNIBE, phone, password, etc.) is pre-filled.
"""
from __future__ import annotations

import base64
import io
import logging
import time
from typing import Any

import ddddocr
import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from . import profile_db
from .registration import (
    _build_headers,
    _extract_error_message,
    _get_http_client,
)

logger = logging.getLogger(__name__)

try:
    _ocr = ddddocr.DdddOcr(beta=True, show_ad=False)
except TypeError:
    _ocr = ddddocr.DdddOcr(beta=True)

# Conversation states
(
    AUTO_CAPTCHA_1,
    AUTO_OTP,
    AUTO_CAPTCHA_2,
) = range(3)

_CAPTCHA_TTL_S = 300


def _auto_state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    if "auto_reg" not in context.user_data:
        context.user_data["auto_reg"] = {}
    return context.user_data["auto_reg"]


# ─── Scheduler trigger ────────────────────────────────────────────────────────

async def notify_profile_owners(app, profiles: list[profile_db.Profile]) -> None:
    """Called by the scheduler when quota is available.

    Groups profiles by user and sends a notification with a Register button
    for each qualifying profile (highest priority first per user).
    """
    # Group by user_id, keep only the first (highest-priority) profile per user
    seen_users: set[int] = set()
    for p in profiles:
        if p.user_id in seen_users:
            continue
        seen_users.add(p.user_id)

        try:
            await profile_db.set_profile_status(
                app.bot_data["db_path"], p.id, "registering"
            )
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    text="🚀 Register Now",
                    callback_data=f"auto_reg:{p.id}",
                )
            ]])
            masked_nin = f"{p.nin[:4]}…{p.nin[-4:]}"
            await app.bot.send_message(
                chat_id=p.user_id,
                text=(
                    f"✅ *Quota available in {p.wilaya_name}!*\n\n"
                    f"Profile `#{p.id}` ({masked_nin}) is ready to register.\n"
                    "Tap the button below to start — you'll need to solve a CAPTCHA."
                ),
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Failed to notify user %s for profile %s", p.user_id, p.id)
            # Reset status so it can be retried
            await profile_db.set_profile_status(
                app.bot_data["db_path"], p.id, "pending"
            )


# ─── Entry: user taps "Register Now" ──────────────────────────────────────────

async def auto_reg_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    profile_id = int((query.data or "").split(":", 1)[1])
    db_path: str = context.application.bot_data["db_path"]
    user_id = update.effective_user.id

    profile = await profile_db.get_profile(db_path, profile_id, user_id)
    if not profile:
        await query.edit_message_text("❌ Profile not found.")
        return ConversationHandler.END

    if profile.status not in ("registering", "pending"):
        await query.edit_message_text(
            f"⚠️ Profile #{profile_id} status is *{profile.status}* — cannot register.",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    # Store profile data in session
    state = _auto_state(context)
    state.clear()
    state["profile_id"] = profile.id
    state["name"] = profile.name
    state["nin"] = profile.nin
    state["cnibe"] = profile.cnibe
    state["phoneNumber"] = profile.phone
    state["password"] = profile.password
    state["wilayaId"] = profile.wilaya_id
    state["communeCode"] = profile.commune_code
    state["email"] = profile.email

    await query.edit_message_text("⏳ Attempting automatic registration (solving CAPTCHA)...")

    client = _get_http_client(context)
    headers = _build_headers(context)
    headers["Content-Type"] = "application/json"

    for attempt in range(1, 4):
        captcha_data = await _fetch_and_solve_captcha(context)
        if not captcha_data:
            continue
            
        captcha_id, solved_text, _ = captcha_data
        
        body = {
            "nin": state["nin"],
            "cnibe": state["cnibe"],
            "phoneNumber": state["phoneNumber"],
            "email": state.get("email", ""),
            "password": state["password"],
            "wilayaId": state["wilayaId"],
            "communeCode": state["communeCode"],
            "categoryId": 1,
            "paymentMethod": "CASH",
        }
        
        req_headers = headers.copy()
        req_headers["X-Captcha-Id"] = captcha_id
        req_headers["X-Captcha-Answer"] = solved_text
        
        try:
            resp = await client.post("/api/v2/citizens/register", json=body, headers=req_headers)
            
            if 200 <= resp.status_code < 300 or resp.status_code == 425:
                if resp.status_code == 425:
                    try: msg = resp.json().get("message", "")
                    except: msg = ""
                    await query.edit_message_text(
                        f"✅ *Auto-CAPTCHA solved!*\n\n⚠️ *Already pending*\n{msg}\n\n"
                        f"Profile: *{state.get('name', 'Unknown')}*\n"
                        f"Phone: `{state['phoneNumber']}`\n\n"
                        "Please enter the *OTP* you received:",
                        parse_mode="Markdown",
                    )
                else:
                    await query.edit_message_text(
                        "✅ *Auto-CAPTCHA solved!* Registration submitted!\n\n"
                        f"Profile: *{state.get('name', 'Unknown')}*\n"
                        f"Phone: `{state['phoneNumber']}`\n\n"
                        "An OTP has been sent to your phone.\n"
                        "Please enter the *OTP*:",
                        parse_mode="Markdown",
                    )
                return AUTO_OTP
            
            error_detail = _extract_error_message(resp)
            logger.warning("Auto-registration attempt %d failed: %s (HTTP %s)", attempt, error_detail, resp.status_code)
        except Exception as exc:
            logger.error("Auto-registration network error: %s", exc)

    # If we fall through, 3 attempts failed.
    await update.effective_chat.send_message("⚠️ Auto-CAPTCHA failed 3 times. Falling back to manual entry.")
    return await _generate_captcha(update, context, captcha_key="captcha1")


async def _fetch_and_solve_captcha(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, str, bytes] | None:
    client = _get_http_client(context)
    headers = _build_headers(context)
    try:
        resp = await client.get("/api/v1/captcha/generate", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        captcha_id = data["captchaId"]
        image_uri = data["captchaImage"]
        b64 = image_uri.split(",", 1)[1] if "," in image_uri else image_uri
        image_bytes = base64.b64decode(b64)
        
        solved_text = _ocr.classification(image_bytes).upper()
        return captcha_id, solved_text, image_bytes
    except Exception as exc:
        logger.error("Failed to fetch/solve captcha: %s", exc)
        return None


# ─── CAPTCHA generation helper ────────────────────────────────────────────────

async def _generate_captcha(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    captcha_key: str,
) -> int:
    state = _auto_state(context)
    client = _get_http_client(context)
    headers = _build_headers(context)

    try:
        resp = await client.get("/api/v1/captcha/generate", headers=headers)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.exception("Failed to generate CAPTCHA")
        await update.effective_chat.send_message(
            f"❌ CAPTCHA generation failed: {exc}\nUse /register to try manually."
        )
        await _reset_profile_status(context, "pending")
        return ConversationHandler.END

    state[f"{captcha_key}_id"] = data["captchaId"]
    state[f"{captcha_key}_ts"] = time.time()
    expires_in = data.get("expiresIn", _CAPTCHA_TTL_S)
    logger.info("CAPTCHA generated (%s): id=%s expiresIn=%ss", captcha_key, data["captchaId"], expires_in)

    # Decode image
    image_uri: str = data["captchaImage"]
    b64 = image_uri.split(",", 1)[1] if "," in image_uri else image_uri
    image_bytes = base64.b64decode(b64)

    step = "1" if captcha_key == "captcha1" else "3"
    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=io.BytesIO(image_bytes),
        caption=(
            f"🔐 *CAPTCHA (step {step})*\n\n"
            "Type your answer below.\n"
            f"_Expires in {expires_in} seconds._"
        ),
        parse_mode="Markdown",
    )

    if captcha_key == "captcha1":
        return AUTO_CAPTCHA_1
    return AUTO_CAPTCHA_2


# ─── Step 1: Collect CAPTCHA #1 → Submit registration ─────────────────────────

async def collect_captcha_1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = _auto_state(context)

    # Check expiry
    if time.time() - state.get("captcha1_ts", 0) > _CAPTCHA_TTL_S:
        await update.message.reply_text("⏰ CAPTCHA expired. Generating a new one…")
        return await _generate_captcha(update, context, captcha_key="captcha1")

    state["captcha1_answer"] = update.message.text.strip()
    await update.message.reply_text("⏳ Submitting registration…")

    # Submit registration
    body = {
        "nin": state["nin"],
        "cnibe": state["cnibe"],
        "phoneNumber": state["phoneNumber"],
        "email": state.get("email", ""),
        "password": state["password"],
        "wilayaId": state["wilayaId"],
        "communeCode": state["communeCode"],
        "categoryId": 1,
        "paymentMethod": "CASH",
    }

    headers = _build_headers(context)
    headers["Content-Type"] = "application/json"
    headers["X-Captcha-Id"] = state["captcha1_id"]
    headers["X-Captcha-Answer"] = state["captcha1_answer"]

    client = _get_http_client(context)

    try:
        resp = await client.post("/api/v2/citizens/register", json=body, headers=headers)
    except Exception as exc:
        logger.error("Auto-registration network error: %s", exc)
        await update.message.reply_text(f"❌ Network error: `{exc}`", parse_mode="Markdown")
        await _reset_profile_status(context, "failed")
        return ConversationHandler.END

    logger.info("Auto-registration response: status=%s body=%s", resp.status_code, resp.text)

    if 200 <= resp.status_code < 300 or resp.status_code == 425:
        if resp.status_code == 425:
            try:
                msg = resp.json().get("message", "")
            except Exception:
                msg = ""
            await update.message.reply_text(
                f"⚠️ *Already pending*\n{msg}\n\n"
                f"Profile: *{state.get('name', 'Unknown')}*\n"
                f"Phone: `{state['phoneNumber']}`\n\n"
                "Please enter the *OTP* you received:",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "✅ Registration submitted!\n\n"
                f"Profile: *{state.get('name', 'Unknown')}*\n"
                f"Phone: `{state['phoneNumber']}`\n\n"
                "An OTP has been sent to your phone.\n"
                "Please enter the *OTP*:",
                parse_mode="Markdown",
            )
        return AUTO_OTP

    # Error
    error_detail = _extract_error_message(resp)
    logger.error("Auto-registration failed: status=%s body=%s", resp.status_code, resp.text)
    await update.message.reply_text(
        f"❌ Registration failed (HTTP {resp.status_code}).\n\nError: {error_detail}"
    )
    await _reset_profile_status(context, "failed")
    return ConversationHandler.END


# ─── Step 2: Collect OTP → Generate CAPTCHA #2 ────────────────────────────────

async def collect_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = _auto_state(context)
    state["otp"] = update.message.text.strip()

    await update.message.reply_text(
        "✅ OTP recorded.\n\n⏳ Attempting to verify automatically (solving CAPTCHA)..."
    )
    
    client = _get_http_client(context)
    headers = _build_headers(context)
    headers["Content-Type"] = "application/json"
    
    for attempt in range(1, 4):
        captcha_data = await _fetch_and_solve_captcha(context)
        if not captcha_data:
            continue
            
        captcha_id, solved_text, _ = captcha_data
        
        body = {
            "nin": state["nin"],
            "otp": state["otp"],
        }
        
        req_headers = headers.copy()
        req_headers["X-Captcha-Id"] = captcha_id
        req_headers["X-Captcha-Answer"] = solved_text
        
        try:
            resp = await client.post("/api/v1/citizens/verify-otp", json=body, headers=req_headers)
            
            if 200 <= resp.status_code < 300:
                db_path: str = context.application.bot_data["db_path"]
                profile_id = state.get("profile_id")
                if profile_id:
                    await profile_db.set_profile_status(db_path, profile_id, "registered")
                await update.message.reply_text(
                    "🎉 *Registration Complete!*\n\n"
                    "Congratulations — your registration has been verified!",
                    parse_mode="Markdown",
                )
                context.user_data.pop("auto_reg", None)
                return ConversationHandler.END
            
            error_detail = _extract_error_message(resp)
            logger.warning("OTP verification attempt %d failed: %s", attempt, error_detail)
        except Exception as exc:
            logger.error("OTP verification network error: %s", exc)

    await update.message.reply_text("⚠️ Auto-CAPTCHA failed 3 times. Falling back to manual entry.")
    return await _generate_captcha(update, context, captcha_key="captcha2")


# ─── Step 3: Collect CAPTCHA #2 → Submit OTP verification ─────────────────────

async def collect_captcha_2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = _auto_state(context)

    # Check expiry
    if time.time() - state.get("captcha2_ts", 0) > _CAPTCHA_TTL_S:
        await update.message.reply_text("⏰ CAPTCHA expired. Generating a new one…")
        return await _generate_captcha(update, context, captcha_key="captcha2")

    state["captcha2_answer"] = update.message.text.strip()
    await update.message.reply_text("⏳ Verifying OTP…")

    body = {
        "nin": state["nin"],
        "otp": state["otp"],
    }

    headers = _build_headers(context)
    headers["Content-Type"] = "application/json"
    headers["X-Captcha-Id"] = state["captcha2_id"]
    headers["X-Captcha-Answer"] = state["captcha2_answer"]

    client = _get_http_client(context)

    try:
        resp = await client.post("/api/v1/citizens/verify-otp", json=body, headers=headers)
    except Exception as exc:
        logger.error("OTP verification network error: %s", exc)
        await update.message.reply_text(f"❌ Network error: `{exc}`", parse_mode="Markdown")
        await _reset_profile_status(context, "failed")
        return ConversationHandler.END

    logger.info("OTP verification response: status=%s body=%s", resp.status_code, resp.text)

    db_path: str = context.application.bot_data["db_path"]
    profile_id = state.get("profile_id")

    if 200 <= resp.status_code < 300:
        if profile_id:
            await profile_db.set_profile_status(db_path, profile_id, "registered")
        await update.message.reply_text(
            "🎉 *Registration Complete!*\n\n"
            "Congratulations — your registration has been verified!",
            parse_mode="Markdown",
        )
    else:
        error_detail = _extract_error_message(resp)
        logger.error("OTP verification failed: status=%s body=%s", resp.status_code, resp.text)
        if profile_id:
            await profile_db.set_profile_status(db_path, profile_id, "failed")
        await update.message.reply_text(
            f"❌ OTP verification failed (HTTP {resp.status_code}).\n\nError: {error_detail}"
        )

    # Clean up
    context.user_data.pop("auto_reg", None)
    return ConversationHandler.END


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _reset_profile_status(context: ContextTypes.DEFAULT_TYPE, status: str) -> None:
    state = context.user_data.get("auto_reg", {})
    profile_id = state.get("profile_id")
    if profile_id:
        db_path: str = context.application.bot_data["db_path"]
        await profile_db.set_profile_status(db_path, profile_id, status)
    context.user_data.pop("auto_reg", None)


async def auto_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reset_profile_status(context, "pending")
    await update.effective_message.reply_text(
        "Auto-registration cancelled. Profile reset to pending."
    )
    return ConversationHandler.END


# ─── Build the ConversationHandler ─────────────────────────────────────────────

def build_auto_registration_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[
            CallbackQueryHandler(auto_reg_start, pattern=r"^auto_reg:"),
        ],
        states={
            AUTO_CAPTCHA_1: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_captcha_1),
            ],
            AUTO_OTP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_otp),
            ],
            AUTO_CAPTCHA_2: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, collect_captcha_2),
            ],
        },
        fallbacks=[],
        per_user=True,
        per_chat=True,
    )
