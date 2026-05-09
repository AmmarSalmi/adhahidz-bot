from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import timedelta

from . import db as db_mod
from . import profile_db
from .api_client import QuotaApiClient, QuotaStatus
from .auto_registration import auto_submit_profiles, remind_preregistered_profiles
from .notifier import notify_users

logger = logging.getLogger(__name__)


async def _confirm_available(
    *,
    api_client: QuotaApiClient,
    wilaya_code: str,
    confirm_fetches: int,
    confirm_delay_s: float,
    proxy_url: str | None = None,
) -> bool:
    """
    Confirm that a wilaya remains available across successive fetches.
    This helps reduce false positives from transient/cached API responses.
    """
    if confirm_fetches <= 0:
        return True

    for i in range(confirm_fetches):
        if confirm_delay_s > 0:
            await asyncio.sleep(confirm_delay_s)

        statuses = await api_client.fetch_wilaya_quotas(proxy_url=proxy_url)
        st = statuses.get(wilaya_code)
        if not st or not st.available:
            logger.info(
                "Availability confirmation failed wilaya=%s attempt=%s/%s",
                wilaya_code,
                i + 1,
                confirm_fetches,
            )
            return False

    logger.debug("Availability confirmed wilaya=%s confirmations=%s", wilaya_code, confirm_fetches)
    return True


async def _poll_once(
    *,
    app,
    db_path: str,
    api_client: QuotaApiClient,
    confirm_fetches: int,
    confirm_delay_s: float,
) -> None:
    from .proxy import get_proxy_url
    use_proxy = app.bot_data.get("proxy_wilaya", False)
    proxy_url = get_proxy_url() if use_proxy else None

    try:
        statuses = await api_client.fetch_wilaya_quotas(proxy_url=proxy_url)
        now = datetime.now(timezone.utc).isoformat()

        # Stamp the last successful fetch timestamp so /fetchinfo can report it
        app.bot_data["last_fetch_ts"] = now

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
                confirmed = await _confirm_available(
                    api_client=api_client,
                    wilaya_code=wilaya_code,
                    confirm_fetches=confirm_fetches,
                    confirm_delay_s=confirm_delay_s,
                    proxy_url=proxy_url,
                )
                if not confirmed:
                    continue

                to_notify = await db_mod.get_subscribers_to_notify(db_path, wilaya_code)
                if to_notify:
                    remaining_txt = "unknown" if status.remaining is None else str(status.remaining)
                    msg = f"✅ Quota available in {status.wilaya_name}! Remaining: {remaining_txt} units."
                    await notify_users(app.bot, to_notify, msg)
                    await db_mod.mark_notified(db_path, to_notify, wilaya_code)

                # Auto-registration: trigger only once per availability window
                auto_reg_done: set[str] = app.bot_data.setdefault("auto_reg_done", set())
                if wilaya_code in auto_reg_done:
                    logger.debug(
                        "Auto-registration already triggered for wilaya %s this window — skipping",
                        wilaya_code,
                    )
                else:
                    try:
                        actionable_profiles = await profile_db.get_actionable_profiles_prioritized(
                            db_path, wilaya_code, ["pending", "registered", "pre-registered"]
                        )
                        if actionable_profiles:
                            logger.info(
                                "Found %d actionable profiles for wilaya %s — triggering auto-registration",
                                len(actionable_profiles),
                                wilaya_code,
                            )
                            await auto_submit_profiles(app, actionable_profiles)
                        auto_reg_done.add(wilaya_code)
                    except Exception:
                        logger.exception("Auto-registration trigger failed for wilaya %s", wilaya_code)
            else:
                # Notify users that the quota they were alerted about is now gone.
                previously_notified = await db_mod.get_notified_subscribers(db_path, wilaya_code)
                if previously_notified:
                    wilaya_name = status.wilaya_name if status else wilaya_code
                    gone_msg = f"❌ Quota in {wilaya_name} is no longer available."
                    await notify_users(app.bot, previously_notified, gone_msg)
                await db_mod.reset_notified_for_wilaya(db_path, wilaya_code)
                # Reset auto-registration gate so it re-triggers next time quota opens
                auto_reg_done: set[str] = app.bot_data.setdefault("auto_reg_done", set())
                auto_reg_done.discard(wilaya_code)
    except Exception:
        logger.exception("Scheduler poll failed")

async def remove_excess_profiles_job(app, db_path: str) -> None:
    logger.info("Running job to remove excess profiles (limit: 3).")
    try:
        user_profiles = await profile_db.get_all_profiles_grouped_by_user(db_path)
        removed_count = 0
        for user_id, profiles in user_profiles.items():
            if len(profiles) > 3:
                excess_profiles = profiles[3:]
                for p in excess_profiles:
                    await profile_db.delete_profile(db_path, p.id, user_id)
                    removed_count += 1
                try:
                    await app.bot.send_message(
                        chat_id=user_id,
                        text=f"⚠️ *Profiles Removed*\n\nAs notified, {len(excess_profiles)} of your excess profiles have been automatically removed to enforce the 3-profile limit.",
                        parse_mode="Markdown",
                    )
                except Exception:
                    logger.exception("Failed to send profile removal notice to user %s", user_id)
        logger.info("Removed %d excess profiles in total.", removed_count)
    except Exception:
        logger.exception("Failed running excess profile removal job.")



def start_scheduler(
    *,
    app,
    db_path: str,
    api_client: QuotaApiClient,
    interval_s: int,
    confirm_fetches: int = 2,
    confirm_delay_s: float = 1.0,
) -> AsyncIOScheduler:
    # Bind scheduler to the currently running application event loop.
    # If we schedule a *sync* callable, APScheduler may run it in a worker thread
    # (no asyncio loop). Scheduling the coroutine directly keeps execution on
    # the app loop and works reliably on Windows + Docker.
    scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())

    async def job_wrapper():
        # APScheduler calls a normal callable; we bridge into async.
        await _poll_once(
            app=app,
            db_path=db_path,
            api_client=api_client,
            confirm_fetches=confirm_fetches,
            confirm_delay_s=confirm_delay_s,
        )

    scheduler.add_job(
        job_wrapper,
        "interval",
        seconds=interval_s,
        max_instances=1,
        misfire_grace_time=60,
    )

    # 12-hour reminder for pre-registered profiles needing OTP verification
    async def reminder_wrapper():
        await remind_preregistered_profiles(app)

    scheduler.add_job(
        reminder_wrapper,
        "interval",
        hours=12,
        max_instances=1,
        misfire_grace_time=300,
    )

    async def excess_profiles_wrapper():
        await remove_excess_profiles_job(app, db_path)

    # Fixed time: May 10, 2026 at 10:00 AM Algeria time (UTC+1)
    alg_tz = timezone(timedelta(hours=1))
    fixed_removal_date = datetime(2026, 5, 10, 10, 0, 0, tzinfo=alg_tz)
    
    if datetime.now(timezone.utc) < fixed_removal_date:
        scheduler.add_job(
            excess_profiles_wrapper,
            "date",
            run_date=fixed_removal_date,
            misfire_grace_time=3600 * 24, # 24h grace
        )
    else:
        # If we missed it (bot rebooted after deadline), run it shortly after startup
        scheduler.add_job(
            excess_profiles_wrapper,
            "date",
            run_date=datetime.now(timezone.utc) + timedelta(seconds=15),
        )

    scheduler.start()
    logger.info("Scheduler started: poll every %ss, pre-registered reminder every 12h", interval_s)
    return scheduler
