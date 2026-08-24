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
    whenever a rule is saved as active from the Automations page.

    'immediate'-schedule rules never reach here with a job to register -
    saving one is itself the send trigger (see web/automations_router.py's
    api_save_serving_rule), not a recurring job - so this only ever
    registers 'weekly' (CronTrigger on day_of_week) or 'monthly'
    (CronTrigger on day, i.e. day-of-month) jobs. Any leftover job from a
    rule that was previously weekly/monthly and has since switched to
    immediate is cancelled by the caller before this would even be
    reached."""
    from autosend.services.serving_reminder import run_serving_reminder_rule

    if rule.get("schedule_type") == "immediate":
        cancel_serving_rule_job(rule["id"])
        return

    hour, minute = rule["send_time"].split(":")
    if rule.get("schedule_type") == "monthly":
        trigger = CronTrigger(
            day=rule["send_day_of_month"],
            hour=int(hour),
            minute=int(minute),
            timezone=rule["timezone"],
        )
    else:
        trigger = CronTrigger(
            day_of_week=rule["send_day_of_week"],
            hour=int(hour),
            minute=int(minute),
            timezone=rule["timezone"],
        )
    scheduler.add_job(
        run_serving_reminder_rule,
        trigger=trigger,
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
    recurring deferred-reminder recheck job (see below).

    Skips rules belonging to an org that doesn't currently have the PCO
    module enabled - serving reminders are entirely PCO-driven, so a
    disabled org shouldn't get a live recurring send job just because a
    rule row still exists from before the module was disabled (see
    cancel_org_serving_rule_jobs/reschedule_org_serving_rules for the
    immediate-effect counterpart when a module is toggled mid-session)."""
    rules = storage.list_active_serving_rules()
    enabled_org_ids = set(storage.orgs_with_module_enabled(storage.MODULE_PCO))
    schedulable = [
        r for r in rules
        if r["org_id"] in enabled_org_ids and storage.is_org_active(r["org_id"])
        and storage.is_org_current(r["org_id"])
    ]
    for rule in schedulable:
        schedule_serving_rule(rule)
    logger.info(
        "Registered %d active serving reminder rule(s) (%d skipped - PCO module disabled or org inactive)",
        len(schedulable), len(rules) - len(schedulable),
    )

    _schedule_serving_throttle_recheck()


def cancel_org_serving_rule_jobs(org_id: int) -> None:
    """Called from ModulesView.toggle() when an org disables the PCO
    module, so its serving-reminder jobs stop firing immediately rather
    than lingering until the next restart (reload_serving_rules only runs
    at startup)."""
    for rule in storage.list_active_serving_rules():
        if rule["org_id"] == org_id:
            cancel_serving_rule_job(rule["id"])


