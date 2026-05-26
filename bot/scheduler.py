from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import timedelta

from . import db as db_mod
from . import profile_db
from .api_client import QuotaApiClient, QuotaStatus
from .auto_registration import auto_submit_profiles, remind_preregistered_profiles
from .notifier import notify_users
from .i18n import t
from .db import get_user_language
from .admin import ADMIN_TELEGRAM_ID

logger = logging.getLogger(__name__)


async def _poll_once(
    *,
    app,
    db_path: str,
    api_client: QuotaApiClient,
) -> None:
    logger.info("--- Starting scheduled quota poll ---")
    from .proxy import get_proxy_url
    use_proxy = app.bot_data.get("proxy_wilaya", False)
    proxy_url = get_proxy_url() if use_proxy else None
    
    if use_proxy:
        logger.debug("Using proxy for this poll: %s", proxy_url)

    try:
        logger.debug("Fetching wilaya quotas from API...")
        statuses = await api_client.fetch_wilaya_quotas(proxy_url=proxy_url)
        logger.debug("API fetch complete. Found %d wilayas.", len(statuses))
        now = datetime.now(ZoneInfo("Africa/Algiers")).isoformat()

        if statuses:
            # Stamp the last successful fetch timestamp so /fetchinfo can report it
            app.bot_data["last_fetch_ts"] = now
        else:
            logger.warning("Quota poll returned no data, skipping update cycle.")
            return

        # Update last-known cache and record history
        last_known: dict[str, QuotaStatus] = app.bot_data.setdefault("last_known", {})
        for code, status in statuses.items():
            prev = last_known.get(code)
            if prev is not None:
                if status.available and not prev.available:
                    # Became available: record OPEN
                    logger.info("Recording history: wilaya=%s OPENED", code)
                    await db_mod.add_quota_history_entry(db_path, code, "OPEN")
                elif not status.available and prev.available:
                    # Became unavailable: record CLOSE
                    logger.info("Recording history: wilaya=%s CLOSED", code)
                    await db_mod.add_quota_history_entry(db_path, code, "CLOSE")
            
            last_known[code] = status

        # If startup couldn't fetch wilayas, populate the inline keyboard source
        if not app.bot_data.get("wilayas"):
            try:
                items = [(s.wilaya_code, s.wilaya_name) for s in statuses.values()]
                items.sort(key=lambda t: (t[0], t[1]))
                app.bot_data["wilayas"] = items
                # Also save to DB cache
                wilaya_dicts = [{"code": code, "name": name} for code, name in items]
                await db_mod.save_wilayas(db_path, wilaya_dicts)
                logger.info("Populated wilaya list and saved to DB from scheduler payload")
            except Exception:
                logger.exception("Failed updating wilaya list from scheduler payload")

        subscribed_wilayas = set(await db_mod.get_distinct_wilayas(db_path))
        profile_wilayas = set(await profile_db.get_distinct_profile_wilayas(db_path))
        watched_wilayas = subscribed_wilayas | profile_wilayas
        if not watched_wilayas:
            return

        for wilaya_code in watched_wilayas:
            status = statuses.get(wilaya_code)
            if status is None:
                logger.debug("No status found for wilaya=%s at %s", wilaya_code, now)
                continue

            logger.debug(
                "Quota check wilaya=%s available=%s remaining=%s at=%s",
                wilaya_code,
                status.available,
                status.remaining,
                now,
            )

            if status.available:
                to_notify = await db_mod.get_subscribers_to_notify(db_path, wilaya_code)
                if to_notify:
                    remaining_txt = "unknown" if status.remaining is None else str(status.remaining)
                    # Background the notification task to avoid blocking the polling loop
                    asyncio.create_task(notify_users(
                        app.bot, to_notify, 
                        "✅ Quota available in {wilaya_name}! Remaining: {remaining} units.", 
                        db_path=db_path, 
                        format_kwargs={"wilaya_name": status.wilaya_name, "remaining": remaining_txt}
                    ))
                    await db_mod.mark_notified(db_path, to_notify, wilaya_code)

                # Auto-registration: trigger every poll if actionable profiles exist (Aggressive Mode)
                if app.bot_data.get("autoreg_enabled", True):
                    try:
                        actionable_profiles = await profile_db.get_actionable_profiles_prioritized(
                            db_path, wilaya_code, ["pending", "registered", "pre-registered"],
                            priority_user_id=ADMIN_TELEGRAM_ID
                        )
                        if actionable_profiles:
                            logger.info(
                                "Found %d actionable profiles for wilaya %s — triggering auto-registration",
                                len(actionable_profiles),
                                wilaya_code,
                            )
                            asyncio.create_task(auto_submit_profiles(app, actionable_profiles))
                    except Exception:
                        logger.exception("Auto-registration trigger failed for wilaya %s", wilaya_code)
                else:
                    logger.debug("Auto-registration is disabled globally, skipping trigger for wilaya %s", wilaya_code)
            else:
                # Notify users that the quota they were alerted about is now gone.
                previously_notified = await db_mod.get_notified_subscribers(db_path, wilaya_code)
                if previously_notified:
                    wilaya_name = status.wilaya_name if status else wilaya_code
                    # Background the notification task
                    asyncio.create_task(notify_users(
                        app.bot, previously_notified, 
                        "❌ Quota in {wilaya_name} is no longer available.", 
                        db_path=db_path, 
                        format_kwargs={"wilaya_name": wilaya_name}
                    ))
                await db_mod.reset_notified_for_wilaya(db_path, wilaya_code)
    except Exception as e:
        import httpx
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (429, 500, 502, 503, 504):
            # --- Fail-Safe Strategy for Rate Limits or Server Errors ---
            current_interval = app.bot_data.get("check_interval_seconds", 30)
            # Increase interval if it's a 429, or if 500s are persistent
            new_interval = int(current_interval * 1.5)
            
            # Cap the interval at 10 minutes to avoid stopping the bot indefinitely
            if new_interval > 600:
                new_interval = 600

            # Ensure it actually increases (at least by 5s)
            if new_interval <= current_interval + 5:
                new_interval = current_interval + 5

            app.bot_data["check_interval_seconds"] = new_interval
            update_poll_interval(app, new_interval)

            status_code = e.response.status_code
            logger.warning(
                "Critical API error (HTTP %d). Throttling: %ds -> %ds",
                status_code,
                current_interval,
                new_interval,
            )

            # Notify admin
            admin_id = app.bot_data.get("admin_id")
            if admin_id:
                try:
                    error_text = str(e)
                    try:
                        resp_json = e.response.json()
                        if "message" in resp_json:
                            error_text = resp_json["message"]
                        elif "error" in resp_json:
                            error_text = resp_json["error"]
                    except Exception:
                        pass

                    emoji = "🚨" if status_code == 429 else "⚠️"
                    reason = "Rate Limit" if status_code == 429 else "Server Error"
                    
                    notif_msg = (
                        f"{emoji} *Fail-Safe Triggered ({reason})*\n\n"
                        f"The bot encountered an error (HTTP {status_code}) while checking quotas.\n\n"
                        f"💬 *Error:* `{error_text}`\n\n"
                        "🔄 *Action:* Automatically increased check interval.\n"
                        f"• Old interval: `{current_interval}s`\n"
                        f"• New interval: `{new_interval}s`"
                    )
                    await app.bot.send_message(
                        chat_id=admin_id, text=notif_msg, parse_mode="Markdown"
                    )
                except Exception:
                    logger.exception("Failed to notify admin about fail-safe trigger")
        else:
            logger.exception("Scheduler poll failed")

