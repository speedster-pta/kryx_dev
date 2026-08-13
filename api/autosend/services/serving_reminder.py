"""Serving Reminders: WhatsApp-reminds people scheduled to serve at either
the next upcoming PCO Services plan for a unit's rule, or every
plan within a configured number of days ahead (see plan_selection_mode).

Mirrors form_response.py/registration_poller.py's shape (resolve PCO data
-> resolve template/number -> build available_fields -> send -> record),
but drives a loop over a Plan's team members instead of a single person,
and uses serving_reminder_log (not send_log alone) for idempotency, since
a rule can legitimately be re-run (recurring schedule + manual trigger)
against the same plan without wanting to re-message people already sent.
"""

from autosend.clients import get_pco_client, resolve_whatsapp_client
from autosend.integrations.whatsapp import MessagingLimitExceeded, WhatsAppSendError
from autosend import storage
from autosend.template_variables import resolve_variable_strict
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

# Available template variables for a Serving Reminder, surfaced to the
# Automations UI the same way FORM_VARIABLES is hardcoded client-side for
# Form Responses - keep this list and automations.html's SERVING_VARIABLES
# in sync by hand, same as that existing pair.
AVAILABLE_FIELDS = (
    "first_name", "last_name", "name",
    "team_position_name", "service_type_name", "plan_title", "plan_date",
)

# PCO team_members `status` attribute codes.
_CONFIRMED = "C"
_UNCONFIRMED = "U"
_DECLINED = "D"


_STATUS_FILTER_ALLOWED = {
    "confirmed_only": {_CONFIRMED},
    "all_scheduled": {_CONFIRMED, _UNCONFIRMED},
    "unconfirmed_only": {_UNCONFIRMED},
}


def _unit_by_id(unit_id: int) -> dict | None:
    # No get_unit_by_id in storage - every existing caller only
    # ever needed by-slug/by-phone-id lookups. Filtering the active list
    # avoids adding a near-duplicate storage function for one caller.
    for c in storage.get_active_units():
        if c["id"] == unit_id:
            return c
    return None


def _record(unit, status, *, phone=None, error_code=None, error_message=None,
            whatsapp_number_id=None, template_name=None, reference_id=None):
    storage.record_send(
        unit_id=unit["id"], source="serving_reminder", status=status,
        whatsapp_number_id=whatsapp_number_id, recipient_phone=phone, template_name=template_name,
        error_code=error_code, error_message=error_message, reference_id=reference_id,
    )


