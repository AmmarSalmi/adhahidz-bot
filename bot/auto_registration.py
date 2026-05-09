"""Auto-registration flow triggered when the scheduler detects available quota.

Phase 1: Try all profiles concurrently (auto-CAPTCHA + submit).
Phase 2: If IP-blocked or CAPTCHA fails, fall back to sequential + manual CAPTCHA.

Successful submission secures a spot — OTP verification can be done later
via /verifyotp.
"""
from __future__ import annotations

import asyncio
import base64
import json
import io
import logging
import time
from typing import Any

import httpx
from telegram import Update
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from . import profile_db
from .captcha_solver import CaptchaSolver, create_solvers
from .registration import (
    _build_headers,
    _close_http_client,
    _extract_error_message,
    _get_http_client,
)

logger = logging.getLogger(__name__)

_primary_solver, _fallback_solver = create_solvers()

# Conversation states for manual CAPTCHA fallback & OTP verification
(
    MANUAL_CAPTCHA,
    VERIFY_OTP_CAPTCHA,
) = range(2)

_CAPTCHA_TTL_S = 300

# HTTP codes that signal IP/rate-limit blocking
_IP_BLOCK_CODES = {429, 403}


# ─── CAPTCHA helpers ──────────────────────────────────────────────────────────

async def _fetch_and_solve_captcha(
    client: httpx.AsyncClient, headers: dict[str, str],
    solver: CaptchaSolver | None = None,
) -> tuple[str, str, bytes] | None:
    """Fetch a CAPTCHA and solve it. Returns (id, answer, image_bytes) or None."""
    used_solver = solver or _primary_solver
    try:
        resp = await client.get("/api/v1/captcha/generate", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        captcha_id = data["captchaId"]
        image_uri = data["captchaImage"]
        b64 = image_uri.split(",", 1)[1] if "," in image_uri else image_uri
        image_bytes = base64.b64decode(b64)
        solved_text = await used_solver.solve(image_bytes)
        logger.info("CAPTCHA solved by %s: id=%s answer=%s", used_solver.name, captcha_id, solved_text)
        return captcha_id, solved_text, image_bytes
    except Exception as exc:
        logger.error("Failed to fetch/solve captcha (%s): %s", used_solver.name, exc)
        return None


async def _fetch_captcha_raw(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> tuple[str, bytes] | None:
    """Fetch a CAPTCHA without solving. Returns (id, image_bytes) or None."""
    try:
        resp = await client.get("/api/v1/captcha/generate", headers=headers)
        resp.raise_for_status()
        data = resp.json()
        captcha_id = data["captchaId"]
        image_uri = data["captchaImage"]
        b64 = image_uri.split(",", 1)[1] if "," in image_uri else image_uri
        image_bytes = base64.b64decode(b64)
        return captcha_id, image_bytes
    except Exception as exc:
        logger.error("Failed to fetch captcha: %s", exc)
        return None


# ─── Single-profile submission (auto-CAPTCHA, up to 3 attempts) ───────────────

async def _try_submit_profile(
    profile: profile_db.Profile,
    client: httpx.AsyncClient,
    base_headers: dict[str, str],
) -> tuple[profile_db.Profile, str, str]:
    """Attempt to submit one profile with cascading CAPTCHA solvers.

    Strategy:
      Attempt 1: ddddocr  (free, ~0.05s)
      Attempt 2: 2captcha (paid, ~6s) — different error profile
      Attempt 3: 2captcha (paid, ~6s) — last resort

    Returns (profile, outcome, detail) where outcome is one of:
      "submitted"    — 2xx or 425 (spot secured)
      "captcha_fail" — all attempts exhausted
      "ip_blocked"   — server returned 429/403
      "error"        — other server error
    """
    body = {
        "nin": profile.nin,
        "cnibe": profile.cnibe,
        "phoneNumber": profile.phone,
        "email": profile.email or "",
        "password": profile.password,
        "wilayaId": profile.wilaya_id,
        "communeCode": profile.commune_code,
        "categoryId": 1,
        "paymentMethod": profile.payment_method,
    }

    # Build attempt list: ddddocr first, then 2captcha fallback
    solvers_to_try = [_primary_solver]
    if _fallback_solver:
        solvers_to_try.append(_fallback_solver)
        solvers_to_try.append(_fallback_solver)
    else:
        # No fallback — just retry with primary
        solvers_to_try.append(_primary_solver)
        solvers_to_try.append(_primary_solver)

    for attempt, solver in enumerate(solvers_to_try, 1):
        solved = await _fetch_and_solve_captcha(client, base_headers, solver=solver)
        if not solved:
            continue

        captcha_id, answer, _ = solved
        req_headers = {**base_headers, "X-Captcha-Id": captcha_id, "X-Captcha-Answer": answer}

        try:
            resp = await client.post("/api/v2/citizens/register", json=body, headers=req_headers)
        except Exception as exc:
            logger.error("Submit network error profile=%s: %s", profile.id, exc)
            return profile, "error", str(exc)

        logger.info(
            "Submit profile=%s attempt=%d solver=%s status=%s body=%s",
            profile.id, attempt, solver.name, resp.status_code, resp.text,
        )

        if 200 <= resp.status_code < 300 or resp.status_code == 425:
            return profile, "submitted", resp.text

        if resp.status_code in _IP_BLOCK_CODES:
            return profile, "ip_blocked", _extract_error_message(resp)

        error_msg = _extract_error_message(resp)

        # Parse JSON to accurately check for the specific CAPTCHA Validation Error
        is_captcha_error = False
        try:
            data = resp.json()
            if data.get("error") == "CAPTCHA Validation Error" or "captcha" in data.get("message", "").lower():
                is_captcha_error = True
        except Exception:
            pass

        if not is_captcha_error and "captcha" not in resp.text.lower():
            logger.warning(
                "Profile %s rejected by server (not CAPTCHA): %s",
                profile.id, error_msg,
            )
            return profile, "error", error_msg

        # Rejected for CAPTCHA — cascade to next solver
        logger.warning(
            "Profile %s attempt %d (%s) rejected for CAPTCHA: %s",
            profile.id, attempt, solver.name, error_msg,
        )

    return profile, "captcha_fail", "All CAPTCHA attempts exhausted"


# ─── Login + Order for registered profiles ────────────────────────────────────

async def _try_login_and_order(
    profile: profile_db.Profile,
    client: httpx.AsyncClient,
    base_headers: dict[str, str],
) -> tuple[profile_db.Profile, str, str]:
    """Login with a registered profile and submit an order.

    Strategy: ddddocr first (free), then 2captcha fallback (paid).
    Returns (profile, outcome, detail) where outcome is one of:
      "ordered"      — order created successfully
      "captcha_fail" — all CAPTCHA attempts exhausted
      "login_fail"   — login failed after all attempts
      "order_fail"   — logged in but order creation failed
      "error"        — network or unexpected error
    """
    # Build solver list: ddddocr first, then 2captcha fallback
    solvers_to_try: list[CaptchaSolver] = [_primary_solver]
    if _fallback_solver:
        solvers_to_try.append(_fallback_solver)
        solvers_to_try.append(_fallback_solver)
    else:
        solvers_to_try.append(_primary_solver)
        solvers_to_try.append(_primary_solver)

    # ── Step 1: Login ──
    access_token = None
    last_login_error = ""

    for attempt, solver in enumerate(solvers_to_try, 1):
        solved = await _fetch_and_solve_captcha(client, base_headers, solver=solver)
        if not solved:
            continue

        captcha_id, answer, _ = solved
        login_headers = {
            **base_headers,
            "X-Captcha-Id": captcha_id,
            "X-Captcha-Answer": answer,
        }
        login_body = {
            "nin": profile.nin,
            "password": profile.password,
            "deviceInfo": "WEB_APP",
            "sessionType": "WEB",
        }

        try:
            resp = await client.post("/api/v1/citizens/login", json=login_body, headers=login_headers)
        except Exception as exc:
            logger.error("Login network error profile=%s: %s", profile.id, exc)
            return profile, "error", str(exc)

        logger.info(
            "Login profile=%s attempt=%d solver=%s status=%s body=%s",
            profile.id, attempt, solver.name, resp.status_code, resp.text,
        )

        if 200 <= resp.status_code < 300:
            access_token = resp.json().get("token")
            if access_token:
                break
            last_login_error = "No token in response"
        else:
            last_login_error = _extract_error_message(resp)
            
            is_captcha_error = False
            try:
                data = resp.json()
                if data.get("error") == "CAPTCHA Validation Error" or "captcha" in data.get("message", "").lower():
                    is_captcha_error = True
            except Exception:
                pass

            if not is_captcha_error and "captcha" not in resp.text.lower():
                logger.warning(
                    "Login profile %s failed (not CAPTCHA): %s",
                    profile.id, last_login_error,
                )
                break  # Don't retry CAPTCHA, it's a real login failure (e.g., bad password)
            logger.warning(
                "Login profile %s attempt %d (%s) failed CAPTCHA: %s",
                profile.id, attempt, solver.name, last_login_error,
            )

    if not access_token:
        return profile, "login_fail", f"Login failed: {last_login_error}"

    # ── Step 2: Submit order ──
    order_headers = {
        **base_headers,
        "Authorization": f"Bearer {access_token}",
        "Referer": "https://adhahi.dz/activation",
        "Origin": "https://adhahi.dz",
    }
    order_body = {
        "wilayaId": profile.wilaya_id,
        "communeCode": profile.commune_code,
        "categoryId": 1,
        "paymentMethod": profile.payment_method,
    }

    try:
        resp = await client.post("/api/v1/orders", json=order_body, headers=order_headers)
    except Exception as exc:
        logger.error("Order network error profile=%s: %s", profile.id, exc)
        return profile, "error", str(exc)

    logger.info(
        "Order profile=%s status=%s body=%s",
        profile.id, resp.status_code, resp.text,
    )

    if 200 <= resp.status_code < 300:
        return profile, "ordered", resp.text

    return profile, "order_fail", _extract_error_message(resp)


# ─── Pre-registered reminder (called by 12h scheduler) ────────────────────────

async def remind_preregistered_profiles(app) -> None:
    """Send reminders to users with pre-registered profiles to verify OTP."""
    db_path: str = app.bot_data.get("db_path", "")
    if not db_path:
        return

    profiles = await profile_db.get_all_profiles_by_status(db_path, "pre-registered")
    if not profiles:
        return

    # Group by user
    user_profiles: dict[int, list[profile_db.Profile]] = {}
    for p in profiles:
        user_profiles.setdefault(p.user_id, []).append(p)

    for user_id, profs in user_profiles.items():
        lines = ["🔔 *OTP Verification Reminder*\n"]
        for p in profs:
            masked = f"{p.nin[:4]}…{p.nin[-4:]}"
            lines.append(f"  • *{p.name or masked}* (`{p.phone}`)")
        lines.append(
            "\nThese profiles are pre-registered but not yet verified.\n"
            "Use /verifyotp to complete verification so they're ready "
            "to snatch an order when quota opens!"
        )
        try:
            await app.bot.send_message(
                chat_id=user_id,
                text="\n".join(lines),
                parse_mode="Markdown",
            )
            logger.info("Sent pre-registered reminder to user %s (%d profiles)", user_id, len(profs))
        except Exception:
            logger.exception("Failed to send reminder to user %s", user_id)


# ─── Scheduler entry point ────────────────────────────────────────────────────

async def auto_submit_profiles(app, profiles: list[profile_db.Profile]) -> None:
    """Called by the scheduler when quota is available.

    Routes each profile based on status:
      - pending     → registration flow (captcha + submit + OTP request)
      - registered  → login + order flow
      - pre-registered / ordered → skipped (handled separately)
    """
    db_path: str = app.bot_data["db_path"]
    api_client = app.bot_data.get("api_client")
    if not api_client:
        return

    # Create an isolated client for this batch — prevents cookie/session
    # bleed from login responses leaking into the shared quota-polling client.
    client: httpx.AsyncClient = api_client.create_session()
    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
        "Accept": "application/json",
        "Referer": "https://adhahi.dz/register",
        "Content-Type": "application/json",
    }

    try:
        # Split by status
        pending_profiles = [p for p in profiles if p.status == "pending"]
        registered_profiles = [p for p in profiles if p.status == "registered"]
        preregistered_profiles = [p for p in profiles if p.status == "pre-registered"]

        # ── Handle pending profiles (existing registration flow) ──
        if pending_profiles:
            user_pending: dict[int, list[profile_db.Profile]] = {}
            for p in pending_profiles:
                user_pending.setdefault(p.user_id, []).append(p)

            for user_id, user_profs in user_pending.items():
                for p in user_profs:
                    await profile_db.set_profile_status(db_path, p.id, "registering")
                try:
                    await _process_user_profiles(app, user_id, user_profs, client, base_headers, db_path)
                except Exception:
                    logger.exception("Auto-registration failed for user %s", user_id)
                    for p in user_profs:
                        await profile_db.set_profile_status(db_path, p.id, "pending")

        # ── Handle registered profiles (login + order flow) ──
        if registered_profiles:
            await _process_registered_profiles(app, registered_profiles, client, base_headers, db_path)

        # ── Handle pre-registered profiles (resend OTP + urgent notify, non-blocking) ──
        if preregistered_profiles:
            await _nudge_preregistered_profiles(app, preregistered_profiles, client, base_headers)
    finally:
        await client.aclose()


async def _nudge_preregistered_profiles(
    app,
    profiles: list[profile_db.Profile],
    client: httpx.AsyncClient,
    base_headers: dict[str, str],
) -> None:
    """Resend OTP for pre-registered profiles and urgently notify users.

    This is fire-and-forget — we resend the OTP and tell the user to verify,
    but we never block waiting for their response.
    """
    for profile in profiles:
        masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
        user_id = profile.user_id

        # Try to resend OTP
        headers = {**base_headers, "Referer": "https://adhahi.dz/activation", "Origin": "https://adhahi.dz"}
        otp_sent = False
        try:
            resp = await client.post(
                "/api/v1/citizens/resend-otp",
                json={"nin": profile.nin},
                headers=headers,
            )
            if 200 <= resp.status_code < 300:
                otp_sent = True
                logger.info("OTP resent for pre-registered profile %s", profile.id)
            else:
                logger.warning(
                    "OTP resend failed for profile %s: HTTP %s %s",
                    profile.id, resp.status_code, resp.text,
                )
        except Exception:
            logger.exception("OTP resend network error for profile %s", profile.id)

        # Notify user urgently — don't wait for response
        try:
            if otp_sent:
                await app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🚨 *QUOTA IS OPEN — Verify NOW!*\n\n"
                        f"Profile: *{profile.name or masked}*\n"
                        f"Phone: `{profile.phone}`\n\n"
                        "An OTP has been resent to your phone.\n"
                        "Use /verifyotp *immediately* to complete verification "
                        "and place your order before quota runs out!"
                    ),
                    parse_mode="Markdown",
                )
            else:
                await app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🚨 *QUOTA IS OPEN — Verify NOW!*\n\n"
                        f"Profile: *{profile.name or masked}*\n"
                        f"Phone: `{profile.phone}`\n\n"
                        "⚠️ Failed to resend OTP. Try /verifyotp and type `resend` "
                        "to request a new OTP manually."
                    ),
                    parse_mode="Markdown",
                )
        except Exception:
            logger.exception("Failed to send urgent nudge to user %s", user_id)


