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
import random
import time
from typing import Any

import httpx
from telegram import Update
from telegram.error import Forbidden, TimedOut
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from . import profile_db
from .i18n import t
from .db import get_user_language
from .captcha_solver import CaptchaSolver, create_solvers
from .proxy import get_proxy_url
from .registration import (
    _extract_error_message,
    _get_http_client,
    _build_headers,
    _close_http_client,
)
from . import db as db_mod
from .notifier import safe_send_message, safe_send_photo

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
        err_type = type(exc).__name__
        logger.error("Failed to fetch/solve captcha (%s) using %s: %s", err_type, used_solver.name, exc)
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
      "quota_closed" — server returned 'Quota is not active'
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

    # Build attempt list: 5x ddddocr first (fast/free), then 2captcha fallback (slow/paid)
    solvers_to_try = [_primary_solver] * 5
    if _fallback_solver:
        solvers_to_try.append(_fallback_solver)
        solvers_to_try.append(_fallback_solver)

    for attempt, solver in enumerate(solvers_to_try, 1):
        if attempt > 1:
            # Humanizing delay to avoid server rate limits
            await asyncio.sleep(random.uniform(0.5, 1.0))
        solved = await _fetch_and_solve_captcha(client, base_headers, solver=solver)
        if not solved:
            continue

        captcha_id, answer, _ = solved
        req_headers = {**base_headers, "X-Captcha-Id": captcha_id, "X-Captcha-Answer": answer}

        try:
            resp = await client.post("/api/v2/citizens/register", json=body, headers=req_headers)
        except Exception as exc:
            err_type = type(exc).__name__
            logger.error("Submit network error profile=%s (%s): %s", profile.id, err_type, exc)
            return profile, "error", f"{err_type}: {exc}"

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
            # Check if account is already active
            if any(term in error_msg.lower() for term in ["déjà actif", "already active", "déjà enregistré", "already registered", "déjà inscrit"]):
                logger.info("Profile %s is already registered on server: %s", profile.id, error_msg)
                return profile, "already_registered", error_msg

            if "quota" in error_msg.lower() and "active" in error_msg.lower():
                logger.info("Profile %s rejected: Quota is not active.", profile.id)
                return profile, "quota_closed", error_msg

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
    # Build solver list: 5x ddddocr first (fast/free), then 2captcha fallback (slow/paid)
    solvers_to_try: list[CaptchaSolver] = [_primary_solver] * 5
    if _fallback_solver:
        solvers_to_try.append(_fallback_solver)
        solvers_to_try.append(_fallback_solver)

    # ── Step 1: Login ──
    access_token = None
    last_login_error = ""

    for attempt, solver in enumerate(solvers_to_try, 1):
        if attempt > 1:
            # Humanizing delay to avoid server rate limits
            await asyncio.sleep(random.uniform(0.5, 1.0))
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
            err_type = type(exc).__name__
            logger.error("Login network error profile=%s (%s): %s", profile.id, err_type, exc)
            return profile, "error", f"{err_type}: {exc}"

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
        err_type = type(exc).__name__
        logger.error("Order network error profile=%s (%s): %s", profile.id, err_type, exc)
        return profile, "error", f"{err_type}: {exc}"

    logger.info(
        "Order profile=%s status=%s body=%s",
        profile.id, resp.status_code, resp.text,
    )

    if 200 <= resp.status_code < 300:
        return profile, "ordered", resp.text

    error_msg = _extract_error_message(resp)
    if "quota" in error_msg.lower() and "active" in error_msg.lower():
        return profile, "quota_closed", error_msg

    # Recognize existing orders to avoid infinite failure loops
    if any(term in error_msg.lower() for term in ["already have an active order", "commande en cours"]):
        logger.info("Profile %s already has an active order on server.", profile.id)
        return profile, "already_ordered", error_msg

    return profile, "order_fail", error_msg


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
            lang = await get_user_language(db_path, user_id)
            translated_lines = [t(lang, "🔔 *OTP Verification Reminder*\n")]
            for p in profs:
                masked = f"{p.nin[:4]}…{p.nin[-4:]}"
                translated_lines.append(f"  • *{p.name or masked}* (`{p.phone}`)")
            translated_lines.append(t(lang, "\nThese profiles are pre-registered but not yet verified.\nUse /verifyotp to complete verification so they're ready to snatch an order when quota opens!"))
            await safe_send_message(
                app.bot,
                user_id=user_id,
                db_path=db_path,
                text="\n".join(translated_lines),
                parse_mode="Markdown",
            )
            logger.info("Sent pre-registered reminder to user %s (%d profiles)", user_id, len(profs))
        except Forbidden:
            logger.warning("Bot was blocked by user_id=%s. Deleting user data.", user_id)
            await db_mod.delete_user_data(db_path, user_id)
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

    # Check if proxy is enabled for auto-registration via admin panel
    use_proxy = app.bot_data.get("proxy_autoreg", False)
    
    # Validation check for proxy settings
    if use_proxy and not get_proxy_url():
        logger.warning("Proxy is enabled in Admin Panel, but credentials (PROXY_USER/PROXY_PASS) are missing from .env!")

    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
        "Accept": "application/json",
        "Referer": "https://adhahi.dz/register",
        "Content-Type": "application/json",
    }

    try:
        # Group ALL profiles by user_id, preserving seniority order (profiles is already sorted)
        user_groups: dict[int, list[profile_db.Profile]] = {}
        ordered_user_ids: list[int] = []
        for p in profiles:
            if p.user_id not in user_groups:
                ordered_user_ids.append(p.user_id)
                user_groups[p.user_id] = []
            user_groups[p.user_id].append(p)

        async def _process_group(user_id: int, group: list[profile_db.Profile]):
            # Split this user's profiles by status
            pending = [p for p in group if p.status == "pending"]
            registered = [p for p in group if p.status == "registered"]
            preregistered = [p for p in group if p.status == "pre-registered"]

            tasks = []
            
            # 1. Handle pending (registration flow)
            if pending:
                for p in pending:
                    await profile_db.set_profile_status(db_path, p.id, "registering")
                tasks.append(_process_user_profiles(app, user_id, pending, api_client, use_proxy, base_headers, db_path))
            
            # 2. Handle registered (login+order flow)
            if registered:
                for p in registered:
                    await profile_db.set_profile_status(db_path, p.id, "registering")
                tasks.append(_process_registered_profiles(app, registered, api_client, use_proxy, base_headers, db_path))
            
            # 3. Handle pre-registered (nudge flow)
            if preregistered:
                tasks.append(_nudge_preregistered_profiles(app, preregistered, api_client, use_proxy, base_headers))
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        # Launch all user groups concurrently. The semaphore in sub-functions will throttle connections.
        await asyncio.gather(*[_process_group(uid, user_groups[uid]) for uid in ordered_user_ids], return_exceptions=True)

    except Exception:
        logger.exception("Unexpected error in auto_submit_profiles")