async def remove_excess_profiles_job(app, db_path: str) -> None:
    logger.info("Running job to remove excess profiles (limit: 3).")
    try:
        user_profiles = await profile_db.get_all_profiles_grouped_by_user(db_path)
        removed_count = 0
        for user_id, profiles in user_profiles.items():
            if len(profiles) > 3 and user_id != ADMIN_TELEGRAM_ID:
                excess_profiles = profiles[3:]
                for p in excess_profiles:
                    await profile_db.delete_profile(db_path, p.id, user_id)
                    removed_count += 1
                try:
                    lang = await get_user_language(db_path, user_id)
                    await app.bot.send_message(
                        chat_id=user_id,
                        text=t(lang, "⚠️ *Profiles Removed*\n\nAs notified, {count} of your excess profiles have been automatically removed to enforce the 3-profile limit.").format(count=len(excess_profiles)),
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("Failed to send profile removal notice to user %s", user_id)
        logger.info("Removed %d excess profiles in total.", removed_count)
    except Exception:
        logger.exception("Failed running excess profile removal job.")


async def send_inbox_report_job(app) -> None:
    """Send a summary of unresolved inbox entries to the admin."""
    logger.info("Running periodic inbox summary report job...")
    db_path = app.bot_data.get("db_path", "")
    admin_id = app.bot_data.get("admin_id")
    
    if not admin_id:
        return

    try:
        # Count unresolved entries
        error_count = await db_mod.count_inbox_entries(db_path, level="ERROR", status="unresolved")
        warning_count = await db_mod.count_inbox_entries(db_path, level="WARNING", status="unresolved")
        
        if error_count == 0 and warning_count == 0:
            logger.info("No unresolved entries to report.")
            return

        text = (
            "📊 *Periodic Inbox Summary*\n\n"
            f"You have new unresolved items in the error inbox:\n"
            f"• 🔴 ERRORS: *{error_count}*\n"
            f"• ⚠️ WARNINGS: *{warning_count}*\n\n"
            "Please check the admin panel to view and resolve them."
        )
        
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 View Inbox", callback_data="admin:inbox:0")],
            [InlineKeyboardButton("📨 Inbox Settings", callback_data="admin:inbox_settings")]
        ])

        await app.bot.send_message(
            chat_id=admin_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        logger.info("Inbox summary report sent to admin.")
    except Exception:
        logger.exception("Failed to run inbox summary report job")


def update_inbox_report_interval(app, interval_mins: int):
    """Start or reschedule the periodic inbox report job."""
    scheduler = app.bot_data.get("scheduler")
    if not scheduler:
        return

    try:
        if scheduler.get_job("inbox_report"):
            scheduler.reschedule_job("inbox_report", trigger='interval', minutes=interval_mins)
            logger.info("Rescheduled inbox_report to every %d minutes", interval_mins)
        else:
            scheduler.add_job(
                send_inbox_report_job,
                "interval",
                args=[app],
                minutes=interval_mins,
                id="inbox_report",
                max_instances=1,
                misfire_grace_time=300
            )
            logger.info("Started inbox_report every %d minutes", interval_mins)
    except Exception:
        logger.exception("Failed to update inbox_report job")


def stop_inbox_report_job(app):
    """Remove the periodic inbox report job."""
    scheduler = app.bot_data.get("scheduler")
    if scheduler and scheduler.get_job("inbox_report"):
        try:
            scheduler.remove_job("inbox_report")
            logger.info("Stopped inbox_report job.")
        except Exception:
            logger.exception("Failed to stop inbox_report job")


def update_poll_interval(app, new_interval_s: int):
    """Reschedule the quota poll job with a new interval."""
    scheduler = app.bot_data.get("scheduler")
    if scheduler:
        try:
            scheduler.reschedule_job("quota_poll", trigger='interval', seconds=new_interval_s)
            logger.info("Rescheduled quota_poll to every %ds", new_interval_s)
        except Exception:
            logger.exception("Failed to reschedule quota_poll job")

# --- Periodic Job Wrappers ---

async def reminder_wrapper(app):
    """Bridge for the pre-registered profile reminder job."""
    await remind_preregistered_profiles(app)

async def excess_profiles_wrapper(app, db_path: str):
    """Bridge for the excess profile removal job."""
    await remove_excess_profiles_job(app, db_path)

def start_scheduler(
    *,
    app,
    db_path: str,
    api_client: QuotaApiClient,
    interval_s: int,
) -> AsyncIOScheduler:
    """Initialize and start the background task scheduler."""
    
    # Bind scheduler to the currently running application event loop.
    scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())

    async def quota_poll_wrapper():
        """Main loop for wilaya quota monitoring."""
        import time
        start_ts = time.perf_counter()
        try:
            await _poll_once(
                app=app,
                db_path=db_path,
                api_client=api_client,
            )
        finally:
            elapsed = time.perf_counter() - start_ts
            logger.info("--- Quota poll cycle completed in %.3fs ---", elapsed)

    # 1. Quota Polling Job
    scheduler.add_job(
        quota_poll_wrapper,
        "interval",
        seconds=interval_s,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        id="quota_poll",
    )

    # 2. 12-hour OTP Verification Reminder
    scheduler.add_job(
        reminder_wrapper,
        "interval",
        args=[app],
        hours=12,
        max_instances=1,
        misfire_grace_time=300,
        id="otp_reminder",
    )

    # 3. Excess Profile Removal (scheduled for a specific date or shortly after startup)
    fixed_removal_date = datetime(2026, 5, 10, 10, 0, 0, tzinfo=ZoneInfo("Africa/Algiers"))
    
    if datetime.now(ZoneInfo("Africa/Algiers")) < fixed_removal_date:
        scheduler.add_job(
            excess_profiles_wrapper,
            "date",
            run_date=fixed_removal_date,
            args=[app, db_path],
            misfire_grace_time=3600 * 24, # 24h grace
            id="excess_removal",
        )
    else:
        # If we missed the deadline (bot rebooted after), run it shortly after startup
        scheduler.add_job(
            excess_profiles_wrapper,
            "date",
            run_date=datetime.now(ZoneInfo("Africa/Algiers")) + timedelta(seconds=15),
            args=[app, db_path],
            misfire_grace_time=3600 * 24,  # 24h grace
            id="excess_removal_catchup",
        )

    scheduler.start()
    logger.info("Scheduler started: poll every %ss, pre-registered reminder every 12h", interval_s)

    # Start inbox report if real-time notifications are muted
    realtime = app.bot_data.get("inbox_realtime_enabled", True)
    if not realtime:
        interval_mins = app.bot_data.get("inbox_report_interval_mins", 60)
        update_inbox_report_interval(app, interval_mins)

    # Load persisted sync schedules from DB
    asyncio.create_task(_load_persisted_sync_schedules(app, db_path))

    return scheduler