async def _process_registered_profiles(
    app,
    profiles: list[profile_db.Profile],
    client: httpx.AsyncClient,
    base_headers: dict[str, str],
    db_path: str,
) -> None:
    """Handle registered profiles: login + submit order concurrently.

    Concurrent processing ensures all profiles attempt to secure quota simultaneously.
    """
    results = await asyncio.gather(
        *[_try_login_and_order(p, client, base_headers) for p in profiles],
        return_exceptions=True
    )

    for result in results:
        if isinstance(result, Exception):
            logger.error("Unexpected error in login+order: %s", result)
            continue

        profile, outcome, detail = result
        masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
        user_id = profile.user_id

        if outcome == "ordered":
            await profile_db.set_profile_status(db_path, profile.id, "ordered")
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    f"🐑 *Order placed!*\n\n"
                    f"Profile: *{profile.name or masked}*\n"
                    f"Phone: `{profile.phone}`\n\n"
                    "Your order has been submitted successfully!"
                ),
                parse_mode="Markdown",
            )
        elif outcome == "order_fail":
            logger.error("Order failed for profile %s: %s", profile.id, detail)
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    f"❌ *Order failed for {profile.name or masked}*\n\n"
                    f"Error: {detail}\n\n"
                    "The profile is still registered. Will retry next time quota is available."
                ),
                parse_mode="Markdown",
            )
        elif outcome == "login_fail":
            logger.error("Login failed for profile %s: %s", profile.id, detail)
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    f"❌ *Login failed for {profile.name or masked}*\n\n"
                    f"Error: {detail}\n\n"
                    "Will retry next time quota is available."
                ),
                parse_mode="Markdown",
            )
        else:
            logger.error("Login+order error for profile %s: %s", profile.id, detail)
            await app.bot.send_message(
                chat_id=user_id,
                text=f"❌ Error for profile *{profile.name or masked}*:\n{detail}",
                parse_mode="Markdown",
            )


