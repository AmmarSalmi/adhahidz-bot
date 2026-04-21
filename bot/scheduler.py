from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import db as db_mod
from .api_client import QuotaApiClient, QuotaStatus
from .notifier import notify_users

logger = logging.getLogger(__name__)


async def _confirm_available(
    *,
    api_client: QuotaApiClient,
    wilaya_code: str,
    confirm_fetches: int,
    confirm_delay_s: float,
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

        statuses = await api_client.fetch_wilaya_quotas()
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
    try:
        statuses = await api_client.fetch_wilaya_quotas()
        now = datetime.now(timezone.utc).isoformat()

        # Update last-known cache
        last_known: dict[str, QuotaStatus] = app.bot_data.setdefault("last_known", {})
        for code, status in statuses.items():
            last_known[code] = status

        # If startup couldn't fetch wilayas, populate the inline keyboard source
        if not app.bot_data.get("wilayas"):
            try:
                items = [(s.wilaya_code, s.wilaya_name) for s in statuses.values()]
                items.sort(key=lambda t: (t[0], t[1]))
                app.bot_data["wilayas"] = items
            except Exception:
                logger.exception("Failed updating wilaya list from scheduler payload")

        subscribed_wilayas = await db_mod.get_distinct_wilayas(db_path)
        if not subscribed_wilayas:
            return

        for wilaya_code in subscribed_wilayas:
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
                )
                if not confirmed:
                    continue

                to_notify = await db_mod.get_subscribers_to_notify(db_path, wilaya_code)
                if not to_notify:
                    continue

                remaining_txt = "unknown" if status.remaining is None else str(status.remaining)
                msg = f"✅ Quota available in {status.wilaya_name}! Remaining: {remaining_txt} units."
                await notify_users(app.bot, to_notify, msg)
                await db_mod.mark_notified(db_path, to_notify, wilaya_code)
            else:
                # Notify users that the quota they were alerted about is now gone.
                previously_notified = await db_mod.get_notified_subscribers(db_path, wilaya_code)
                if previously_notified:
                    wilaya_name = status.wilaya_name if status else wilaya_code
                    gone_msg = f"❌ Quota in {wilaya_name} is no longer available."
                    await notify_users(app.bot, previously_notified, gone_msg)
                await db_mod.reset_notified_for_wilaya(db_path, wilaya_code)
    except Exception:
        logger.exception("Scheduler poll failed")


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

    scheduler.add_job(job_wrapper, "interval", seconds=interval_s, max_instances=1)
    scheduler.start()
    return scheduler
