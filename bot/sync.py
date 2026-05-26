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
from telegram.helpers import escape_markdown

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


def _parse_remaining_time(msg: str) -> str:
    """Extract remaining time from the OTP resend error message."""
    # Find all matches of numbers followed by time units (minutes, secondes, etc.) in French or English
    pattern = r"(\d+)\s*(minute|seconde|hour|min|sec|heure)s?"
    matches = re.findall(pattern, msg, re.IGNORECASE)
    if matches:
        parts = []
        for val, unit in matches:
            u = unit.lower()
            if u.startswith("min"):
                parts.append(f"{val}m")
            elif u.startswith("sec"):
                parts.append(f"{val}s")
            elif u.startswith("hour") or u.startswith("heur"):
                parts.append(f"{val}h")
            else:
                parts.append(f"{val} {unit}")
        return " ".join(parts)

    # Fallback: find any digits
    digits = re.findall(r"\d+", msg)
    if len(digits) >= 2:
        return f"{digits[0]}m {digits[1]}s"
    elif len(digits) == 1:
        return f"{digits[0]}m"

    return "a few minutes"


# ══════════════════════════════════════════════════════════════════════════════
#  Public entry point
# ══════════════════════════════════════════════════════════════════════════════

async def run_global_sync(app, force: bool = False) -> dict[str, Any]:
    """Run the full two-stage global sync. Returns a stats dict."""
    db_path: str = app.bot_data["db_path"]
    api_client = app.bot_data["api_client"]
    admin_id = app.bot_data.get("admin_id")

    mode = "FORCE" if force else "NORMAL"
    logger.info("=== Global Sync START (mode=%s) ===", mode)

    primary_solver, fallback_solver = create_solvers()

    if force:
        profiles = await profile_db.get_all_profiles(db_path)
    else:
        profiles = await profile_db.get_unsynced_profiles(db_path)
    total_profiles = await profile_db.get_total_profile_count(db_path)
    logger.info("Profiles to scan: %d / %d total (mode=%s)", len(profiles), total_profiles, mode)

    stats = {
        "total": total_profiles,
        "scanned": len(profiles),
        "pending": 0,
        "pre_registered": 0,
        "registered_need_audit": 0,
        "registered_no_order": 0,
        "ordered": 0,
        "bad_password": 0,
        "errors": 0,
        "skipped_synced": 0 if force else (total_profiles - len(profiles)),
        "notifications_sent": 0,
        "rate_limit_pauses": 0,
    }
    error_details: list[str] = []

    start_ts = time.monotonic()

    if admin_id:
        try:
            sync_title = "🔄 *Force Global Sync Started*" if force else "🔄 *Global Sync Started*"
            scan_label = "Total to scan (forced)" if force else "Unsynced (to scan)"
            skip_msg = f"\nAlready synced (skipping): *{stats['skipped_synced']}*" if not force else ""
            await app.bot.send_message(
                chat_id=admin_id,
                text=(
                    f"{sync_title}\n\n"
                    f"Total profiles: *{total_profiles}*\n"
                    f"{scan_label}: *{len(profiles)}*{skip_msg}"
                ),
                parse_mode="Markdown",
            )
        except Exception:
            logger.exception("Failed to send sync start message to admin")

    # ── Stage 1 — Light Probe ─────────────────────────────────────────────
    logger.info("--- Stage 1: Light Probe (%d profiles) ---", len(profiles))
    stage2_profiles: list[profile_db.Profile] = []

    for i in range(0, len(profiles), STAGE1_BATCH):
        batch = profiles[i:i + STAGE1_BATCH]
        for p in batch:
            pname = p.name or f"{p.nin[:4]}…{p.nin[-4:]}"
            result, reason = await _probe_nin(api_client, p, app)
            if result == "rate_limited":
                stats["rate_limit_pauses"] += 1
                logger.warning("Rate limited on profile %s (%s) — pausing %ds", p.id, pname, RATE_LIMIT_PAUSE_S)
                await asyncio.sleep(RATE_LIMIT_PAUSE_S)
                result, reason = await _probe_nin(api_client, p, app)

            logger.info("Stage1 profile %s (%s): %s — %s", p.id, pname, result, reason)

            if result == "pre-registered":
                stats["pre_registered"] += 1
            elif result == "registered":
                stats["registered_need_audit"] += 1
                stage2_profiles.append(p)
            elif result == "pending":
                stats["pending"] += 1
            else:
                stats["errors"] += 1
                error_details.append(f"❌ *{pname}*: {reason}")

        # Log batch progress
        done = min(i + STAGE1_BATCH, len(profiles))
        logger.info("Stage 1 progress: %d/%d profiles probed", done, len(profiles))

        if i + STAGE1_BATCH < len(profiles):
            await asyncio.sleep(random.uniform(*STAGE1_JITTER))

    # ── Stage 2 — Deep Audit ──────────────────────────────────────────────
    logger.info("--- Stage 2: Deep Audit (%d profiles need login) ---", len(stage2_profiles))
    for i in range(0, len(stage2_profiles), STAGE2_BATCH):
        batch = stage2_profiles[i:i + STAGE2_BATCH]
        for p in batch:
            pname = p.name or f"{p.nin[:4]}…{p.nin[-4:]}"
            result, reason = await _deep_audit(
                api_client, p, app,
                primary_solver=primary_solver,
                fallback_solver=fallback_solver,
            )
            if result == "rate_limited":
                stats["rate_limit_pauses"] += 1
                logger.warning("Rate limited on profile %s (%s) — pausing %ds", p.id, pname, RATE_LIMIT_PAUSE_S)
                await asyncio.sleep(RATE_LIMIT_PAUSE_S)
                result, reason = await _deep_audit(
                    api_client, p, app,
                    primary_solver=primary_solver,
                    fallback_solver=fallback_solver,
                )

            logger.info("Stage2 profile %s (%s): %s — %s", p.id, pname, result, reason)

            if result == "ordered":
                stats["ordered"] += 1
            elif result == "bad_password":
                stats["bad_password"] += 1
            elif result == "registered_no_order":
                stats["registered_no_order"] += 1
            else:
                stats["errors"] += 1
                error_details.append(f"❌ *{pname}*: {reason}")

        done = min(i + STAGE2_BATCH, len(stage2_profiles))
        logger.info("Stage 2 progress: %d/%d profiles audited", done, len(stage2_profiles))

        if i + STAGE2_BATCH < len(stage2_profiles):
            await asyncio.sleep(random.uniform(*STAGE2_JITTER))

    # ── Send admin summary ────────────────────────────────────────────────
    elapsed = time.monotonic() - start_ts
    elapsed_min = elapsed / 60

    if admin_id:
        try:
            sync_complete_title = "📊 *Force Global Sync Complete*" if force else "📊 *Global Sync Complete*"
            summary = [
                f"{sync_complete_title}\n",
                f"⏱ Duration: *{elapsed_min:.1f} minutes*",
                f"📋 Total profiles in DB: *{stats['total']}*",
                f"🔍 Scanned this run: *{stats['scanned']}*",
                f"⏭ Skipped (already synced): *{stats['skipped_synced']}*\n",
                f"*Status Breakdown:*",
                f"  🆕 Pending: *{stats['pending']}*",
                f"  🔵 Pre-registered: *{stats['pre_registered']}*",
                f"  🟢 Registered (no order): *{stats['registered_no_order']}*",
                f"  🎉 Ordered: *{stats['ordered']}*",
                f"  🔑 Bad password: *{stats['bad_password']}*",
                f"  ❌ Errors: *{stats['errors']}*\n",
                f"⚠️ Rate limit pauses: *{stats['rate_limit_pauses']}*"
            ]
            
            if error_details:
                summary.append("\n📝 *Error Details (top 15):*")
                summary.extend(error_details[:15])
                if len(error_details) > 15:
                    summary.append(f"_(...and {len(error_details)-15} more errors in logs)_")

            await app.bot.send_message(
                chat_id=admin_id, text="\n".join(summary), parse_mode="Markdown"
            )
        except Exception:
            logger.exception("Failed to send sync summary to admin")

    logger.info(
        "=== Global Sync END (mode=%s) — scanned=%d pending=%d pre_reg=%d "
        "registered_no_order=%d ordered=%d bad_pw=%d errors=%d "
        "rate_pauses=%d elapsed=%.1fmin ===",
        mode, stats["scanned"], stats["pending"], stats["pre_registered"],
        stats["registered_no_order"], stats["ordered"], stats["bad_password"],
        stats["errors"], stats["rate_limit_pauses"], elapsed_min,
    )
    return stats