async def _process_user_profiles(
    app,
    user_id: int,
    profiles: list[profile_db.Profile],
    client: httpx.AsyncClient,
    base_headers: dict[str, str],
    db_path: str,
) -> None:
    """Phase 1: concurrent auto-CAPTCHA. Phase 2: sequential manual fallback.

    Concurrent processing ensures all profiles attempt to secure quota simultaneously.
    """

    # ── Phase 1: Concurrent auto-CAPTCHA ──
    results = await asyncio.gather(
        *[_try_submit_profile(p, client, base_headers) for p in profiles],
        return_exceptions=True
    )

    remaining: list[profile_db.Profile] = []
    ip_blocked = False

    for result in results:
        if isinstance(result, Exception):
            logger.error("Unexpected error in concurrent submit: %s", result)
            continue

        profile, outcome, detail = result

        if outcome == "submitted":
            await profile_db.set_profile_status(db_path, profile.id, "submitted")
            masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    f"✅ *Registration submitted!*\n\n"
                    f"Profile: *{profile.name or masked}*\n"
                    f"Phone: `{profile.phone}`\n\n"
                    "Your spot is secured! Use /verifyotp to complete OTP verification when ready."
                ),
                parse_mode="Markdown",
            )
        elif outcome == "ip_blocked":
            ip_blocked = True
            remaining.append(profile)
        elif outcome == "captcha_fail":
            remaining.append(profile)
        else:
            await profile_db.set_profile_status(db_path, profile.id, "failed")
            await app.bot.send_message(
                chat_id=user_id,
                text=f"❌ Registration failed for profile *{profile.name}*:\n{detail}",
                parse_mode="Markdown",
            )

    if not remaining:
        return

    # ── Phase 2: Sequential fallback ──
    if ip_blocked:
        logger.info("IP block detected for user %s — switching to sequential", user_id)

    # Sort remaining by priority
    remaining.sort(key=lambda p: p.priority)

    for profile in remaining:
        p_result = await _try_submit_profile(profile, client, base_headers)
        _, outcome, detail = p_result

        if outcome == "submitted":
            await profile_db.set_profile_status(db_path, profile.id, "submitted")
            masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
            await app.bot.send_message(
                chat_id=user_id,
                text=(
                    f"✅ *Registration submitted!*\n\n"
                    f"Profile: *{profile.name or masked}*\n"
                    f"Phone: `{profile.phone}`\n\n"
                    "Your spot is secured! Use /verifyotp to complete OTP verification when ready."
                ),
                parse_mode="Markdown",
            )
        elif outcome == "captcha_fail":
            # Queue manual CAPTCHA for user
            await _send_manual_captcha(app, user_id, profile, client, base_headers, db_path)
        else:
            await profile_db.set_profile_status(db_path, profile.id, "failed")
            await app.bot.send_message(
                chat_id=user_id,
                text=f"❌ Registration failed for profile *{profile.name}*:\n{detail}",
                parse_mode="Markdown",
            )


