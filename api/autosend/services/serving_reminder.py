"""Serving Reminders: WhatsApp-reminds people scheduled to serve at either
the next upcoming PCO Services plan for a unit's rule (plan_selection_mode
'next_event'), or every plan within a configured number of days ahead
('days_ahead').

Mirrors form_response.py/registration_poller.py's shape (resolve PCO data
-> resolve template/number -> build available_fields -> send -> record),
but drives a loop over a Plan's team members instead of a single person,
and uses serving_reminder_log (not send_log alone) for idempotency, since
a rule can legitimately be re-run (recurring schedule + manual trigger)
against the same plan without wanting to re-message people already sent.

'next_event' sends one message per (plan, person) via _run_for_plan -
there's only ever one plan in that mode's run, so this is never more than
one message per person anyway. 'days_ahead' (the monthly-digest case)
instead combines every plan a person is on across the whole run into ONE
message with ONE calendar link bundling all of their events, via
_run_days_ahead_combined - see that function's docstring for why plan- vs
person-outer looping matters here."""

from datetime import datetime, timedelta

from autosend.clients import get_pco_client, resolve_whatsapp_client
from autosend.integrations.whatsapp import MessagingLimitExceeded, WhatsAppSendError
from autosend import storage
from autosend.template_variables import resolve_variable_strict
from autosend.utils.logging import get_logger
from autosend.utils.phone import normalize_phone_e164

logger = get_logger(__name__)

# PCO team_members `status` attribute codes.
_CONFIRMED = "C"
_UNCONFIRMED = "U"


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


def _ical_event_for_plan(unit, rule, plan) -> dict | None:
    """Creates (or, on a reschedule, updates in place - same UID, bumped
    SEQUENCE, see storage.upsert_ical_event) the one shared calendar event
    for this plan, so every team member scheduled on it gets their own
    link pointing at the same occurrence. Returns None - meaning no
    add-to-calendar button variable is available this run - when the
    org's ical module isn't enabled, or PCO hasn't attached a scheduled
    time to this plan yet (sort_date absent).

    Deliberately keeps the event title/description to scheduling
    information only (service type + plan title) - no team-position or
    per-person detail belongs on the shared calendar entry; that stays in
    the WhatsApp message text, which is already personalised per
    recipient."""
    if not storage.is_enabled(unit["org_id"], storage.MODULE_ICAL):
        return None
    sort_date = plan.get("sort_date")
    if not sort_date:
        return None
    try:
        starts_dt = datetime.fromisoformat(sort_date.replace("Z", "+00:00"))
    except ValueError:
        return None

    # PCO's Plan resource (as fetched here) only gives a single scheduled
    # timestamp (sort_date), not a start/end pair - defaulting to a 1h
    # block is a known simplification, not a claim of precise duration.
    ends_at = (starts_dt + timedelta(hours=1)).isoformat()
    expires_at = (starts_dt + timedelta(days=1)).isoformat()

    event, _is_update = storage.upsert_ical_event(
        unit["id"], "pco_serving", f"{rule['pco_service_type_id']}:{plan['id']}",
        rule.get("pco_service_type_name") or "Service",
        sort_date,
        description=plan.get("title") or None,
        ends_at=ends_at,
        expires_at=expires_at,
    )
    return event


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

    ical_event = _ical_event_for_plan(unit, rule, plan)

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

        # PCO's own e164 field is normally already valid (see utils/phone.py
        # docstring), but re-validate/reformat defensively here in case a
        # person's PCO record has an incomplete/malformed phone entry -
        # treat that the same as no phone on file rather than sending an
        # unvalidated string to WhatsApp.
        default_region = (whatsapp_client.number or {}).get("default_region", "ZA")
        phone = normalize_phone_e164(phone, default_region)
        if not phone:
            detail = "Phone number on file is not a valid phone number"
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
        if ical_event:
            # Idempotent per (rule, plan, phone) - reruns against the same
            # plan (recurring schedule, or a manual "Send now" re-trigger)
            # reuse this person's existing token rather than minting a new
            # one. link_key is a bundle-of-one here (see
            # _run_days_ahead_combined for the many-events case).
            link = storage.get_or_create_ical_link(
                f"servingplan:{rule_id}:{plan['id']}", phone, ical_event["expires_at"],
            )
            storage.attach_event_to_link(link["id"], ical_event["id"])
            available_fields["calendar_link_suffix"] = f"{link['token']}.ics"

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


