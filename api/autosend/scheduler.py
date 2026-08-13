"""Shared APScheduler instance.

One AsyncIOScheduler for the whole app - main.py already runs the
registration poller on it; campaign sends (this module) share the same
instance rather than running a second scheduler in the process.

Campaign jobs use APScheduler's default in-memory job store, not a DB-backed
one. That's deliberate: storage.campaigns already persists everything a job
needs to be reconstructed (scheduled_at, payload_json), so on startup we
just re-read pending campaigns and re-register their jobs (see
reload_pending_campaigns), rather than keeping two sources of truth in sync.

Note: AsyncIOScheduler runs plain (non-async) job functions through its
default ThreadPoolExecutor, not on the event loop - so launch_scheduled_
campaign (which blocks on synchronous HTTP calls and time.sleep via
_run_campaign) does not block the rest of the app while a campaign sends.
"""
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from autosend import storage
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

scheduler = AsyncIOScheduler()


def _job_id(campaign_id: int) -> str:
    return f"campaign-{campaign_id}"


def schedule_campaign(campaign_id: int, scheduled_at_iso: str) -> None:
    """Registers (or re-registers) the send job for a scheduled campaign."""
    # Local import: campaign_runner imports schedule_campaign from this
    # module at call time, so importing it back here at module load time
    # would be circular. Deferring to call time breaks that cycle.
    from autosend.web.campaign_runner import launch_scheduled_campaign

    run_time = datetime.fromisoformat(scheduled_at_iso)
    scheduler.add_job(
        launch_scheduled_campaign,
        trigger=DateTrigger(run_date=run_time),
        args=[campaign_id],
        id=_job_id(campaign_id),
        replace_existing=True,
        # No misfire cutoff: if the app was down past scheduled_at, fire as
        # soon as it's back up rather than silently dropping the send.
        misfire_grace_time=None,
    )


def cancel_scheduled_job(campaign_id: int) -> None:
    job = scheduler.get_job(_job_id(campaign_id))
    if job:
        job.remove()


def reload_pending_campaigns() -> None:
    """Called once on app startup (after scheduler.start()) to re-register
    jobs for any campaign that was scheduled but hadn't fired before the
    last restart. Also (re-)registers the recurring throttle-recheck job -
    doing both here means one startup call wires up everything the
    scheduler needs to know about campaigns."""
    pending = storage.list_pending_scheduled_campaigns()
    for campaign in pending:
        schedule_campaign(campaign["id"], campaign["scheduled_at"])
    logger.info("Re-registered %d pending scheduled campaign(s)", len(pending))

    _schedule_throttle_recheck()


THROTTLE_RECHECK_JOB_ID = "campaign-throttle-recheck"
THROTTLE_RECHECK_INTERVAL_MINUTES = 15


def _schedule_throttle_recheck() -> None:
    scheduler.add_job(
        recheck_throttled_campaigns,
        trigger=IntervalTrigger(minutes=THROTTLE_RECHECK_INTERVAL_MINUTES),
        id=THROTTLE_RECHECK_JOB_ID,
        replace_existing=True,
        # Fire once shortly after registration too, not just after the
        # first full interval - a campaign throttled right after a
        # deploy/restart shouldn't have to wait a full 15 minutes for its
        # first recheck.
        next_run_time=datetime.now(),
    )


def recheck_throttled_campaigns() -> None:
    """Runs every THROTTLE_RECHECK_INTERVAL_MINUTES. Every campaign sitting
    at status='throttled' just re-attempts its own gate check as a normal
    side effect of launch_scheduled_campaign -> _run_campaign calling
    whatsapp_limits.gate_send() again before its first send; if capacity is
    still exhausted it throttles itself right back to 'throttled' with the
    same (now possibly-shrunk) remaining rows. No separate "is there
    capacity" pre-check is needed here - that would just duplicate the gate
    logic _run_campaign already applies per-row."""
    from autosend.web.campaign_runner import launch_scheduled_campaign

    throttled = storage.list_throttled_campaigns()
    for campaign in throttled:
        try:
            launch_scheduled_campaign(campaign["id"])
        except Exception:
            logger.exception("Error rechecking throttled campaign %s", campaign["id"])