async def _send_manual_captcha(
    app, user_id: int, profile: profile_db.Profile,
    client: httpx.AsyncClient, base_headers: dict[str, str], db_path: str,
) -> None:
    """Send a CAPTCHA image to the user for manual solving.

    Stores the pending profile + captcha info in bot_data so the
    ConversationHandler can pick it up.
    """
    captcha = await _fetch_captcha_raw(client, base_headers)
    if not captcha:
        await profile_db.set_profile_status(db_path, profile.id, "pending")
        await app.bot.send_message(
            chat_id=user_id,
            text=f"⚠️ Could not generate CAPTCHA for profile *{profile.name}*. Will retry next cycle.",
            parse_mode="Markdown",
        )
        return

    captcha_id, image_bytes = captcha

    # Store pending manual captcha keyed by user_id and message_id
    masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
    msg = await app.bot.send_photo(
        chat_id=user_id,
        photo=io.BytesIO(image_bytes),
        caption=(
            f"🔐 *Manual CAPTCHA needed*\n\n"
            f"Profile: *{profile.name or masked}*\n"
            f"Phone: `{profile.phone}`\n\n"
            "Auto-solve failed. **Reply to this message** with the CAPTCHA answer.\n"
            f"_Expires in {_CAPTCHA_TTL_S} seconds._"
        ),
        parse_mode="Markdown",
    )

    pending_dict = app.bot_data.setdefault(f"manual_captchas_{user_id}", {})
    pending_dict[msg.message_id] = {
        "profile": profile,
        "captcha_id": captcha_id,
        "captcha_ts": time.time(),
    }