# ---------------------------------------------------------------------------
# Global sync schedule management
# ---------------------------------------------------------------------------

async def _global_sync_wrapper(app) -> None:
    """Run the global sync, guarded against duplicate executions."""
    if app.bot_data.get("global_sync_running"):
        logger.info("Skipping scheduled global sync — already running")
        return

    app.bot_data["global_sync_running"] = True
    try:
        from .sync import run_global_sync
        await run_global_sync(app)
    except Exception:
        logger.exception("Scheduled global sync failed")
    finally:
        app.bot_data["global_sync_running"] = False


async def _load_persisted_sync_schedules(app, db_path: str) -> None:
    """Load and register all active sync schedules from DB on startup."""
    try:
        schedules = await db_mod.get_active_sync_schedules(db_path)
        for s in schedules:
            add_sync_job(
                app,
                s["id"],
                s["schedule_type"],
                interval_s=s.get("interval_seconds"),
                run_at=s.get("run_at"),
            )
        if schedules:
            logger.info("Loaded %d persisted sync schedule(s)", len(schedules))
    except Exception:
        logger.exception("Failed to load persisted sync schedules")


def add_sync_job(
    app,
    schedule_id: int,
    schedule_type: str,
    *,
    interval_s: int | None = None,
    run_at: str | None = None,
) -> None:
    """Add a sync job to the running scheduler."""
    scheduler = app.bot_data.get("scheduler")
    if not scheduler:
        return

    job_id = f"global_sync_{schedule_id}"

    # Remove existing job with same ID if any
    if scheduler.get_job(job_id):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass

    try:
        if schedule_type == "interval" and interval_s:
            scheduler.add_job(
                _global_sync_wrapper,
                "interval",
                args=[app],
                seconds=interval_s,
                id=job_id,
                max_instances=1,
                misfire_grace_time=3600,
                coalesce=True,
            )
            logger.info("Added sync interval job %s: every %ds", job_id, interval_s)
        elif schedule_type == "once" and run_at:
            from datetime import datetime as _dt
            from zoneinfo import ZoneInfo as _ZI
            dt = _dt.fromisoformat(run_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_ZI("Africa/Algiers"))
            if dt > _dt.now(_ZI("Africa/Algiers")):
                scheduler.add_job(
                    _global_sync_wrapper,
                    "date",
                    args=[app],
                    run_date=dt,
                    id=job_id,
                    misfire_grace_time=3600 * 24,
                )
                logger.info("Added one-time sync job %s: at %s", job_id, run_at)
            else:
                logger.info("Skipping past one-time sync job %s (was %s)", job_id, run_at)
    except Exception:
        logger.exception("Failed to add sync job %s", job_id)


def remove_all_sync_jobs(app) -> None:
    """Remove all global_sync_* jobs from the running scheduler."""
    scheduler = app.bot_data.get("scheduler")
    if not scheduler:
        return
    jobs = scheduler.get_jobs()
    for job in jobs:
        if job.id.startswith("global_sync_"):
            try:
                scheduler.remove_job(job.id)
                logger.info("Removed sync job %s", job.id)
            except Exception:
                logger.exception("Failed to remove sync job %s", job.id)