# ---- Serving Reminders ----
# Unlike campaigns (one-shot DateTrigger, in-memory job store rebuilt from
# storage.campaigns on startup), each active rule gets one recurring
# CronTrigger job that fires every week at its own day/time in its own
# timezone - the job itself (run_serving_reminder_rule) is idempotent
# per-plan via serving_reminder_log, so there's no separate "already fired
# this week" bookkeeping needed here the way campaigns need scheduled_at
# tracking.
#
# run_serving_reminder_rule is a coroutine function - AsyncIOScheduler runs
# awaitable jobs directly on the event loop (unlike the sync
# launch_scheduled_campaign above, which needs the ThreadPoolExecutor
# because it blocks on synchronous calls).

def _serving_rule_job_id(rule_id: int) -> str:
    return f"serving-rule-{rule_id}"


def schedule_serving_rule(rule: dict) -> None:
    """Registers (or re-registers) the recurring job for one active
    Serving Reminder rule. Called on startup (reload_serving_rules) and
    whenever a rule is saved as active from the Automations page."""
    from autosend.services.serving_reminder import run_serving_reminder_rule

    hour, minute = rule["send_time"].split(":")
    scheduler.add_job(
        run_serving_reminder_rule,
        trigger=CronTrigger(
            day_of_week=rule["send_day_of_week"],
            hour=int(hour),
            minute=int(minute),
            timezone=rule["timezone"],
        ),
        args=[rule["id"]],
        id=_serving_rule_job_id(rule["id"]),
        replace_existing=True,
        # If the app was down when this would have fired, don't fire a
        # backlog of stale runs - the recipient list would just be the
        # next real occurrence anyway since this is idempotent on the
        # *plan*, not the fire time; simplest to skip a missed run
        # entirely rather than fire it late.
        misfire_grace_time=None,
    )


def cancel_serving_rule_job(rule_id: int) -> None:
    job = scheduler.get_job(_serving_rule_job_id(rule_id))
    if job:
        job.remove()


def reload_serving_rules() -> None:
    """Called once on app startup (alongside reload_pending_campaigns) to
    register every currently-active rule's recurring job, plus the
    recurring deferred-reminder recheck job (see below)."""
    rules = storage.list_active_serving_rules()
    for rule in rules:
        schedule_serving_rule(rule)
    logger.info("Registered %d active serving reminder rule(s)", len(rules))

    _schedule_serving_throttle_recheck()


SERVING_THROTTLE_RECHECK_JOB_ID = "serving-throttle-recheck"
SERVING_THROTTLE_RECHECK_INTERVAL_MINUTES = 15


def _schedule_serving_throttle_recheck() -> None:
    scheduler.add_job(
        recheck_deferred_serving_reminders,
        trigger=IntervalTrigger(minutes=SERVING_THROTTLE_RECHECK_INTERVAL_MINUTES),
        id=SERVING_THROTTLE_RECHECK_JOB_ID,
        replace_existing=True,
        # Same reasoning as campaigns' throttle recheck: fire once shortly
        # after registration, not just after the first full interval - a
        # rule deferred right before a deploy/restart shouldn't wait a
        # full 15 minutes for its first recheck.
        next_run_time=datetime.now(),
    )


async def recheck_deferred_serving_reminders() -> None:
    """Runs every SERVING_THROTTLE_RECHECK_INTERVAL_MINUTES. Mirrors
    recheck_throttled_campaigns's role, but serving reminders have no
    single rule-level status to flip (state lives per rule+plan+person in
    serving_reminder_log), so this queries the log directly for
    (rule_id, plan_id) pairs still holding deferred rows and retries each
    one individually via retry_deferred_plan, rather than re-launching a
    single stored job the way launch_scheduled_campaign does.

    Unlike recheck_throttled_campaigns (sync, runs on APScheduler's
    default ThreadPoolExecutor), this is async - retry_deferred_plan
    awaits PCO and WhatsApp client calls, so it runs directly on the
    event loop, same as schedule_serving_rule's jobs do.
    """
    from autosend.services.serving_reminder import retry_deferred_plan

    deferred = storage.list_deferred_serving_reminders()
    for item in deferred:
        try:
            await retry_deferred_plan(item["rule_id"], item["pco_plan_id"])
        except Exception:
            logger.exception(
                "Error rechecking deferred serving reminders for rule %s plan %s",
                item["rule_id"], item["pco_plan_id"],
            )
