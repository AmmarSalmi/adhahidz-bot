"""Global Profile Sync — two-stage cascade to audit all profiles.

Stage 1 (Light Probe): POST /api/v1/citizens/resend-otp for each NIN.
Stage 2 (Deep Audit):  Login + check orders for profiles flagged 'registered'.

Safety:  batch of 3 (stage 1) or 2 (stage 2), random jitter, auto-pause on 429.
CAPTCHA: 3× ddddocr → 1× 2captcha fallback.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import random
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

import httpx

from . import db as db_mod
from . import profile_db
from .captcha_solver import create_solvers
from .db import get_user_language
from .i18n import t
from .notifier import safe_send_message
from .proxy import get_proxy_url

logger = logging.getLogger(__name__)

# ── Registration-style headers ────────────────────────────────────────────────
_REG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Referer": "https://adhahi.dz/activation",
    "Origin": "https://adhahi.dz",
    "Content-Type": "application/json",
}

# ── Batching / safety constants ───────────────────────────────────────────────
STAGE1_BATCH = 3
STAGE1_JITTER = (3.0, 7.0)
STAGE2_BATCH = 2
STAGE2_JITTER = (5.0, 10.0)
RATE_LIMIT_PAUSE_S = 900  # 15 minutes


# ══════════════════════════════════════════════════════════════════════════════
#  Public entry point
# ══════════════════════════════════════════════════════════════════════════════

async def run_global_sync(app) -> dict[str, int]:
    """Run the full two-stage global sync.  Returns a stats dict."""
    db_path: str = app.bot_data["db_path"]
    api_client = app.bot_data["api_client"]
    admin_id = app.bot_data.get("admin_id")

    # Initialise CAPTCHA solvers once
    primary_solver, fallback_solver = create_solvers()

    profiles = await profile_db.get_unsynced_profiles(db_path)
    total_profiles = await profile_db.get_total_profile_count(db_path)

    stats = {
        "total": total_profiles,
        "scanned": len(profiles),
        "pending": 0,
        "pre_registered": 0,
        "registered_need_audit": 0,  # flagged for stage 2
        "registered_no_order": 0,
        "ordered": 0,
        "bad_password": 0,
        "errors": 0,
        "skipped_synced": total_profiles - len(profiles),
        "notifications_sent": 0,
        "rate_limit_pauses": 0,
    }

    start_ts = time.monotonic()

    if admin_id:
        try:
            await app.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🔄 *Global Sync Started*\n\n"
                    f"Total profiles: *{total_profiles}*\n"
                    f"Unsynced (to scan): *{len(profiles)}*\n"
                    f"Already synced (skipping): *{stats['skipped_synced']}*"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            logger.exception("Failed to send sync start message to admin")

    # ── Stage 1 — Light Probe ─────────────────────────────────────────────
    stage2_profiles: list[profile_db.Profile] = []

    for i in range(0, len(profiles), STAGE1_BATCH):
        batch = profiles[i:i + STAGE1_BATCH]
        for p in batch:
            result = await _probe_nin(api_client, p, app)
            if result == "rate_limited":
                stats["rate_limit_pauses"] += 1
                if admin_id:
                    try:
                        await app.bot.send_message(
                            chat_id=admin_id,
                            text="⚠️ *Rate limited during sync.* Pausing 15 minutes…",
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass
                await asyncio.sleep(RATE_LIMIT_PAUSE_S)
                # Retry this profile after pause
                result = await _probe_nin(api_client, p, app)

            if result == "pre-registered":
                stats["pre_registered"] += 1
            elif result == "registered":
                stats["registered_need_audit"] += 1
                stage2_profiles.append(p)
            elif result == "pending":
                stats["pending"] += 1
            else:
                stats["errors"] += 1

        # Jitter between batches
        if i + STAGE1_BATCH < len(profiles):
            await asyncio.sleep(random.uniform(*STAGE1_JITTER))

    # ── Stage 2 — Deep Audit ──────────────────────────────────────────────
    for i in range(0, len(stage2_profiles), STAGE2_BATCH):
        batch = stage2_profiles[i:i + STAGE2_BATCH]
        for p in batch:
            result = await _deep_audit(
                api_client, p, app,
                primary_solver=primary_solver,
                fallback_solver=fallback_solver,
            )
            if result == "rate_limited":
                stats["rate_limit_pauses"] += 1
                await asyncio.sleep(RATE_LIMIT_PAUSE_S)
                result = await _deep_audit(
                    api_client, p, app,
                    primary_solver=primary_solver,
                    fallback_solver=fallback_solver,
                )
            if result == "ordered":
                stats["ordered"] += 1
            elif result == "bad_password":
                stats["bad_password"] += 1
            elif result == "registered_no_order":
                stats["registered_no_order"] += 1
            else:
                stats["errors"] += 1

        if i + STAGE2_BATCH < len(stage2_profiles):
            await asyncio.sleep(random.uniform(*STAGE2_JITTER))

    # ── Send admin summary ────────────────────────────────────────────────
    elapsed = time.monotonic() - start_ts
    elapsed_min = elapsed / 60

    if admin_id:
        try:
            summary = (
                f"📊 *Global Sync Complete*\n\n"
                f"⏱ Duration: *{elapsed_min:.1f} minutes*\n"
                f"📋 Total profiles in DB: *{stats['total']}*\n"
                f"🔍 Scanned this run: *{stats['scanned']}*\n"
                f"⏭ Skipped (already synced): *{stats['skipped_synced']}*\n\n"
                f"*Status Breakdown:*\n"
                f"  🆕 Pending (not registered): *{stats['pending']}*\n"
                f"  🔵 Pre-registered (OTP pending): *{stats['pre_registered']}*\n"
                f"  🟢 Registered (no order): *{stats['registered_no_order']}*\n"
                f"  🎉 Ordered (has pending order): *{stats['ordered']}*\n"
                f"  🔑 Bad password: *{stats['bad_password']}*\n"
                f"  ❌ Errors: *{stats['errors']}*\n\n"
                f"⚠️ Rate limit pauses: *{stats['rate_limit_pauses']}*"
            )
            await app.bot.send_message(
                chat_id=admin_id, text=summary, parse_mode="Markdown"
            )
        except Exception:
            logger.exception("Failed to send sync summary to admin")

    return stats


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 1 — Light Probe
# ══════════════════════════════════════════════════════════════════════════════

async def _probe_nin(api_client, profile: profile_db.Profile, app) -> str:
    """POST resend-otp and interpret the response.

    Returns: 'pre-registered', 'registered', 'pending', 'error', 'rate_limited'.
    Also sends user notification and updates DB status + is_synced.
    """
    db_path: str = app.bot_data["db_path"]
    client = api_client.create_session()
    masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
    pname = profile.name or masked

    try:
        try:
            resp = await client.post(
                "/api/v1/citizens/resend-otp",
                json={"nin": profile.nin},
                headers=_REG_HEADERS,
            )
        except Exception as exc:
            logger.error("Stage 1 network error for profile %s: %s", profile.id, exc)
            return "error"

        if resp.status_code == 429:
            return "rate_limited"

        if 200 <= resp.status_code < 300:
            # Fresh OTP sent — pre-registered
            await profile_db.set_profile_status(db_path, profile.id, "pre-registered")
            await profile_db.mark_profile_synced(db_path, profile.id)
            await _notify_pre_registered_fresh(app, profile)
            return "pre-registered"

        # Parse error body
        try:
            error_msg = resp.json().get("message", resp.text)
        except Exception:
            error_msg = resp.text or ""

        if "déjà été envoyé" in error_msg:
            # OTP already active (cooldown) — still pre-registered
            remaining_time = _parse_remaining_time(error_msg)
            await profile_db.set_profile_status(db_path, profile.id, "pre-registered")
            await profile_db.mark_profile_synced(db_path, profile.id)
            await _notify_pre_registered_cooldown(app, profile, remaining_time)
            return "pre-registered"

        if "Compte déjà actif" in error_msg:
            # Account is active — need Stage 2 deep audit
            await profile_db.set_profile_status(db_path, profile.id, "registered")
            # Don't mark synced yet — Stage 2 will do it
            return "registered"

        # "User not found" or other 4xx → pending
        await profile_db.set_profile_status(db_path, profile.id, "pending")
        await profile_db.mark_profile_synced(db_path, profile.id)
        return "pending"

    finally:
        await client.aclose()


def _parse_remaining_time(msg: str) -> str:
    """Extract wait time from server message like 'veuillez patienter 12 minutes'."""
    match = re.search(r"(\d+)\s*(minute|min|mn|second|sec)", msg, re.IGNORECASE)
    if match:
        num = match.group(1)
        unit = match.group(2).lower()
        if unit.startswith("sec"):
            return f"{num} seconds"
        return f"{num} minutes"
    return "a few minutes"


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 2 — Deep Audit (Login + Orders)
# ══════════════════════════════════════════════════════════════════════════════

async def _deep_audit(
    api_client,
    profile: profile_db.Profile,
    app,
    *,
    primary_solver,
    fallback_solver,
) -> str:
    """Login and check orders for a registered profile.

    Returns: 'ordered', 'registered_no_order', 'bad_password', 'error', 'rate_limited'.
    """
    db_path: str = app.bot_data["db_path"]
    client = api_client.create_session()
    masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"

    try:
        # ── Solve CAPTCHA and login ───────────────────────────────────────
        access_token = None
        login_error_msg = ""

        for attempt in range(4):  # 3 × ddddocr + 1 × 2captcha
            solver = primary_solver if attempt < 3 else fallback_solver
            if solver is None:
                break

            try:
                captcha_resp = await client.get(
                    "/api/v1/captcha/generate", headers=_REG_HEADERS
                )
                if captcha_resp.status_code == 429:
                    return "rate_limited"
                captcha_resp.raise_for_status()
                cdata = captcha_resp.json()
                captcha_id = cdata["captchaId"]
                img_uri = cdata["captchaImage"]
                b64 = img_uri.split(",", 1)[1] if "," in img_uri else img_uri
                img_bytes = base64.b64decode(b64)
            except Exception as exc:
                logger.error("Captcha fetch failed for profile %s: %s", profile.id, exc)
                continue

            try:
                answer = await solver.solve(img_bytes)
            except Exception as exc:
                logger.warning("Captcha solve failed (%s) for profile %s: %s",
                               solver.name, profile.id, exc)
                continue

            login_headers = {
                **_REG_HEADERS,
                "X-Captcha-Id": str(captcha_id),
                "X-Captcha-Answer": str(answer),
            }
            login_body = {
                "nin": profile.nin,
                "password": profile.password,
                "deviceInfo": "WEB_APP",
                "sessionType": "WEB",
            }

            try:
                login_resp = await client.post(
                    "/api/v1/citizens/login",
                    json=login_body,
                    headers=login_headers,
                )
            except Exception as exc:
                logger.error("Login request failed for profile %s: %s", profile.id, exc)
                continue

            if login_resp.status_code == 429:
                return "rate_limited"

            if 200 <= login_resp.status_code < 300:
                access_token = login_resp.json().get("token")
                break

            # Parse login error
            try:
                login_error_msg = login_resp.json().get("message", login_resp.text)
            except Exception:
                login_error_msg = login_resp.text or ""

            # Check for bad password specifically
            if any(kw in login_error_msg.lower() for kw in (
                "mot de passe", "password", "incorrect", "invalid credentials"
            )):
                # Bad password detected — mark invalid and notify user
                await profile_db.set_profile_invalid(db_path, profile.id)
                await profile_db.set_profile_status(db_path, profile.id, "registered")
                await _notify_bad_password(app, profile)
                return "bad_password"

            # If it's a captcha error, try again with next solver
            await asyncio.sleep(1)

        if not access_token:
            # Could not login after all attempts (bad captcha or other)
            # If we got a password error above, we already returned.
            # Otherwise treat as transient error — don't mark synced so we retry later.
            logger.warning(
                "Could not login profile %s after 4 attempts. Last error: %s",
                profile.id, login_error_msg
            )
            return "error"

        # ── Fetch orders ──────────────────────────────────────────────────
        order_headers = {
            **_REG_HEADERS,
            "Authorization": f"Bearer {access_token}",
            "Referer": "https://adhahi.dz/user/confirmation",
        }

        try:
            orders_resp = await client.get(
                "/api/v1/orders/my-orders?page=0&size=10",
                headers=order_headers,
            )
        except Exception as exc:
            logger.error("Orders fetch failed for profile %s: %s", profile.id, exc)
            return "error"

        if orders_resp.status_code == 429:
            return "rate_limited"

        if 200 <= orders_resp.status_code < 300:
            orders_data = orders_resp.json()
            recent = orders_data.get("recentOrders", [])
            has_pending = any(o.get("status") == "PENDING" for o in recent)

            if has_pending:
                await profile_db.set_profile_status(db_path, profile.id, "ordered")
                await profile_db.mark_profile_synced(db_path, profile.id)
                await _notify_ordered(app, profile)
                return "ordered"
            else:
                await profile_db.set_profile_status(db_path, profile.id, "registered")
                await profile_db.mark_profile_synced(db_path, profile.id)
                await _notify_no_order(app, profile)
                return "registered_no_order"
        else:
            logger.error(
                "Orders fetch returned HTTP %s for profile %s",
                orders_resp.status_code, profile.id
            )
            return "error"

    finally:
        await client.aclose()


# ══════════════════════════════════════════════════════════════════════════════
#  User notification helpers
# ══════════════════════════════════════════════════════════════════════════════

async def _notify_pre_registered_fresh(app, profile: profile_db.Profile) -> None:
    """Notify user: fresh OTP sent, please verify."""
    db_path = app.bot_data["db_path"]
    lang = await get_user_language(db_path, profile.user_id)
    masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
    pname = profile.name or masked
    msg_key = (
        "⚠️ *Action Required: Complete OTP Verification*\n\n"
        "Profile: *{name}*\nNIN: `{nin}`\n\n"
        "Your profile is registered but OTP verification is still pending.\n"
        "A new OTP has just been sent to your phone.\n"
        "If you don't verify, you will miss the next auto-registration batch "
        "when quota opens for your wilaya.\n\n"
        "Please complete verification now:\n"
        "🔗 https://adhahi.dz/activation"
    )
    text = t(lang, msg_key).format(name=pname, nin=profile.nin)
    await safe_send_message(app.bot, profile.user_id, db_path=db_path,
                            text=text, parse_mode="Markdown")


async def _notify_pre_registered_cooldown(
    app, profile: profile_db.Profile, remaining_time: str
) -> None:
    """Notify user: OTP already active, include wait time."""
    db_path = app.bot_data["db_path"]
    lang = await get_user_language(db_path, profile.user_id)
    masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
    pname = profile.name or masked
    msg_key = (
        "⚠️ *Action Required: Complete OTP Verification*\n\n"
        "Profile: *{name}*\nNIN: `{nin}`\n\n"
        "Your profile is registered but OTP verification is still pending.\n"
        "An OTP was already sent to your phone. You can request a new one "
        "in {remaining_time}.\n"
        "If you don't verify, you will miss the next auto-registration batch "
        "when quota opens for your wilaya.\n\n"
        "Please complete verification now:\n"
        "🔗 https://adhahi.dz/activation"
    )
    text = t(lang, msg_key).format(
        name=pname, nin=profile.nin, remaining_time=remaining_time
    )
    await safe_send_message(app.bot, profile.user_id, db_path=db_path,
                            text=text, parse_mode="Markdown")


async def _notify_bad_password(app, profile: profile_db.Profile) -> None:
    """Notify user: password was rejected by the server."""
    db_path = app.bot_data["db_path"]
    lang = await get_user_language(db_path, profile.user_id)
    masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
    pname = profile.name or masked
    msg_key = (
        "🔑 *Password Issue Detected*\n\n"
        "Profile: *{name}*\nNIN: `{nin}`\n\n"
        "The password stored in your profile was rejected by the server.\n"
        "Please reset your password or provide the correct one.\n\n"
        "🔗 Reset password: https://adhahi.dz/forgot-password\n\n"
        "After resetting, update your profile via /profiles → Edit Profile."
    )
    text = t(lang, msg_key).format(name=pname, nin=profile.nin)
    await safe_send_message(app.bot, profile.user_id, db_path=db_path,
                            text=text, parse_mode="Markdown")


async def _notify_no_order(app, profile: profile_db.Profile) -> None:
    """Notify user: registered but no pending order found."""
    db_path = app.bot_data["db_path"]
    lang = await get_user_language(db_path, profile.user_id)
    masked = f"{profile.nin[:4]}…{profile.nin[-4:]}"
    pname = profile.name or masked
    msg_key = (
        "📋 *No Active Order Found*\n\n"
        "Profile: *{name}*\nNIN: `{nin}`\n\n"
        "Your account is active on the server, but we found no pending order.\n"
        "Please visit the website to login, check the orders section, and "
        "confirm manually.\n\n"
        "🔗 https://adhahi.dz/login\n\n"
        "Rest assured — the bot will still try to place an order for you "
        "next time your wilaya's quota opens."
    )
    text = t(lang, msg_key).format(name=pname, nin=profile.nin)
    await safe_send_message(app.bot, profile.user_id, db_path=db_path,
                            text=text, parse_mode="Markdown")


async def _notify_ordered(app, profile: profile_db.Profile) -> None:
    """Notify user: active order found — Eid reminder."""
    db_path = app.bot_data["db_path"]
    lang = await get_user_language(db_path, profile.user_id)
    pname = profile.name or f"{profile.nin[:4]}…{profile.nin[-4:]}"
    msg_key = (
        "🎉 *Order Reminder*\n\n"
        "Profile: *{name}*\n\n"
        "You already have an active order for this profile.\n"
        "Please remember that you can track your order by logging into the "
        "official website adhahi.dz.\n\n"
        "Also, watch for SMS sent by the website that sets your appointment.\n\n"
        "Have a blessed Eid Al-Adha! 🐑"
    )
    text = t(lang, msg_key).format(name=pname)
    await safe_send_message(app.bot, profile.user_id, db_path=db_path,
                            text=text, parse_mode="Markdown")