async def _run_for_plan(
    pco_client, whatsapp_client, whatsapp_number_id, unit, rule, plan,
    allowed_statuses, limit_hit: bool,
) -> tuple[int, int, int, bool, str | None]:
    """Sends this rule's reminder to one plan's eligible team members.
    Returns (sent, skipped, failed, limit_hit, error) - limit_hit is
    threaded through (not reset per plan) so once the WABA's 24h limit is
    hit on an earlier plan in this run, every remaining plan's sends are
    skipped too rather than each independently re-discovering the same
    limit. `error` is set (and sent/skipped/failed left at 0) only if the
    team-member fetch itself failed - a fetch failure on one plan doesn't
    abort the rest of the run's other plans.

    Sends are attempted one at a time and only stop once Meta actually
    rejects a send with MessagingLimitExceeded - there's no upfront
    capacity check against the whole target list the way campaigns'
    _run_campaign sizes batches against available_capacity() first. That
    means a plan can partially succeed: everyone processed before the
    limit is hit gets sent, everyone from that point on (including
    whoever triggered the rejection) gets deferred."""
    rule_id = rule["id"]

    try:
        team_members = await pco_client.get_plan_team_members(rule["pco_service_type_id"], plan["id"])
    except Exception as exc:
        logger.exception(
            "[%s] Failed to fetch team members for plan %s (rule %s)",
            unit["slug"], plan["id"], rule_id,
        )
        return 0, 0, 0, limit_hit, f"Failed to fetch scheduled team from Planning Center: {exc}"

    targets = [tm for tm in team_members if tm.get("person_id") and tm.get("status") in allowed_statuses]

    sent = skipped = failed = 0

    for member in targets:
        person_id = member["person_id"]

        if storage.is_serving_reminder_sent(rule_id, plan["id"], person_id):
            skipped += 1
            continue

        if limit_hit:
            # Once the WABA's 24h limit is hit, every remaining send this
            # run will fail the same way - stop attempting them (same
            # defer-and-retry-next-time reasoning as registration_poller's
            # `break` on MessagingLimitExceeded) rather than recording a
            # wall of identical failures.
            skipped += 1
            continue

        try:
            person = await pco_client.get_person(person_id)
            phone = await pco_client.get_person_phone(person_id)
        except Exception as exc:
            logger.warning(
                "[%s] Could not fetch person %s for rule %s: %s",
                unit["slug"], person_id, rule_id, exc,
            )
            storage.mark_serving_reminder(rule_id, plan["id"], person_id, "failed", detail=str(exc))
            _record(unit, "failed", template_name=rule["template_name"],
                    whatsapp_number_id=whatsapp_number_id, error_message=str(exc), reference_id=plan["id"])
            failed += 1
            continue

        if not phone:
            detail = "No phone number on file"
            storage.mark_serving_reminder(rule_id, plan["id"], person_id, "failed", detail=detail)
            _record(unit, "failed", template_name=rule["template_name"],
                    whatsapp_number_id=whatsapp_number_id, error_message=detail, reference_id=plan["id"])
            failed += 1
            continue

        attrs = person["data"]["attributes"]
        available_fields = {
            "first_name": attrs.get("first_name") or attrs.get("name", ""),
            "last_name": attrs.get("last_name", ""),
            "name": attrs.get("name", ""),
            "team_position_name": member.get("team_position_name", ""),
            "service_type_name": rule.get("pco_service_type_name") or "",
            "plan_title": plan.get("title") or "",
            "plan_date": plan.get("dates") or "",
        }

        try:
            ordered_values = [resolve_variable_strict(var, available_fields) for var in rule["body_variable_order"]]
        except KeyError as missing:
            detail = f"Missing variable: {missing}"
            logger.error(
                "[%s] Serving reminder template %s requires variable %s, not available. Available: %s",
                unit["slug"], rule["template_name"], missing, list(available_fields),
            )
            storage.mark_serving_reminder(rule_id, plan["id"], person_id, "failed", detail=detail)
            _record(unit, "failed", phone=phone, template_name=rule["template_name"],
                    whatsapp_number_id=whatsapp_number_id, error_message=detail, reference_id=plan["id"])
            failed += 1
            continue

        try:
            button_values = [
                resolve_variable_strict(key, available_fields) if key else None
                for key in rule.get("button_variables") or []
            ]
        except KeyError as missing:
            detail = f"Missing button variable: {missing}"
            storage.mark_serving_reminder(rule_id, plan["id"], person_id, "failed", detail=detail)
            _record(unit, "failed", phone=phone, template_name=rule["template_name"],
                    whatsapp_number_id=whatsapp_number_id, error_message=detail, reference_id=plan["id"])
            failed += 1
            continue

        try:
            await whatsapp_client.send_template(
                phone, rule["template_name"], *ordered_values,
                header_image_url=rule.get("header_image_url"), button_values=button_values,
            )
        except MessagingLimitExceeded as exc:
            storage.mark_serving_reminder(rule_id, plan["id"], person_id, "deferred", detail=str(exc))
            _record(unit, "deferred", phone=phone, template_name=rule["template_name"],
                    whatsapp_number_id=whatsapp_number_id, error_message=str(exc), reference_id=plan["id"])
            limit_hit = True
            skipped += 1
            continue
        except Exception as exc:
            code = exc.code if isinstance(exc, WhatsAppSendError) else None
            storage.mark_serving_reminder(rule_id, plan["id"], person_id, "failed", detail=str(exc))
            _record(unit, "failed", phone=phone, template_name=rule["template_name"],
                    whatsapp_number_id=whatsapp_number_id, error_code=code, error_message=str(exc),
                    reference_id=plan["id"])
            failed += 1
            continue

        storage.mark_serving_reminder(rule_id, plan["id"], person_id, "sent")
        _record(unit, "sent", phone=phone, template_name=rule["template_name"],
                whatsapp_number_id=whatsapp_number_id, reference_id=plan["id"])
        sent += 1
        logger.info(
            "[%s] Sent serving reminder (%s) for plan %s to %s (%s)",
            unit["slug"], rule["template_name"], plan["id"], available_fields["first_name"], phone,
        )

    return sent, skipped, failed, limit_hit, None