async def _nudge_preregistered_profiles(
    app,
    profiles: list[profile_db.Profile],
    api_client: Any,
    use_proxy: bool,
    base_headers: dict[str, str],
) -> None:
    """Resend OTP for pre-registered profiles and urgently notify users.

    This is fire-and-forget — we resend the OTP and tell the user to verify,
    but we never block waiting for their response.
    """
    sem = app.bot_data["concurrency_semaphore"]
    nudge_history = app.bot_data.setdefault("nudge_history", {}) # profile_id -> timestamp
    now = time.time()
    
    for profile in profiles:
        # Cooldown check: only nudge once every hour
        last_nudge = nudge_history.get(profile.id, 0)
        if now - last_nudge < 3600:
            logger.debug("Skipping nudge for profile %s (cooldown active)", profile.id)
            continue

        # Each nudge gets its own connection (minimal overhead as it's sequential)
        async with sem:
            proxy_url = get_proxy_url(session_id=profile.nin) if use_proxy else None
            async with api_client.create_session(proxy_url=proxy_url) as client:
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
                except Exception as exc:
                    err_type = type(exc).__name__
                    logger.error("OTP resend network error for profile %s (%s): %s", profile.id, err_type, exc)

        # Notify user urgently — don't wait for response
        try:
            if otp_sent:
                lang = await get_user_language(db_path, user_id)
                await safe_send_message(
                    app.bot,
                    user_id=user_id,
                    db_path=db_path,
                    text=t(lang, "🚨 *QUOTA IS OPEN — Verify NOW!*\n\nProfile: *{name}*\nPhone: `{phone}`\n\nAn OTP has been resent to your phone.\nPlease use the official website *immediately* to verify and complete your order:\n🔗 https://adhahi.dz/activation").format(name=profile.name or masked, phone=profile.phone),
                    parse_mode="Markdown",
                )
                nudge_history[profile.id] = now
            else:
                lang = await get_user_language(db_path, user_id)
                await safe_send_message(
                    app.bot,
                    user_id=user_id,
                    db_path=db_path,
                    text=t(lang, "🚨 *QUOTA IS OPEN — Verify NOW!*\n\nProfile: *{name}*\nPhone: `{phone}`\n\n⚠️ Failed to resend OTP. Please try verifying via the official website:\n🔗 https://adhahi.dz/activation").format(name=profile.name or masked, phone=profile.phone),
                    parse_mode="Markdown",
                )
        except Forbidden:
            logger.warning("Bot was blocked by user_id=%s. Deleting user data.", user_id)
            await db_mod.delete_user_data(db_path, user_id)
        except Exception:
            logger.exception("Failed to send urgent nudge to user %s", user_id)