async def _run_days_ahead_combined(
    pco_client, whatsapp_client, whatsapp_number_id, unit, rule, plans, allowed_statuses,
) -> tuple[int, int, int, list[dict]]:
    """days_ahead mode only: rather than one message per (plan, person) -
    which would mean a volunteer scheduled 4 times in the window gets 4
    separate reminders - this gathers every plan each person is on across
    the whole run first, then sends ONE combined message per person with
    ONE calendar link bundling all of their events (see
    integrations/ical/builder.py). next_event mode is unaffected by this
    function - it still sends one message per plan via _run_for_plan,
    since there's only ever one plan in that mode's run.

    Returns (sent, skipped, failed, plan_summaries) - sent/skipped/failed
    count PEOPLE, not plan-messages, since a person's several plans now
    result in exactly one send outcome, not several."""
    rule_id = rule["id"]
    plan_errors = []
    # person_id -> list of (plan, member)
    assignments_by_person: dict[str, list[tuple[dict, dict]]] = {}

    for plan in plans:
        try:
            team_members = await pco_client.get_plan_team_members(rule["pco_service_type_id"], plan["id"])
        except Exception as exc:
            logger.exception(
                "[%s] Failed to fetch team members for plan %s (rule %s)",
                unit["slug"], plan["id"], rule_id,
            )
            plan_errors.append((plan, f"Failed to fetch scheduled team from Planning Center: {exc}"))
            continue

        for member in team_members:
            if member.get("person_id") and member.get("status") in allowed_statuses:
                assignments_by_person.setdefault(member["person_id"], []).append((plan, member))

    sent = skipped = failed = 0
    limit_hit = False

    for person_id, assignments in assignments_by_person.items():
        plan_ids = sorted({str(plan["id"]) for plan, _member in assignments})

        if all(storage.is_serving_reminder_sent(rule_id, pid, person_id) for pid in plan_ids):
            skipped += 1
            continue

        if limit_hit:
            for plan, _member in assignments:
                storage.mark_serving_reminder(rule_id, plan["id"], person_id, "deferred",
                                               detail="Skipped - WABA 24h limit already hit this run")
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
            phone = None
            person_fetch_error = str(exc)
        else:
            person_fetch_error = None

        if person_fetch_error or not phone:
            detail = person_fetch_error or "No phone number on file"
            for plan, _member in assignments:
                storage.mark_serving_reminder(rule_id, plan["id"], person_id, "failed", detail=detail)
            _record(unit, "failed", template_name=rule["template_name"],
                    whatsapp_number_id=whatsapp_number_id, error_message=detail, reference_id=str(person_id))
            failed += 1
            continue

        default_region = (whatsapp_client.number or {}).get("default_region", "ZA")
        phone = normalize_phone_e164(phone, default_region)
        if not phone:
            detail = "Phone number on file is not a valid phone number"
            for plan, _member in assignments:
                storage.mark_serving_reminder(rule_id, plan["id"], person_id, "failed", detail=detail)
            _record(unit, "failed", template_name=rule["template_name"],
                    whatsapp_number_id=whatsapp_number_id, error_message=detail, reference_id=str(person_id))
            failed += 1
            continue

        # Sorted by plan date, not PCO's arbitrary member-list order, so
        # the summary text and the calendar bundle both read chronologically.
        assignments_sorted = sorted(assignments, key=lambda pair: pair[0].get("sort_date") or "")
        attrs = person["data"]["attributes"]
        available_fields = {
            "first_name": attrs.get("first_name") or attrs.get("name", ""),
            "last_name": attrs.get("last_name", ""),
            "name": attrs.get("name", ""),
            "service_type_name": rule.get("pco_service_type_name") or "",
            "schedule_summary": "; ".join(
                f"{plan.get('dates') or plan.get('sort_date') or ''} - "
                f"{member.get('team_position_name') or 'Serving'}"
                for plan, member in assignments_sorted
            ),
            # team_position_name/plan_title/plan_date are deliberately NOT
            # provided here (unlike _run_for_plan's available_fields) -
            # each person has several, potentially different, values for
            # each across this run's plans, and picking just one would be
            # misleading. A template configured for days_ahead mode should
            # use schedule_summary instead; selecting one of the
            # single-plan fields on such a rule fails loudly (KeyError ->
            # "Missing variable") rather than silently showing one of
            # several true values.
        }

        events_for_link = []
        for plan, _member in assignments_sorted:
            event = _ical_event_for_plan(unit, rule, plan)
            if event:
                events_for_link.append(event)
        if events_for_link:
            link_key = f"servingdays:{rule_id}:{person_id}:{'-'.join(plan_ids)}"
            link_expires_at = max(e["expires_at"] for e in events_for_link)
            link = storage.get_or_create_ical_link(link_key, phone, link_expires_at)
            for event in events_for_link:
                storage.attach_event_to_link(link["id"], event["id"])
            available_fields["calendar_link_suffix"] = f"{link['token']}.ics"

        try:
            ordered_values = [resolve_variable_strict(var, available_fields) for var in rule["body_variable_order"]]
            button_values = [
                resolve_variable_strict(key, available_fields) if key else None
                for key in rule.get("button_variables") or []
            ]
        except KeyError as missing:
            detail = f"Missing variable: {missing}"
            logger.error(
                "[%s] Serving reminder template %s requires variable %s, not available. Available: %s",
                unit["slug"], rule["template_name"], missing, list(available_fields),
            )
            for plan, _member in assignments:
                storage.mark_serving_reminder(rule_id, plan["id"], person_id, "failed", detail=detail)
            _record(unit, "failed", phone=phone, template_name=rule["template_name"],
                    whatsapp_number_id=whatsapp_number_id, error_message=detail, reference_id=str(person_id))
            failed += 1
            continue

        try:
            await whatsapp_client.send_template(
                phone, rule["template_name"], *ordered_values,
                header_image_url=rule.get("header_image_url"), button_values=button_values,
            )
        except MessagingLimitExceeded as exc:
            for plan, _member in assignments:
                storage.mark_serving_reminder(rule_id, plan["id"], person_id, "deferred", detail=str(exc))
            _record(unit, "deferred", phone=phone, template_name=rule["template_name"],
                    whatsapp_number_id=whatsapp_number_id, error_message=str(exc), reference_id=str(person_id))
            limit_hit = True
            skipped += 1
            continue
        except Exception as exc:
            code = exc.code if isinstance(exc, WhatsAppSendError) else None
            for plan, _member in assignments:
                storage.mark_serving_reminder(rule_id, plan["id"], person_id, "failed", detail=str(exc))
            _record(unit, "failed", phone=phone, template_name=rule["template_name"],
                    whatsapp_number_id=whatsapp_number_id, error_code=code, error_message=str(exc),
                    reference_id=str(person_id))
            failed += 1
            continue

        for plan, _member in assignments:
            storage.mark_serving_reminder(rule_id, plan["id"], person_id, "sent")
        _record(unit, "sent", phone=phone, template_name=rule["template_name"],
                whatsapp_number_id=whatsapp_number_id, reference_id=str(person_id))
        sent += 1
        logger.info(
            "[%s] Sent combined serving reminder (%s) for %d plan(s) to %s (%s)",
            unit["slug"], rule["template_name"], len(assignments), available_fields["first_name"], phone,
        )

    plan_summaries = [{"id": p["id"], "title": p.get("title"), "dates": p.get("dates"), "error": None}
                       for p in plans]
    for plan, error in plan_errors:
        for summary in plan_summaries:
            if summary["id"] == plan["id"]:
                summary["error"] = error

    return sent, skipped, failed, plan_summaries


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

    if not storage.is_org_active(unit["org_id"]):
        # Blocks both the recurring cron job and the manual "Send now"
        # button - an inactive org can't send either way.
        logger.info("run_serving_reminder_rule: org %s is inactive, not sending", unit["org_id"])
        return {"error": "Organisation is inactive - sending is disabled"}

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

    if mode == "days_ahead":
        # Combined path: one message per person covering every plan
        # they're on in this run, not one message per plan - see
        # _run_days_ahead_combined's docstring.
        total_sent, total_skipped, total_failed, plan_results = await _run_days_ahead_combined(
            pco_client, whatsapp_client, whatsapp_number_id, unit, rule, plans, allowed_statuses,
        )
        logger.info(
            "[%s] Serving reminder rule %s (days_ahead, %d plan(s)): sent=%d skipped=%d failed=%d",
            unit["slug"], rule_id, len(plans), total_sent, total_skipped, total_failed,
        )
        return {"plans": plan_results, "sent": total_sent, "skipped": total_skipped, "failed": total_failed}

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