async def run_serving_reminder_rule(rule_id: int) -> dict:
    """Runs one rule against either its next upcoming plan, or every plan
    within its configured days_ahead window, per rule["plan_selection_mode"].
    Called both by the scheduler (recurring, active rules only) and the
    manual "Send now" button (any rule, active or not - an explicit
    request overrides the toggle).

    Returns a summary dict - {"plans": [...], "sent", "skipped", "failed"} -
    so the manual-trigger endpoint can report back what happened per plan,
    rather than the caller having to re-query serving_reminder_log itself.
    """
    rule = storage.get_serving_rule_by_id(rule_id)
    if not rule:
        logger.error("run_serving_reminder_rule: rule %s no longer exists", rule_id)
        return {"error": "Rule not found"}

    unit = _unit_by_id(rule["unit_id"])
    if not unit:
        logger.error("run_serving_reminder_rule: unit %s no longer exists", rule["unit_id"])
        return {"error": "Unit not found"}

    pco_client = get_pco_client(unit)
    mode = rule.get("plan_selection_mode") or "next_event"

    try:
        if mode == "days_ahead":
            plans = await pco_client.get_upcoming_plans(rule["pco_service_type_id"], rule["days_ahead"])
        else:
            next_plan = await pco_client.get_next_plan(rule["pco_service_type_id"])
            plans = [next_plan] if next_plan else []
    except Exception:
        logger.exception(
            "[%s] Failed to fetch upcoming plan(s) for service type %s (rule %s)",
            unit["slug"], rule["pco_service_type_id"], rule_id,
        )
        return {"error": "Failed to fetch upcoming plan(s) from Planning Center"}

    if not plans:
        logger.info(
            "[%s] No upcoming plan(s) found for service type %s (rule %s) - nothing to send",
            unit["slug"], rule["pco_service_type_name"], rule_id,
        )
        return {"plans": [], "sent": 0, "skipped": 0, "failed": 0}

    allowed_statuses = _STATUS_FILTER_ALLOWED.get(rule["status_filter"], {_CONFIRMED, _UNCONFIRMED})
    whatsapp_client = resolve_whatsapp_client(unit, rule)
    whatsapp_number_id = whatsapp_client.number.get("id") if whatsapp_client.number else None

    plan_results = []
    total_sent = total_skipped = total_failed = 0
    limit_hit = False

    for plan in plans:
        sent, skipped, failed, limit_hit, plan_error = await _run_for_plan(
            pco_client, whatsapp_client, whatsapp_number_id, unit, rule, plan,
            allowed_statuses, limit_hit,
        )
        plan_results.append({
            "id": plan["id"], "title": plan.get("title"), "dates": plan.get("dates"),
            "sent": sent, "skipped": skipped, "failed": failed, "error": plan_error,
        })
        total_sent += sent
        total_skipped += skipped
        total_failed += failed
        logger.info(
            "[%s] Serving reminder rule %s (plan %s): sent=%d skipped=%d failed=%d",
            unit["slug"], rule_id, plan["id"], sent, skipped, failed,
        )

    return {"plans": plan_results, "sent": total_sent, "skipped": total_skipped, "failed": total_failed}