async def _process_registered_profiles(
    app,
    profiles: list[profile_db.Profile],
    api_client: Any,
    use_proxy: bool,
    base_headers: dict[str, str],
    db_path: str,
) -> None:
    """Handle registered profiles: login + submit order concurrently.

    Each profile uses a dedicated sticky proxy session to ensure IP consistency.
    """
    if not profiles:
        return
    user_id = profiles[0].user_id
    sem = app.bot_data["concurrency_semaphore"]

    try:
        async def _run_sticky_login_and_order(p: profile_db.Profile):
            async with sem:
                proxy_url = get_proxy_url(session_id=p.nin) if use_proxy else None
                async with api_client.create_session(proxy_url=proxy_url) as client:
                    return await _try_login_and_order(p, client, base_headers)

        results = await asyncio.gather(
            *[_run_sticky_login_and_order(p) for p in profiles],
            return_exceptions=True
        )

        for result in results:
            if isinstance(result, Exception):
                logger.error("Unexpected error in login+order: %s", result)
                continue

            profile, outcome, detail = result
            masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"

            if outcome == "ordered" or outcome == "already_ordered":
                await profile_db.set_profile_status(db_path, profile.id, "ordered")
                lang = await get_user_language(db_path, user_id)
                
                if outcome == "ordered":
                    text = t(lang, "🐑 *Order placed!*\n\nProfile: *{name}*\nPhone: `{phone}`\n\nYour order has been submitted successfully!")
                else:
                    text = t(lang, "🐑 *Order already active!*\n\nProfile: *{name}*\nPhone: `{phone}`\n\nI detected that this profile already has an active order. Status updated successfully.")
                
                await safe_send_message(
                    app.bot,
                    user_id=user_id,
                    db_path=db_path,
                    text=text.format(name=profile.name or masked, phone=profile.phone),
                    parse_mode="Markdown",
                )
            elif outcome == "order_fail":
                logger.error("Order failed for profile %s: %s", profile.id, detail)
                await profile_db.set_profile_status(db_path, profile.id, "registered")
                lang = await get_user_language(db_path, user_id)
                await safe_send_message(
                    app.bot,
                    user_id=user_id,
                    db_path=db_path,
                    text=t(lang, "❌ *Order failed for {name}*\n\nError: {detail}\n\nThe profile is still registered. Will retry next time quota is available.").format(name=profile.name or masked, detail=detail),
                    parse_mode="Markdown",
                )
            elif outcome == "login_fail":
                logger.error("Login failed for profile %s: %s", profile.id, detail)
                await profile_db.set_profile_status(db_path, profile.id, "registered")
                lang = await get_user_language(db_path, user_id)
                await safe_send_message(
                    app.bot,
                    user_id=user_id,
                    db_path=db_path,
                    text=t(lang, "❌ *Login failed for {name}*\n\nError: {detail}\n\nWill retry next time quota is available.").format(name=profile.name or masked, detail=detail),
                    parse_mode="Markdown",
                )
            elif outcome == "quota_closed":
                logger.info("Order missed for profile %s: Quota closed.", profile.id)
                await profile_db.set_profile_status(db_path, profile.id, "registered")
                lang = await get_user_language(db_path, user_id)
                await safe_send_message(
                    app.bot,
                    user_id=user_id,
                    db_path=db_path,
                    text=t(lang, "⏳ *Order missed for {name}*\n\nThe quota closed while I was solving the login CAPTCHA.\nI've kept the profile as *registered*. Will retry immediately when it re-opens!").format(name=profile.name or masked),
                    parse_mode="Markdown",
                )
            else:
                logger.error("Login+order error for profile %s: %s", profile.id, detail)
                await profile_db.set_profile_status(db_path, profile.id, "registered")
                lang = await get_user_language(db_path, user_id)
                await safe_send_message(
                    app.bot,
                    user_id=user_id,
                    db_path=db_path,
                    text=t(lang, "❌ Error for profile *{name}*:\n{detail}").format(name=profile.name or masked, detail=detail),
                    parse_mode="Markdown",
                )
    except Forbidden:
        logger.warning("Bot was blocked by user_id=%s. Deleting all data.", user_id)
        await db_mod.delete_user_data(db_path, user_id)
    except Exception:
        logger.exception("Failed to process registered profiles for user %s", user_id)