# ══════════════════════════════════════════════════════════════════════════════
#  Stage 1 — Light Probe
# ══════════════════════════════════════════════════════════════════════════════

async def _probe_nin(api_client, profile: profile_db.Profile, app) -> tuple[str, str]:
    """POST resend-otp and interpret the response.

    Returns: (status, reason)
    """
    db_path: str = app.bot_data["db_path"]
    client = api_client.create_session()

    try:
        try:
            resp = await client.post(
                "/api/v1/citizens/resend-otp",
                json={"nin": profile.nin},
                headers=_REG_HEADERS,
                timeout=20.0,
            )
        except httpx.TimeoutException:
            return "error", "Network Timeout"
        except Exception as exc:
            logger.error("Stage 1 network error for profile %s: %s", profile.id, exc)
            return "error", str(exc)

        if resp.status_code == 429:
            return "rate_limited", "Rate limit"

        if 200 <= resp.status_code < 300:
            await profile_db.set_profile_status(db_path, profile.id, "pre-registered")
            await profile_db.mark_profile_synced(db_path, profile.id)
            await _notify_pre_registered_fresh(app, profile)
            return "pre-registered", "Fresh OTP sent"

        try:
            body = resp.json()
            error_msg = body.get("message", resp.text)
        except Exception:
            error_msg = resp.text or ""

        if "déjà été envoyé" in error_msg:
            remaining_time = _parse_remaining_time(error_msg)
            await profile_db.set_profile_status(db_path, profile.id, "pre-registered")
            await profile_db.mark_profile_synced(db_path, profile.id)
            await _notify_pre_registered_cooldown(app, profile, remaining_time)
            return "pre-registered", f"OTP already sent (wait {remaining_time})"

        if "Compte déjà actif" in error_msg:
            await profile_db.set_profile_status(db_path, profile.id, "registered")
            return "registered", "Active account (needs audit)"

        # 404 or other 4xx usually means not registered
        # "Aucun OTP d'inscription actif trouvé" = no active registration OTP → never registered
        if resp.status_code == 404 or any(kw in error_msg.lower() for kw in (
            "not found", "aucune inscription", "no registration", "aucun otp"
        )):
            await profile_db.set_profile_status(db_path, profile.id, "pending")
            await profile_db.mark_profile_synced(db_path, profile.id)
            return "pending", "Profile not found on server"

        # If we got a 500 or unknown 4xx, it's an error (don't mark synced)
        return "error", f"Server returned {resp.status_code}: {error_msg[:50]}"

    finally:
        await client.aclose()


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
) -> tuple[str, str]:
    """Login and check orders with cascading CAPTCHA logic.

    Returns: (status, reason)
    """
    db_path: str = app.bot_data["db_path"]
    client = api_client.create_session()

    try:
        access_token = None
        last_error = "Unknown error"

        # Cascade loop: 3 cycles of (3x ddddocr -> 1x 2captcha)
        for cycle in range(3):
            for attempt in range(4): # 0,1,2 = ddddocr; 3 = 2captcha
                solver = primary_solver if attempt < 3 else fallback_solver
                if solver is None:
                    continue

                # 1. Generate CAPTCHA
                try:
                    captcha_resp = await client.get(
                        "/api/v1/captcha/generate", headers=_REG_HEADERS, timeout=15.0
                    )
                    if captcha_resp.status_code == 429:
                        return "rate_limited", "Rate limit"
                    captcha_resp.raise_for_status()
                    cdata = captcha_resp.json()
                    captcha_id = cdata["captchaId"]
                    img_uri = cdata["captchaImage"]
                    b64 = img_uri.split(",", 1)[1] if "," in img_uri else img_uri
                    img_bytes = base64.b64decode(b64)
                except Exception as exc:
                    last_error = f"Captcha gen failed: {exc}"
                    continue

                # 2. Solve
                try:
                    answer = await solver.solve(img_bytes)
                except Exception as exc:
                    last_error = f"Solver {solver.name} failed: {exc}"
                    continue

                # 3. Login
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
                        timeout=25.0,
                    )
                except Exception as exc:
                    last_error = f"Login request failed: {exc}"
                    continue

                if login_resp.status_code == 429:
                    return "rate_limited", "Rate limit"

                if 200 <= login_resp.status_code < 300:
                    access_token = login_resp.json().get("token")
                    break

                # 4. Handle Login Failure
                try:
                    body = login_resp.json()
                    msg = body.get("message", login_resp.text)
                except Exception:
                    msg = login_resp.text or ""
                
                last_error = msg

                # Check for "Not Found" error in login (Server returns 400 with specific message)
                if any(kw in msg.lower() for kw in ("aucune inscription", "not found", "no registration")):
                    await profile_db.set_profile_status(db_path, profile.id, "pending")
                    await profile_db.mark_profile_synced(db_path, profile.id)
                    return "pending", "No registration found (reset to pending)"

                # Check for terminal errors (Bad Password)
                if any(kw in msg.lower() for kw in ("mot de passe", "password", "incorrect", "invalid credentials")):
                    await profile_db.set_profile_invalid(db_path, profile.id)
                    await profile_db.set_profile_status(db_path, profile.id, "registered")
                    await _notify_bad_password(app, profile)
                    return "bad_password", "Invalid password"
                
                # Check for account lockout
                if any(kw in msg.lower() for kw in ("verrouillé", "locked", "too many attempts")):
                    return "error", f"Account Locked: {msg[:40]}"

                # If it's a captcha error, just continue the loop
                await asyncio.sleep(1)
            
            if access_token:
                break
            
            # End of cyclejitter
            await asyncio.sleep(2)

        if not access_token:
            return "error", f"Login failed: {last_error[:50]}"

        # ── Fetch orders ──────────────────────────────────────────────────
        order_headers = {
            **_REG_HEADERS,
            "Authorization": f"Bearer {access_token}",
        }

        try:
            orders_resp = await client.get(
                "/api/v1/orders/my-orders?page=0&size=10",
                headers=order_headers,
                timeout=20.0,
            )
        except Exception as exc:
            return "error", f"Orders fetch failed: {exc}"

        if orders_resp.status_code == 429:
            return "rate_limited", "Rate limit"

        if 200 <= orders_resp.status_code < 300:
            orders_data = orders_resp.json()
            recent = orders_data.get("recentOrders", [])
            has_pending = any(o.get("status") == "PENDING" for o in recent)

            if has_pending:
                await profile_db.set_profile_status(db_path, profile.id, "ordered")
                await profile_db.mark_profile_synced(db_path, profile.id)
                await _notify_ordered(app, profile)
                return "ordered", "Active order found"
            else:
                await profile_db.set_profile_status(db_path, profile.id, "registered")
                await profile_db.mark_profile_synced(db_path, profile.id)
                await _notify_no_order(app, profile)
                return "registered_no_order", "Account active, no order"
        else:
            return "error", f"Orders API returned {orders_resp.status_code}"

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
    text = t(lang, msg_key).format(
        name=escape_markdown(pname, version=1),
        nin=escape_markdown(profile.nin, version=1)
    )
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
        name=escape_markdown(pname, version=1),
        nin=escape_markdown(profile.nin, version=1),
        remaining_time=remaining_time
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
    text = t(lang, msg_key).format(
        name=escape_markdown(pname, version=1),
        nin=escape_markdown(profile.nin, version=1)
    )
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
    text = t(lang, msg_key).format(
        name=escape_markdown(pname, version=1),
        nin=escape_markdown(profile.nin, version=1)
    )
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
    text = t(lang, msg_key).format(name=escape_markdown(pname, version=1))
    await safe_send_message(app.bot, profile.user_id, db_path=db_path,
                            text=text, parse_mode="Markdown")