# ─── Manual CAPTCHA conversation handler ──────────────────────────────────────

async def manual_captcha_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Triggered when user replies to a manual CAPTCHA message."""
    if not update.message or not update.message.reply_to_message:
        return

    user_id = update.effective_user.id
    reply_msg_id = update.message.reply_to_message.message_id
    
    pending_dict = context.application.bot_data.get(f"manual_captchas_{user_id}", {})
    pending = pending_dict.get(reply_msg_id)

    if not pending:
        # Not a reply to an active manual captcha
        return

    profile: profile_db.Profile = pending["profile"]
    captcha_id: str = pending["captcha_id"]
    captcha_ts: float = pending["captcha_ts"]
    db_path: str = context.application.bot_data["db_path"]

    # Check expiry
    if time.time() - captcha_ts > _CAPTCHA_TTL_S:
        # Generate a new one
        client = _get_http_client(context)
        headers = _build_headers(context)
        new_captcha = await _fetch_captcha_raw(client, headers)
        if not new_captcha:
            await update.message.reply_text("❌ Failed to regenerate CAPTCHA. Will retry next cycle.")
            await profile_db.set_profile_status(db_path, profile.id, "pending")
            pending_dict.pop(reply_msg_id, None)
            return

        new_id, new_bytes = new_captcha

        await update.message.reply_text("⏰ Previous CAPTCHA expired. Here's a new one:")
        masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
        new_msg = await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=io.BytesIO(new_bytes),
            caption=(
                f"🔐 *CAPTCHA* for *{profile.name or masked}*\n"
                "**Reply to this message** with your answer."
            ),
            parse_mode="Markdown",
        )
        
        pending_dict.pop(reply_msg_id, None)
        pending_dict[new_msg.message_id] = {
            "profile": profile,
            "captcha_id": new_id,
            "captcha_ts": time.time(),
        }
        return

    answer = update.message.text.strip()
    await update.message.reply_text(f"⏳ Submitting registration for {profile.name or profile.nin[:4]+'…'}…")

    # Submit
    body = {
        "nin": profile.nin,
        "cnibe": profile.cnibe,
        "phoneNumber": profile.phone,
        "email": profile.email or "",
        "password": profile.password,
        "wilayaId": profile.wilaya_id,
        "communeCode": profile.commune_code,
        "categoryId": 1,
        "paymentMethod": profile.payment_method,
    }

    client = _get_http_client(context)
    headers = _build_headers(context)
    headers["Content-Type"] = "application/json"
    headers["X-Captcha-Id"] = captcha_id
    headers["X-Captcha-Answer"] = answer

    try:
        resp = await client.post("/api/v2/citizens/register", json=body, headers=headers)
    except Exception as exc:
        logger.error("Manual submit network error: %s", exc)
        await update.message.reply_text(f"❌ Network error: `{exc}`", parse_mode="Markdown")
        await profile_db.set_profile_status(db_path, profile.id, "pending")
        pending_dict.pop(reply_msg_id, None)
        return

    logger.info("Manual submit response: status=%s body=%s", resp.status_code, resp.text)

    if 200 <= resp.status_code < 300 or resp.status_code == 425:
        await profile_db.set_profile_status(db_path, profile.id, "submitted")
        await update.message.reply_text(
            f"✅ *Registration submitted for {profile.name}!*\n\n"
            "Your spot is secured! Use /verifyotp when ready.",
            parse_mode="Markdown",
        )
    else:
        error_detail = _extract_error_message(resp)
        await profile_db.set_profile_status(db_path, profile.id, "failed")
        await update.message.reply_text(
            f"❌ Registration failed (HTTP {resp.status_code}).\n\nError: {error_detail}"
        )

    pending_dict.pop(reply_msg_id, None)


# ─── Post-OTP flow: create order + fetch receipt ─────────────────────────────

async def _complete_post_otp_flow(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    profile: profile_db.Profile,
    verify_response: httpx.Response,
    db_path: str,
) -> None:
    """Handle the post-OTP-verification flow: extract token, create order, fetch receipt."""
    # Extract access token
    try:
        verify_data = verify_response.json()
        access_token = verify_data.get("accessToken", "")
    except Exception:
        access_token = ""

    if not access_token:
        await profile_db.set_profile_status(db_path, profile.id, "registered")
        await update.message.reply_text(
            "✅ OTP verified, but no access token received.\n"
            "You may need to complete the order manually.",
        )
        return

    client = _get_http_client(context)
    headers = _build_headers(context)
    headers["Content-Type"] = "application/json"
    headers["Authorization"] = f"Bearer {access_token}"
    headers["Referer"] = "https://adhahi.dz/activation"
    headers["Origin"] = "https://adhahi.dz"

    # ── Create order ──
    order_body = {
        "wilayaId": profile.wilaya_id,
        "communeCode": profile.commune_code,
        "categoryId": 1,
        "paymentMethod": profile.payment_method,
    }

    await update.message.reply_text("⏳ Creating order…")

    try:
        resp = await client.post("/api/v1/orders", json=order_body, headers=headers)
    except Exception as exc:
        logger.error("Order creation network error: %s", exc)
        await update.message.reply_text(
            f"✅ OTP verified but order creation failed.\n\n"
            f"❌ Network error: `{exc}`\n\n"
            f"🔑 Access Token:\n`{access_token}`",
            parse_mode="Markdown",
        )
        await profile_db.set_profile_status(db_path, profile.id, "registered")
        return

    logger.info("Order creation response: status=%s body=%s", resp.status_code, resp.text)

    if resp.status_code >= 400:
        error_detail = _extract_error_message(resp)
        await update.message.reply_text(
            f"✅ OTP verified but order creation failed (HTTP {resp.status_code}).\n\n"
            f"❌ Error: {error_detail}\n\n"
            f"🔑 Access Token:\n`{access_token}`",
            parse_mode="Markdown",
        )
        await profile_db.set_profile_status(db_path, profile.id, "registered")
        return

    # Order created successfully
    await profile_db.set_profile_status(db_path, profile.id, "ordered")

    try:
        order_text = json.dumps(resp.json(), indent=2, ensure_ascii=False)
    except Exception:
        order_text = resp.text

    masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
    await update.message.reply_text(
        f"🎉 *Order Created Successfully!*\n\n"
        f"Profile: *{profile.name or masked}*\n"
        f"```\n{order_text[:3000]}\n```",
        parse_mode="Markdown",
    )

    # ── Fetch my-orders ──
    headers["Referer"] = "https://adhahi.dz/user/confirmation"
    try:
        resp = await client.get(
            "/api/v1/orders/my-orders?page=0&size=10", headers=headers,
        )
        if 200 <= resp.status_code < 300:
            orders_data = resp.json()
            orders_text = json.dumps(orders_data, indent=2, ensure_ascii=False)
            await update.message.reply_text(
                f"📋 *My Orders:*\n```\n{orders_text[:3000]}\n```",
                parse_mode="Markdown",
            )

            # ── Fetch receipt if available ──
            recent = orders_data.get("recentOrders", [])
            if recent:
                receipt_num = recent[0].get("orderReceiptNumber", "")
                if receipt_num:
                    try:
                        resp = await client.get(
                            f"/api/v1/receipts/{receipt_num}/data", headers=headers,
                        )
                        if 200 <= resp.status_code < 300:
                            receipt_text = json.dumps(
                                resp.json(), indent=2, ensure_ascii=False,
                            )
                            await update.message.reply_text(
                                f"🧾 *Receipt ({receipt_num}):*\n"
                                f"```\n{receipt_text[:3000]}\n```",
                                parse_mode="Markdown",
                            )
                        else:
                            logger.warning(
                                "Receipt fetch failed: status=%s body=%s",
                                resp.status_code, resp.text,
                            )
                    except Exception as exc:
                        logger.error("Receipt fetch error: %s", exc)
        else:
            logger.warning(
                "My-orders fetch failed: status=%s body=%s",
                resp.status_code, resp.text,
            )
    except Exception as exc:
        logger.error("My-orders fetch error: %s", exc)


# ─── /verifyotp command ───────────────────────────────────────────────────────

async def verifyotp_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """List submitted profiles and let user pick one to verify OTP."""
    user_id = update.effective_user.id
    db_path: str = context.application.bot_data["db_path"]

    submitted = await profile_db.get_profiles_by_status(db_path, user_id, "submitted")
    pre_registered = await profile_db.get_profiles_by_status(db_path, user_id, "pre-registered")
    submitted = submitted + pre_registered
    if not submitted:
        await update.effective_message.reply_text("No profiles awaiting OTP verification.")
        return ConversationHandler.END

    if len(submitted) == 1:
        # Jump straight to OTP prompt
        context.user_data["verify_otp"] = {"profile": submitted[0]}
        masked = f"{submitted[0].nin[:4]}…{submitted[0].nin[-4:]}"
        await update.effective_message.reply_text(
            f"📱 *OTP Verification*\n\n"
            f"Profile: *{submitted[0].name or masked}*\n"
            f"Phone: `{submitted[0].phone}`\n\n"
            "Enter the OTP you received.\n"
            "If your OTP expired, type `resend` to get a new one.",
            parse_mode="Markdown",
        )
        return VERIFY_OTP_CAPTCHA

    # Multiple — list them
    lines = ["📱 *OTP Verification*\n\nMultiple profiles awaiting verification:\n"]
    for p in submitted:
        masked = f"{p.nin[:4]}…{p.nin[-4:]}"
        lines.append(f"  `{p.id}` — *{p.name or masked}* ({p.phone})")
    lines.append("\nReply with the profile ID number:")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

    context.user_data["verify_otp"] = {"profiles": submitted}
    return VERIFY_OTP_CAPTCHA


async def verifyotp_handle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle OTP input or profile selection for verification."""
    state = context.user_data.get("verify_otp", {})
    db_path: str = context.application.bot_data["db_path"]
    text = update.message.text.strip()

    # Handle resend
    if text.lower() == "resend":
        profile = state.get("profile")
        if not profile:
            await update.message.reply_text("No profile selected. Please start again with /verifyotp.")
            return ConversationHandler.END

        client = _get_http_client(context)
        headers = _build_headers(context)
        headers["Content-Type"] = "application/json"
        try:
            resp = await client.post(
                "/api/v1/citizens/resend-otp",
                json={"nin": profile.nin},
                headers=headers,
            )
            if 200 <= resp.status_code < 300:
                masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
                await update.message.reply_text(
                    f"✅ OTP resent for *{profile.name or masked}* (`{profile.phone}`)!\n"
                    "Enter the new OTP:",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"❌ Failed to resend OTP (HTTP {resp.status_code}). Try again later."
                )
        except Exception as exc:
            await update.message.reply_text(f"❌ Network error: `{exc}`", parse_mode="Markdown")
        return VERIFY_OTP_CAPTCHA

    # If we need profile selection first
    if "profiles" in state and "profile" not in state:
        try:
            pid = int(text)
        except ValueError:
            await update.message.reply_text("Please enter a valid profile ID number:")
            return VERIFY_OTP_CAPTCHA

        profile = next((p for p in state["profiles"] if p.id == pid), None)
        if not profile:
            await update.message.reply_text("Profile not found. Try again:")
            return VERIFY_OTP_CAPTCHA

        state["profile"] = profile
        masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
        await update.message.reply_text(
            f"Selected: *{profile.name or masked}*\n"
            f"Phone: `{profile.phone}`\n\n"
            "Enter the OTP sent to this phone (or type `resend` for a new one):",
            parse_mode="Markdown",
        )
        return VERIFY_OTP_CAPTCHA

    # We have a profile and this is the OTP
    profile = state.get("profile")
    if not profile:
        await update.message.reply_text("Session expired. Please use /verifyotp again.")
        context.user_data.pop("verify_otp", None)
        return ConversationHandler.END

    otp = text
    await update.message.reply_text("⏳ Solving CAPTCHA and verifying OTP…")

    client = _get_http_client(context)
    headers = _build_headers(context)
    headers["Content-Type"] = "application/json"

    # Try auto-CAPTCHA for OTP verification (3 attempts)
    for attempt in range(1, 4):
        solved = await _fetch_and_solve_captcha(client, headers)
        if not solved:
            continue

        captcha_id, answer, _ = solved
        req_headers = {**headers, "X-Captcha-Id": captcha_id, "X-Captcha-Answer": answer}
        body = {"nin": profile.nin, "otp": otp}

        try:
            resp = await client.post("/api/v1/citizens/verify-otp", json=body, headers=req_headers)
        except Exception as exc:
            logger.error("OTP verify network error: %s", exc)
            continue

        logger.info("OTP verify response: status=%s body=%s", resp.status_code, resp.text)

        if 200 <= resp.status_code < 300:
            await _complete_post_otp_flow(update, context, profile, resp, db_path)
            context.user_data.pop("verify_otp", None)
            await _close_http_client(context)
            return ConversationHandler.END

        error_detail = _extract_error_message(resp)
        logger.warning("OTP verify attempt %d failed: %s", attempt, error_detail)

    # All auto-CAPTCHA failed — ask user to solve manually
    captcha = await _fetch_captcha_raw(client, headers)
    if not captcha:
        await update.message.reply_text("❌ Could not generate CAPTCHA. Try /verifyotp again later.")
        context.user_data.pop("verify_otp", None)
        await _close_http_client(context)
        return ConversationHandler.END

    captcha_id, image_bytes = captcha
    state["otp"] = otp
    state["captcha_id"] = captcha_id
    state["captcha_ts"] = time.time()

    await context.bot.send_photo(
        chat_id=update.effective_chat.id,
        photo=io.BytesIO(image_bytes),
        caption="🔐 Auto-CAPTCHA failed. Please solve this CAPTCHA to verify your OTP:",
        parse_mode="Markdown",
    )
    return VERIFY_OTP_CAPTCHA