async def _process_user_profiles(
    app,
    user_id: int,
    profiles: list[profile_db.Profile],
    api_client: Any,
    use_proxy: bool,
    base_headers: dict[str, str],
    db_path: str,
) -> None:
    """Phase 1: concurrent auto-CAPTCHA. Phase 2: sequential manual fallback.

    Each profile uses a dedicated sticky proxy session to ensure IP consistency.
    """
    try:
        sem = app.bot_data["concurrency_semaphore"]
        async def _run_sticky_submit(p: profile_db.Profile):
            async with sem:
                proxy_url = get_proxy_url(session_id=p.nin) if use_proxy else None
                async with api_client.create_session(proxy_url=proxy_url) as client:
                    return await _try_submit_profile(p, client, base_headers)

        # ── Phase 1: Concurrent auto-CAPTCHA ──
        results = await asyncio.gather(
            *[_run_sticky_submit(p) for p in profiles],
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
                lang = await get_user_language(db_path, user_id)
                await safe_send_message(
                    app.bot,
                    user_id=user_id,
                    db_path=db_path,
                    text=t(lang, "✅ *Registration submitted!*\n\nProfile: *{name}*\nPhone: `{phone}`\n\nYour spot is secured! Use the official website to complete OTP verification when ready:\n🔗 https://adhahi.dz/activation").format(name=profile.name or masked, phone=profile.phone),
                    parse_mode="Markdown",
                )
            elif outcome == "already_registered":
                # Switch to login + order flow immediately
                logger.info("Profile %s already registered. Switching to login flow.", profile.id)
                await profile_db.set_profile_status(db_path, profile.id, "registered")
                
                async with sem:
                    proxy_url = get_proxy_url(session_id=profile.nin) if use_proxy else None
                    async with api_client.create_session(proxy_url=proxy_url) as client:
                        l_profile, l_outcome, l_detail = await _try_login_and_order(profile, client, base_headers)
                
                masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
                if l_outcome == "ordered" or l_outcome == "already_ordered":
                    await profile_db.set_profile_status(db_path, l_profile.id, "ordered")
                    lang = await get_user_language(db_path, user_id)
                    
                    if l_outcome == "ordered":
                        text = t(lang, "🐑 *Order placed!*\n\nProfile: *{name}*\nAccount was already active; order was submitted successfully!")
                    else:
                        text = t(lang, "🐑 *Order already active!*\n\nProfile: *{name}*\nAccount was already active and an order was already found on server! Status updated.")
                        
                    await safe_send_message(
                        app.bot,
                        user_id=user_id,
                        db_path=db_path,
                        text=text.format(name=l_profile.name or masked),
                        parse_mode="Markdown",
                    )
                else:
                    lang = await get_user_language(db_path, user_id)
                    await safe_send_message(
                        app.bot,
                        user_id=user_id,
                        db_path=db_path,
                        text=t(lang, "🟡 *Account already active for {name}*\n\nI tried to log in and place an order, but failed: {detail}\nStatus updated to *registered*. Will retry order next time.").format(name=profile.name or masked, detail=l_detail),
                        parse_mode="Markdown",
                    )
            elif outcome == "ip_blocked":
                ip_blocked = True
                await profile_db.set_profile_status(db_path, profile.id, "pending")
                remaining.append(profile)
            elif outcome == "captcha_fail":
                await profile_db.set_profile_status(db_path, profile.id, "pending")
                remaining.append(profile)
            elif outcome == "quota_closed":
                await profile_db.set_profile_status(db_path, profile.id, "pending")
                masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
                lang = await get_user_language(db_path, user_id)
                await safe_send_message(
                    app.bot,
                    user_id=user_id,
                    db_path=db_path,
                    text=t(lang, "⏳ *Registration missed for {name}*\n\nThe quota closed while I was solving the CAPTCHA.\nI've reset the profile to *pending*. Will try again immediately when it re-opens!").format(name=profile.name or masked),
                    parse_mode="Markdown",
                )
            else:
                await profile_db.set_profile_status(db_path, profile.id, "failed")
                lang = await get_user_language(db_path, user_id)
                
                # Real-time MICLAT NOT FOUND notification
                if "MICLAT NOT FOUND" in detail:
                    text = t(lang, "⚠️ *Invalid NIN Detected*\n\nYour profile *{name}* was rejected by the server because the NIN `{nin}` does not exist in the Ministry of Interior's database (MICLAT).\n\nPlease check for typos and edit your profile using the /profiles menu.").format(name=profile.name or f"ID {profile.id}", nin=profile.nin)
                else:
                    text = t(lang, "❌ Registration failed for profile *{name}*:\n{detail}").format(name=profile.name, detail=detail)
                
                await safe_send_message(
                    app.bot,
                    user_id=user_id,
                    db_path=db_path,
                    text=text,
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
            async with sem:
                proxy_url = get_proxy_url(session_id=profile.nin) if use_proxy else None
                async with api_client.create_session(proxy_url=proxy_url) as client:
                    p_result = await _try_submit_profile(profile, client, base_headers)
                    _, outcome, detail = p_result

                    if outcome == "submitted":
                        await profile_db.set_profile_status(db_path, profile.id, "submitted")
                        masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
                        await safe_send_message(
                            app.bot,
                            user_id=user_id,
                            db_path=db_path,
                            text=(
                                f"✅ *Registration submitted!*\n\n"
                                f"Profile: *{profile.name or masked}*\n"
                                f"Phone: `{profile.phone}`\n\n"
                                "Your spot is secured! Use the official website to complete OTP verification when ready:\n🔗 https://adhahi.dz/activation"
                            ),
                            parse_mode="Markdown",
                        )
                    elif outcome == "already_registered":
                        # Switch to login + order flow immediately
                        logger.info("Profile %s already registered (Phase 2). Switching to login flow.", profile.id)
                        await profile_db.set_profile_status(db_path, profile.id, "registered")
                        
                        l_profile, l_outcome, l_detail = await _try_login_and_order(profile, client, base_headers)
                        masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
                        if l_outcome == "ordered":
                            await profile_db.set_profile_status(db_path, l_profile.id, "ordered")
                            lang = await get_user_language(db_path, user_id)
                            await safe_send_message(
                                app.bot,
                                user_id=user_id,
                                db_path=db_path,
                                text=t(lang, "🐑 *Order placed!*\n\nProfile: *{name}*\nAccount was already active; order was submitted successfully!").format(name=l_profile.name or masked),
                                parse_mode="Markdown",
                            )
                        else:
                            lang = await get_user_language(db_path, user_id)
                            await safe_send_message(
                                app.bot,
                                user_id=user_id,
                                db_path=db_path,
                                text=t(lang, "🟡 *Account already active for {name}*\n\nI tried to log in and place an order, but failed: {detail}\nStatus updated to *registered*. Will retry order next time.").format(name=profile.name or masked, detail=l_detail),
                                parse_mode="Markdown",
                            )
                    elif outcome == "ip_blocked":
                        await profile_db.set_profile_status(db_path, profile.id, "pending")
                        lang = await get_user_language(db_path, user_id)
                        await safe_send_message(
                            app.bot,
                            user_id=user_id,
                            db_path=db_path,
                            text=t(lang, "⚠️ *IP Blocked during Phase 2* for {name}. Resetting to pending.").format(name=profile.name or masked),
                            parse_mode="Markdown",
                        )
                    elif outcome == "captcha_fail":
                        # Queue manual CAPTCHA for user
                        await _send_manual_captcha(app, user_id, profile, client, base_headers, db_path)
                    elif outcome == "quota_closed":
                        await profile_db.set_profile_status(db_path, profile.id, "pending")
                        masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
                        lang = await get_user_language(db_path, user_id)
                        await safe_send_message(
                            app.bot,
                            user_id=user_id,
                            db_path=db_path,
                            text=t(lang, "⏳ *Registration missed (Phase 2) for {name}*\n\nThe quota closed while I was solving the CAPTCHA.\nI've reset the profile to *pending*. Will try again when it re-opens!").format(name=profile.name or masked),
                            parse_mode="Markdown",
                        )
                    else:
                        await profile_db.set_profile_status(db_path, profile.id, "failed")
                        lang = await get_user_language(db_path, user_id)
                        
                        # Real-time MICLAT NOT FOUND notification
                        if "MICLAT NOT FOUND" in detail:
                            text = t(lang, "⚠️ *Invalid NIN Detected*\n\nYour profile *{name}* was rejected by the server because the NIN `{nin}` does not exist in the Ministry of Interior's database (MICLAT).\n\nPlease check for typos and edit your profile using the /profiles menu.").format(name=profile.name or f"ID {profile.id}", nin=profile.nin)
                        else:
                            text = t(lang, "❌ Registration failed for profile *{name}*:\n{detail}").format(name=profile.name, detail=detail)
                        
                        await safe_send_message(
                            app.bot,
                            user_id=user_id,
                            db_path=db_path,
                            text=text,
                            parse_mode="Markdown",
                        )
    except Forbidden:
        logger.warning("Bot was blocked by user_id=%s. Deleting all data.", user_id)
        await db_mod.delete_user_data(db_path, user_id)
    except Exception:
        logger.exception("Failed to process profiles for user %s", user_id)


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
        lang = await get_user_language(db_path, user_id)
        await safe_send_message(
            app.bot,
            user_id=user_id,
            db_path=db_path,
            text=t(lang, "⚠️ Could not generate CAPTCHA for profile *{name}*. Will retry next cycle.").format(name=profile.name),
            parse_mode="Markdown",
        )
        return

    captcha_id, image_bytes = captcha

    # Store pending manual captcha keyed by user_id and message_id
    masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
    lang = await get_user_language(db_path, user_id)
    msg = await safe_send_photo(
        app.bot,
        user_id=user_id,
        db_path=db_path,
        photo=io.BytesIO(image_bytes),
        caption=t(lang, "🔐 *Manual CAPTCHA needed*\n\nProfile: *{name}*\nPhone: `{phone}`\n\nAuto-solve failed. **Reply to this message** with the CAPTCHA answer.\n_Expires in {expires_in} seconds._").format(name=profile.name or masked, phone=profile.phone, expires_in=_CAPTCHA_TTL_S),
        parse_mode="Markdown",
    )
    if not msg:
        return

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
        new_msg = await safe_send_photo(
            context.bot,
            user_id=update.effective_chat.id,
            db_path=db_path,
            photo=io.BytesIO(new_bytes),
            caption=(
                f"🔐 *CAPTCHA* for *{profile.name or masked}*\n"
                "**Reply to this message** with your answer."
            ),
            parse_mode="Markdown",
        )
        if not new_msg:
            return
        
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
            "Your spot is secured! Use the official website to verify when ready:\n🔗 https://adhahi.dz/activation",
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
    """Inform user that OTP verification via bot is disabled."""
    db_path: str = context.application.bot_data["db_path"]
    lang = await get_user_language(db_path, update.effective_user.id)
    
    await update.effective_message.reply_text(
        t(lang, "⚠️ *OTP verification via bot is currently disabled.* \n\nPlease use the official website to verify your OTP and complete your registration:\n\n🔗 https://adhahi.dz/activation"),
        parse_mode="Markdown",
        disable_web_page_preview=False
    )
    return ConversationHandler.END


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

    await safe_send_photo(
        context.bot,
        user_id=update.effective_chat.id,
        db_path=db_path,
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
        per_message=False,
    )