async def retry_deferred_plan(rule_id: int, pco_plan_id: str) -> dict:
    """Re-attempts sends for whoever is still 'deferred' against one
    specific (rule, plan) pair - called by the scheduler's periodic
    serving-reminder throttle-recheck (scheduler.py::
    recheck_deferred_serving_reminders, mirrors campaigns'
    recheck_throttled_campaigns), not by the weekly per-rule cron job.

    Unlike run_serving_reminder_rule, this does NOT ask PCO "what's the
    next/upcoming plan" - the plan is already known (it's the one that
    was deferred), so this refetches that exact plan by ID via
    pco_client.get_plan() to pick up any title/date changes, then hands
    off to the same _run_for_plan every other send path uses.
    _run_for_plan's own is_serving_reminder_sent check means only the
    still-deferred people are re-attempted; anyone already sent (e.g. via
    a manual "Send now" in between) is skipped as usual.

    Like run_serving_reminder_rule (and unlike campaigns' pre-sized
    batches), this is reactive, not proactive - it can itself partially
    succeed and re-defer people if capacity runs out again partway
    through the retry.

    limit_hit is passed in fresh (False) rather than carried over from
    whatever ended the original run - the whole point of a recheck is
    that capacity may have freed up since then.
    """
    rule = storage.get_serving_rule_by_id(rule_id)
    if not rule or not rule["active"]:
        # Rule was deleted or turned off since it deferred - nothing to
        # retry. Not an error: this is the expected outcome once staff
        # disables a rule that still has stale deferred rows.
        return {"plan_id": pco_plan_id, "sent": 0, "skipped": 0, "failed": 0, "error": None}

    unit = _unit_by_id(rule["unit_id"])
    if not unit:
        logger.error(
            "retry_deferred_plan: unit %s no longer exists (rule %s)",
            rule["unit_id"], rule_id,
        )
        return {"plan_id": pco_plan_id, "sent": 0, "skipped": 0, "failed": 0,
                "error": "Unit not found"}

    pco_client = get_pco_client(unit)

    try:
        plan = await pco_client.get_plan(rule["pco_service_type_id"], pco_plan_id)
    except Exception as exc:
        logger.exception(
            "[%s] Failed to refetch plan %s for deferred retry (rule %s)",
            unit["slug"], pco_plan_id, rule_id,
        )
        return {"plan_id": pco_plan_id, "sent": 0, "skipped": 0, "failed": 0,
                "error": f"Failed to refetch plan from Planning Center: {exc}"}

    if not plan:
        # Plan was deleted/cancelled in PCO since the original deferral -
        # nothing left to retry against. Leave the deferred log rows as
        # they are (not converting to "failed") since this isn't a send
        # failure, just a plan that no longer exists.
        logger.info(
            "[%s] Deferred plan %s (rule %s) no longer exists in Planning Center - skipping retry",
            unit["slug"], pco_plan_id, rule_id,
        )
        return {"plan_id": pco_plan_id, "sent": 0, "skipped": 0, "failed": 0,
                "error": "Plan no longer exists in Planning Center"}

    allowed_statuses = _STATUS_FILTER_ALLOWED.get(rule["status_filter"], {_CONFIRMED, _UNCONFIRMED})
    whatsapp_client = resolve_whatsapp_client(unit, rule)
    whatsapp_number_id = whatsapp_client.number.get("id") if whatsapp_client.number else None

    sent, skipped, failed, _limit_hit, plan_error = await _run_for_plan(
        pco_client, whatsapp_client, whatsapp_number_id, unit, rule, plan,
        allowed_statuses, False,
    )

    logger.info(
        "[%s] Retried deferred serving reminders for rule %s (plan %s): sent=%d skipped=%d failed=%d",
        unit["slug"], rule_id, pco_plan_id, sent, skipped, failed,
    )

    return {"plan_id": pco_plan_id, "sent": sent, "skipped": skipped, "failed": failed, "error": plan_error}