async def verifyotp_captcha_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle manual CAPTCHA answer for OTP verification."""
    state = context.user_data.get("verify_otp", {})
    profile = state.get("profile")
    otp = state.get("otp")
    captcha_id = state.get("captcha_id")
    db_path: str = context.application.bot_data["db_path"]

    if not profile or not otp or not captcha_id:
        await update.message.reply_text("Session expired. Use /verifyotp again.")
        context.user_data.pop("verify_otp", None)
        return ConversationHandler.END

    answer = update.message.text.strip()
    await update.message.reply_text("⏳ Verifying OTP…")

    client = _get_http_client(context)
    headers = _build_headers(context)
    headers["Content-Type"] = "application/json"
    headers["X-Captcha-Id"] = captcha_id
    headers["X-Captcha-Answer"] = answer

    try:
        resp = await client.post(
            "/api/v1/citizens/verify-otp",
            json={"nin": profile.nin, "otp": otp},
            headers=headers,
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Network error: `{exc}`", parse_mode="Markdown")
        context.user_data.pop("verify_otp", None)
        return ConversationHandler.END

    logger.info("OTP verify (manual captcha) response: status=%s body=%s", resp.status_code, resp.text)

    if 200 <= resp.status_code < 300:
        await _complete_post_otp_flow(update, context, profile, resp, db_path)
    else:
        error_detail = _extract_error_message(resp)
        await update.message.reply_text(
            f"❌ OTP verification failed (HTTP {resp.status_code}).\n\nError: {error_detail}\n\n"
            "Use /verifyotp to try again.",
        )

    context.user_data.pop("verify_otp", None)
    await _close_http_client(context)
    return ConversationHandler.END


async def verifyotp_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("verify_otp", None)
    await _close_http_client(context)
    await update.effective_message.reply_text("OTP verification cancelled.")
    return ConversationHandler.END


# ─── Build handlers ───────────────────────────────────────────────────────────

def build_verifyotp_handler() -> ConversationHandler:
    """ConversationHandler for /verifyotp flow."""
    return ConversationHandler(
        entry_points=[
            CommandHandler("verifyotp", verifyotp_start),
            CallbackQueryHandler(verifyotp_start, pattern=r"^menu:cmd:verifyotp$"),
        ],
        states={
            VERIFY_OTP_CAPTCHA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, verifyotp_handle),
            ],
        },
        fallbacks=[CommandHandler("cancel", verifyotp_cancel)],
        per_user=True,
        per_chat=True,
    )
