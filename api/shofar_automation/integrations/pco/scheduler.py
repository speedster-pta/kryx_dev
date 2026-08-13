"""
integrations/pco/scheduler.py

APScheduler job registration for PCO. The deferred serving-reminder
retry job (recheck_deferred_serving_reminders, 15-minute IntervalTrigger
in the parent project) is only scheduled if at least one organisation
currently has the pco module enabled — it should not run unconditionally
for a platform where most orgs have never touched PCO.

Re-checked on its own interval (not just at startup) so an org toggling
the module on/off doesn't need an app restart to take effect.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from shofar_automation.storage.modules import orgs_with_module_enabled
from shofar_automation.core.automation_engine import fire

logger = logging.getLogger("kryx.integrations.pco.scheduler")

MODULE_KEY = "pco"
JOB_ID = "pco_recheck_deferred_serving_reminders"


def register_scheduler_jobs(scheduler: AsyncIOScheduler) -> None:
    """
    Called once from app startup (core composition root), for every
    installed integration. Registers a lightweight "should I even be
    running" gate job rather than assuming PCO is in use.
    """
    scheduler.add_job(
        _recheck_deferred_serving_reminders,
        trigger=IntervalTrigger(minutes=15),
        id=JOB_ID,
        replace_existing=True,
    )


async def _recheck_deferred_serving_reminders() -> None:
    org_ids = orgs_with_module_enabled(MODULE_KEY)
    if not org_ids:
        logger.debug("PCO module not enabled for any organisation — skipping tick")
        return

    for org_id in org_ids:
        try:
            await _recheck_for_org(org_id)
        except Exception:
            logger.exception("Deferred serving reminder recheck failed for org_id=%s", org_id)


async def _recheck_for_org(org_id: int) -> None:
    """
    Placeholder for the actual deferred-reminder query + re-fire logic
    ported from the parent project's recheck_deferred_serving_reminders.
    Fires through automation_engine so the send logic itself stays in
    automations.py, not duplicated here.
    """
    # deferred = pco_storage.get_due_deferred_reminders(org_id)
    # for reminder in deferred:
    #     fire("pco.serving.reminder.due", org_id, reminder)
    logger.debug("Rechecked deferred serving reminders for org_id=%s", org_id)