def reschedule_org_serving_rules(org_id: int) -> None:
    """Mirror of cancel_org_serving_rule_jobs for re-enabling the PCO
    module mid-session - without this, a rule left active in the DB while
    disabled would only resume on the next app restart, which would be
    inconsistent with disable's immediate effect above."""
    for rule in storage.list_active_serving_rules():
        if rule["org_id"] == org_id:
            schedule_serving_rule(rule)


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
    (rule_id, plan_id) pairs still holding deferred rows.

    'next_event'-mode rules retry each (rule_id, plan_id) pair
    individually via retry_deferred_plan, same as before - each such
    rule's run only ever has one plan anyway.

    'days_ahead'-mode rules combine every plan a person is on into one
    message (see services/serving_reminder.py::_run_days_ahead_combined),
    so a per-plan retry doesn't compose - re-sending just one of a
    person's several plans would fragment the "one message" premise.
    Instead, each such rule is re-run wholesale via
    run_serving_reminder_rule, once per distinct rule_id (not once per
    deferred plan) - the rule's own is_serving_reminder_sent dedup means
    anyone already fully sent this run is skipped entirely, so this only
    actually re-messages whoever is still deferred or was never attempted.

    Unlike recheck_throttled_campaigns (sync, runs on APScheduler's
    default ThreadPoolExecutor), this is async - both retry paths await
    PCO and WhatsApp client calls, so this runs directly on the event
    loop, same as schedule_serving_rule's jobs do.
    """
    from autosend.services.serving_reminder import retry_deferred_plan, run_serving_reminder_rule

    deferred = storage.list_deferred_serving_reminders()
    days_ahead_rule_ids_retried: set[int] = set()

    for item in deferred:
        rule = storage.get_serving_rule_by_id(item["rule_id"])
        if not rule or not storage.is_enabled(rule["org_id"], storage.MODULE_PCO):
            # Rule's org disabled the PCO module (or the rule itself is
            # gone) since this row was logged - skip rather than retry a
            # send for an org that turned the integration off.
            continue
        if not storage.is_org_active(rule["org_id"]) or not storage.is_org_current(rule["org_id"]):
            # Org is inactive, or its subscription isn't currently active -
            # skip rather than retry a send for an org that can't
            # currently send.
            continue

        if rule.get("plan_selection_mode") == "days_ahead":
            if item["rule_id"] in days_ahead_rule_ids_retried:
                continue  # already re-run once for this rule this recheck pass
            days_ahead_rule_ids_retried.add(item["rule_id"])
            try:
                await run_serving_reminder_rule(item["rule_id"])
            except Exception:
                logger.exception(
                    "Error rechecking deferred (days_ahead) serving reminders for rule %s",
                    item["rule_id"],
                )
            continue

        try:
            await retry_deferred_plan(item["rule_id"], item["pco_plan_id"])
        except Exception:
            logger.exception(
                "Error rechecking deferred serving reminders for rule %s plan %s",
                item["rule_id"], item["pco_plan_id"],
            )


# ---- Platform Billing ----
# Two scheduler responsibilities: (1) apply a deferred plan downgrade
# exactly when its billing period ends (one-shot DateTrigger job per
# pending downgrade, mirroring schedule_serving_rule's per-rule job
# pattern above) and (2) run the recurring billing sweep once a day
# (CronTrigger, same shared AsyncIOScheduler instance as everything
# else in this module).

def _downgrade_job_id(subscription_id: int) -> str:
    return f"downgrade-{subscription_id}"


def schedule_pending_downgrade(subscription) -> None:
    """Registers (or re-registers) the one-shot job that applies a single
    subscription's pending downgrade at its effective date. `subscription`
    is a storage.Subscription (or anything with the same
    id/pending_downgrade_effective_at attributes)."""
    from autosend.billing.engine import apply_pending_downgrades

    if not subscription.pending_downgrade_effective_at:
        return
    run_time = datetime.fromisoformat(subscription.pending_downgrade_effective_at)
    scheduler.add_job(
        apply_pending_downgrades,
        trigger=DateTrigger(run_date=run_time),
        id=_downgrade_job_id(subscription.id),
        replace_existing=True,
        # No misfire cutoff: if the app was down past the effective date,
        # apply it as soon as it's back up rather than silently leaving
        # the downgrade stuck pending indefinitely.
        misfire_grace_time=None,
    )


def cancel_pending_downgrade_job(subscription_id: int) -> None:
    job = scheduler.get_job(_downgrade_job_id(subscription_id))
    if job:
        job.remove()


def reload_pending_downgrades() -> None:
    """Called once on app startup (alongside reload_pending_campaigns/
    reload_serving_rules) to re-register a one-shot job for every
    subscription still holding a pending downgrade from before the last
    restart - apply_pending_downgrades() itself sweeps every due
    subscription regardless of which one's job actually fired, so
    registering one job per subscription here is about firing promptly
    at the right time, not about which specific subscription each job
    "belongs" to."""
    from autosend import storage

    pending = storage.list_subscriptions_with_pending_downgrade()
    for subscription in pending:
        schedule_pending_downgrade(subscription)
    logger.info("Re-registered %d pending plan downgrade(s)", len(pending))

    _schedule_recurring_billing()


def _cancellation_job_id(subscription_id: int) -> str:
    return f"cancel-{subscription_id}"


def schedule_pending_cancellation(subscription) -> None:
    """Registers (or re-registers) the one-shot job that applies a single
    subscription's pending cancellation at cancel_at - same shape as
    schedule_pending_downgrade above."""
    from autosend.billing.engine import apply_pending_cancellations

    if not subscription.cancel_at:
        return
    run_time = datetime.fromisoformat(subscription.cancel_at)
    scheduler.add_job(
        apply_pending_cancellations,
        trigger=DateTrigger(run_date=run_time),
        id=_cancellation_job_id(subscription.id),
        replace_existing=True,
        misfire_grace_time=None,
    )


def cancel_pending_cancellation_job(subscription_id: int) -> None:
    """Removes the one-shot job - called when an org-admin undoes a
    pending cancellation (billing.engine.cancel_pending_cancellation) so
    a stale job doesn't fire and cancel a subscription that was un-cancelled."""
    job = scheduler.get_job(_cancellation_job_id(subscription_id))
    if job:
        job.remove()


def reload_pending_cancellations() -> None:
    """Called once on app startup, alongside reload_pending_downgrades -
    re-registers a one-shot job for every subscription still holding a
    pending cancellation from before the last restart."""
    from autosend import storage

    pending = storage.list_subscriptions_with_pending_cancellation()
    for subscription in pending:
        schedule_pending_cancellation(subscription)
    logger.info("Re-registered %d pending subscription cancellation(s)", len(pending))


RECURRING_BILLING_JOB_ID = "billing-recurring-charge"


def _schedule_recurring_billing() -> None:
    """Once-a-day sweep (03:00) that charges every active subscription
    whose current_period_end has passed - see
    billing.engine.run_recurring_billing for why a single daily attempt
    (no custom retry/dunning) is enough here."""
    from autosend.billing.engine import run_recurring_billing

    scheduler.add_job(
        run_recurring_billing,
        trigger=CronTrigger(hour=3, minute=0),
        id=RECURRING_BILLING_JOB_ID,
        replace_existing=True,
    )
