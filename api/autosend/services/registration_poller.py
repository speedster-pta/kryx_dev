from datetime import datetime, timedelta

import httpx

from autosend.clients import get_pco_client, get_stitch_client, resolve_whatsapp_client
from autosend.integrations.stitch import (
    build_reference,
    build_payer_name,
    extract_link_suffix,
    format_amount_due,
)
from autosend.integrations.whatsapp import MessagingLimitExceeded, WhatsAppSendError
from autosend import storage
from autosend.template_variables import is_custom_variable, resolve_variable_lenient, resolve_variable_strict
from autosend.utils.logging import get_logger
from autosend.utils.phone import normalize_phone_e164

logger = get_logger(__name__)


async def poll_for_new_registrations() -> None:
    for unit in storage.get_active_units():
        if not unit.get("pco_campus_id"):
            continue
        if not storage.is_enabled(unit["org_id"], storage.MODULE_PCO):
            # Org turned the PCO module off since it last polled clean -
            # skip rather than keep sending registration confirmations for
            # an integration the org can no longer see/manage in the UI.
            continue
        if not storage.is_org_active(unit["org_id"]):
            # Org is inactive (e.g. not currently paying) - it can still
            # see/manage the PCO module, just not send through it.
            continue
        if not storage.is_org_current(unit["org_id"]):
            # Org's subscription isn't currently active - same
            # can't-send-but-can-manage treatment as is_org_active above.
            continue
        try:
            await _poll_unit(unit)
        except Exception:
            # _poll_unit only re-raises here when the failure
            # wasn't a transient PCO/HTTP error (those are handled and
            # swallowed further down, with a retry next cycle) - i.e. this
            # is an actual bug, already logged at CRITICAL at its source.
            # Catch it here purely so one unit's bug can't stop
            # the rest of the organisation from being polled this cycle.
            logger.exception(
                "[%s] Poll cycle aborted for this unit due to an unexpected error",
                unit["slug"],
            )


async def _poll_unit(unit: dict) -> None:
    pco_client = get_pco_client(unit)
    try:
        eligible_signups = await pco_client.get_eligible_signups()
    except httpx.HTTPError:
        # Expected, transient: PCO unreachable, rate-limited, timed out,
        # etc. Not this unit's fault and not a bug - just skip
        # this cycle and let the next poll try again.
        logger.warning(
            "[%s] PCO API error fetching eligible signups; will retry next poll",
            unit["slug"], exc_info=True,
        )
        return
    except Exception:
        # Not an HTTP-level failure, so this is a programming bug (bad
        # response parsing, wrong key/attribute, etc.), not PCO being
        # unreachable. Swallowing it the same way as a network blip would
        # let the poller "succeed" silently forever while never actually
        # polling this unit. Log loudly and re-raise so it's
        # treated as a real failure by the caller.
        logger.critical(
            "[%s] Unexpected error fetching eligible signups - this looks like a bug, "
            "not a transient PCO issue",
            unit["slug"], exc_info=True,
        )
        raise

    logger.info(
        "[%s] Found %d eligible signup(s): %s",
        unit["slug"], len(eligible_signups),
        [s["name"] for s in eligible_signups],
    )

    for signup in eligible_signups:
        try:
            await _poll_signup(unit, signup)
        except Exception:
            # Same reasoning as poll_for_new_registrations' wrapper: this
            # only reaches here for a real bug (already logged at
            # CRITICAL in _poll_signup), and it's caught here so one
            # broken signup can't stop the rest of this unit's
            # signups from being polled.
            logger.exception(
                "[%s] Signup %s (%s) skipped this cycle due to an unexpected error",
                unit["slug"], signup.get("id"), signup.get("name"),
            )


async def _poll_signup(unit: dict, signup: dict) -> None:
    pco_client = get_pco_client(unit)
    signup_id = signup["id"]
    watermark = storage.get_signup_watermark(signup_id)

    if watermark is None:
        try:
            newest_id = await _get_newest_registration_id(pco_client, signup_id)
        except httpx.HTTPError:
            logger.warning(
                "Failed to baseline signup %s - PCO API error, will retry next poll",
                signup_id, exc_info=True,
            )
            return
        except Exception:
            logger.critical(
                "Unexpected error baselining signup %s - this looks like a bug, "
                "not a transient PCO issue",
                signup_id, exc_info=True,
            )
            raise
        if newest_id:
            storage.set_signup_watermark(signup_id, newest_id)
            logger.info(
                "Baselined signup %s (%s) at registration %s - no messages sent for existing registrations",
                signup_id, signup["name"], newest_id,
            )
        else:
            logger.info("Signup %s (%s) has no registrations yet - nothing to baseline", signup_id, signup["name"])
        return

    try:
        new_registrations = await pco_client.get_registrations_for_signup(
            signup_id, stop_at_registration_id=watermark
        )
    except httpx.HTTPError:
        logger.warning(
            "Failed to fetch registrations for signup %s - PCO API error, will retry next poll",
            signup_id, exc_info=True,
        )
        return
    except Exception:
        logger.critical(
            "Unexpected error fetching registrations for signup %s - this looks like a bug, "
            "not a transient PCO issue",
            signup_id, exc_info=True,
        )
        raise

    if not new_registrations:
        return

    # Skip at inception if there's no automation configured for this
    # signup's registration type yet - cheaper than discovering it deep
    # inside _process_registration_inner (which would first fetch PCO
    # registration detail, resolve the contact, and look up their phone
    # number for every registration, only to find nothing to send at the
    # end), and keeps automation history free of entries for units
    # that simply haven't set an automation up yet. Per Option B: these
    # specific registrations are skipped for good (watermark advances past
    # them) - once a template IS added, only registrations arriving after
    # that point will be messaged, not this backlog.
    template_type = "payment_reminder" if signup["is_paid"] else "free_acknowledgment"
    if not storage.get_template(unit["id"], template_type):
        logger.info(
            "[%s] Signup %s (%s): skipping %d new registration(s) - no %s "
            "automation configured yet for %s",
            unit["slug"], signup_id, signup["name"], len(new_registrations),
            template_type, unit["slug"],
        )
        storage.set_signup_watermark(signup_id, new_registrations[-1]["id"])
        return

    logger.info(
        "[%s] Signup %s (%s): %d new registration(s)",
        unit["slug"], signup_id, signup["name"], len(new_registrations),
    )

    last_resolved_id = None

    for registration in new_registrations:
        registration_id = registration["id"]

        if storage.is_processed(registration_id):
            last_resolved_id = registration_id
            continue

        try:
            await _process_registration(unit, registration_id, signup)
            storage.mark_processed(registration_id, signup_id, status="sent")
        except MessagingLimitExceeded as exc:
            # Not a genuine failure - this registration's confirmation
            #/payment message just couldn't send because the number is at
            # its 24h messaging limit right now. Don't mark it processed
            # and don't advance the watermark past it (or anything after
            # it in this batch, since order matters here) - leave it as-is
            # so the next poll cycle picks it up again and retries once
            # capacity is back. Unlike a real error, this doesn't need
            # manual attention via /ops/failures.
            logger.warning(
                "[%s] Registration %s (signup %s) deferred - messaging limit: %s. "
                "Will retry on next poll.",
                unit["slug"], registration_id, signup_id, exc,
            )
            break
        except Exception as exc:
            logger.exception(
                "Failed to process registration %s (signup %s) - will NOT auto-retry, "
                "check /ops/failures",
                registration_id, signup_id,
            )
            storage.mark_processed(registration_id, signup_id, status="failed", detail=str(exc))

        last_resolved_id = registration_id

    if last_resolved_id:
        storage.set_signup_watermark(signup_id, last_resolved_id)


async def _get_newest_registration_id(pco_client, signup_id: str) -> str | None:
    all_regs = await pco_client.get_registrations_for_signup(signup_id, stop_at_registration_id=None)
    return all_regs[-1]["id"] if all_regs else None


def _ical_events_for_signup(unit: dict, signup: dict) -> list[dict]:
    """Creates (or, on a reschedule, updates in place - same UID, bumped
    SEQUENCE, see storage.upsert_ical_event) one shared calendar event per
    PCO signup_time on this signup, so every registrant's link bundles the
    exact same set of occurrences (mirrors services/serving_reminder.py's
    _ical_event_for_plan, but per-time rather than per-plan since a single
    signup can have several distinct sessions - see
    PlanningCenterClient.get_eligible_signups's docstring on why these are
    kept separate rather than collapsed into one earliest-to-latest span).
    Returns [] - meaning no calendar_link_suffix variable is available
    this run - when the org's ical module isn't enabled, or PCO has no
    signup_times attached to this signup yet."""
    if not storage.is_enabled(unit["org_id"], storage.MODULE_ICAL):
        return []

    events = []
    for time in signup.get("times") or []:
        starts_at = time["starts_at"]
        ends_at = time.get("ends_at")
        try:
            expiry_basis = datetime.fromisoformat((ends_at or starts_at).replace("Z", "+00:00"))
        except ValueError:
            continue
        expires_at = (expiry_basis + timedelta(days=1)).isoformat()

        event, _is_update = storage.upsert_ical_event(
            unit["id"], "pco_signup_time", f"{signup['id']}:{time['id']}",
            signup["name"], starts_at,
            location=signup.get("location"),
            ends_at=ends_at,
            expires_at=expires_at,
        )
        events.append(event)
    return events


def _calendar_link_suffix(signup: dict, phone: str, ical_events: list[dict]) -> str | None:
    """Bundles every ical_event for this signup onto one link for this
    recipient (idempotent per (signup, phone), same as
    get_or_create_ical_link's contract elsewhere) and returns the
    "<token>" suffix for available_fields (points at the /ical/{token}
    add-to-calendar landing page, not the raw .ics file directly - see
    web/ical_router.py), or None when there's nothing to bundle (ical
    module off, or no signup_times)."""
    if not ical_events:
        return None
    expires_at = max(event["expires_at"] for event in ical_events)
    link = storage.get_or_create_ical_link(f"registration:{signup['id']}", phone, expires_at)
    for event in ical_events:
        storage.attach_event_to_link(link["id"], event["id"])
    return link["token"]


def _resolve_body_values(
    unit: dict, template: dict, available_fields: dict,
) -> list[str]:
    """Resolves this template's body_variable_order into the ordered list
    of literal text to send, in place of the historical hardcoded
    [name, event_name] / [name, event_name, amount_due, reference]
    positions - a custom:<text> entry (see template_variables.py) is
    returned as-is; any other entry is looked up in available_fields.
    Raises ValueError (caught by _process_registration's existing
    exception handling, same as any other failure building this send) if
    a configured key isn't available for this automation type."""
    order = template.get("body_variable_order") or []
    try:
        return [resolve_variable_strict(key, available_fields) for key in order]
    except KeyError as missing:
        raise ValueError(
            f"Template {template['template_name']} requires variable {missing}, "
            f"which isn't available for this automation. Available: {list(available_fields)}"
        ) from missing


def _resolve_button_values(
    unit: dict, template_name: str, button_variables: list[str], available_fields: dict,
) -> list[str | None]:
    """Resolves each configured button variable key to its value for this
    send. A blank/unset entry means that button position has no dynamic
    URL variable. A custom:<text> entry (see template_variables.py) is
    used as-is. A key that isn't available for this automation type (e.g.
    "reference" configured on a free_acknowledgment template, which never
    computes one) is logged and that single button is skipped - it does
    NOT abort the rest of the registration send."""
    values: list[str | None] = []
    for key in button_variables:
        if not key:
            values.append(None)
            continue
        if is_custom_variable(key):
            values.append(resolve_variable_lenient(key, available_fields))
            continue
        if key not in available_fields:
            logger.error(
                "[%s] Template %s button requires variable '%s', which isn't available for "
                "this automation - skipping that button. Available: %s",
                unit["slug"], template_name, key, list(available_fields),
            )
            values.append(None)
            continue
        values.append(available_fields[key])
    return values


async def _process_registration(unit: dict, registration_id: str, signup: dict) -> None:
    """Wraps _process_registration_inner to record every outcome (sent/
    failed/deferred) to send_log, using whatever context (phone,
    template_name, number) the inner function managed to gather before
    any failure - then always re-raises unchanged, so _poll_signup's
    existing dedup marking / MessagingLimitExceeded defer-and-retry logic
    is untouched by this."""
    ctx: dict = {"phone": None, "template_name": None, "whatsapp_number_id": None}
    try:
        await _process_registration_inner(unit, registration_id, signup, ctx)
    except MessagingLimitExceeded as exc:
        storage.record_send(
            unit_id=unit["id"], source="registration_poller", status="deferred",
            whatsapp_number_id=ctx["whatsapp_number_id"], recipient_phone=ctx["phone"],
            template_name=ctx["template_name"], error_message=str(exc), reference_id=registration_id,
        )
        raise
    except Exception as exc:
        code = exc.code if isinstance(exc, WhatsAppSendError) else None
        storage.record_send(
            unit_id=unit["id"], source="registration_poller", status="failed",
            whatsapp_number_id=ctx["whatsapp_number_id"], recipient_phone=ctx["phone"],
            template_name=ctx["template_name"], error_code=code, error_message=str(exc),
            reference_id=registration_id,
        )
        raise
    else:
        storage.record_send(
            unit_id=unit["id"], source="registration_poller", status="sent",
            whatsapp_number_id=ctx["whatsapp_number_id"], recipient_phone=ctx["phone"],
            template_name=ctx["template_name"], reference_id=registration_id,
        )


async def _process_registration_inner(
    unit: dict, registration_id: str, signup: dict, ctx: dict
) -> None:
    pco_client = get_pco_client(unit)

    detail = await pco_client.get_registration_detail(registration_id)
    reg_attrs = detail.get("data", {}).get("attributes", {})
    total_due_cents = reg_attrs.get("total_due_cents", 0)
    included = detail.get("included", [])
    contact = next((p for p in included if p["type"] == "Person"), None)
    if not contact:
        raise ValueError(f"Registration {registration_id} has no registrant_contact")

    person_id = contact["id"]
    contact_attrs = contact["attributes"]
    first_name = contact_attrs.get("first_name") or contact_attrs.get("name", "")
    last_name = contact_attrs.get("last_name", "")

    phone = await pco_client.get_person_phone(person_id)
    if not phone:
        raise ValueError(f"No phone number on file for person {person_id} ({first_name} {last_name})")

    ical_events = _ical_events_for_signup(unit, signup)

    if signup["is_paid"]:
        template = storage.get_template(unit["id"], "payment_reminder")
        if not template:
            raise ValueError(f"No payment_reminder template configured for {unit['slug']}")
        whatsapp_client = resolve_whatsapp_client(unit, template)
        ctx["template_name"] = template["template_name"]
        if whatsapp_client.number:
            ctx["whatsapp_number_id"] = whatsapp_client.number.get("id")
        # PCO's own e164 field is normally already valid (see utils/phone.py
        # docstring), but re-validate/reformat defensively here in case a
        # person's PCO record has an incomplete/malformed phone entry -
        # treat that the same as no phone on file rather than sending an
        # unvalidated string to WhatsApp.
        default_region = (whatsapp_client.number or {}).get("default_region", "ZA")
        phone = normalize_phone_e164(phone, default_region)
        if not phone:
            raise ValueError(f"Phone number on file for person {person_id} ({first_name} {last_name}) is not a valid phone number")
        ctx["phone"] = phone
        reference = build_reference(signup["name"], first_name, last_name)
        amount_due = format_amount_due(total_due_cents)

        # Stitch is an opt-in, billable module (storage.MODULE_STITCH) on
        # top of the existing per-unit "Active" checkbox (SQLAdmin's
        # Stitch Credentials) - both have to be true, since the module
        # toggle is the org's paid entitlement to use Stitch at all, while
        # the per-unit checkbox is that specific unit's own on/off switch
        # within an org that already has the module. A unit that hasn't
        # configured it, whose org hasn't enabled the module, gets no
        # payment link generated and no "Stitch Suffix" variable
        # available, rather than a hard failure.
        link_suffix = None
        if storage.is_enabled(unit["org_id"], storage.MODULE_STITCH) and storage.is_stitch_active(unit["id"]):
            stitch_client = get_stitch_client(unit)
            payer_name = build_payer_name(first_name, last_name)
            payment_link = await stitch_client.create_payment_link(
                amount_cents=total_due_cents, payer_name=payer_name, merchant_reference=reference,
            )
            link_suffix = extract_link_suffix(payment_link)

        available_fields = {
            "first_name": first_name,
            "event_name": signup["name"],
            "total_due": amount_due,
            "reference": reference,
        }
        if link_suffix is not None:
            available_fields["link_suffix"] = link_suffix
        calendar_link_suffix = _calendar_link_suffix(signup, phone, ical_events)
        if calendar_link_suffix:
            available_fields["calendar_link_suffix"] = calendar_link_suffix

        body_values = _resolve_body_values(unit, template, available_fields)

        button_variables = template.get("button_variables") or []
        if button_variables:
            button_values = _resolve_button_values(
                unit, template["template_name"], button_variables, available_fields,
            )
        elif link_suffix is not None:
            # Nothing configured yet in the Automations UI - preserve the
            # existing default of always linking to the Stitch payment link
            # (send_payment_template applies this same fallback if passed
            # None, but being explicit here keeps this function's behaviour
            # self-documenting).
            button_values = None
        else:
            # Stitch isn't active for this unit, so there's no link to
            # default the button to - leave it unset rather than filling
            # it with nothing useful.
            button_values = [None]

        await whatsapp_client.send_payment_template(
            to_phone_e164=phone,
            template_name=template["template_name"],
            registrant_first_name=first_name,
            event_name=signup["name"],
            amount_due=amount_due,
            reference=reference,
            link_suffix=link_suffix or "",
            header_image_url=template.get("header_image_url"),
            button_values=button_values,
            body_values=body_values,
        )
        logger.info(
            "[%s] Sent PAYMENT WhatsApp for registration %s (%s, ref=%s) to %s",
            unit["slug"], registration_id, signup["name"], reference, phone,
        )
    else:
        template = storage.get_template(unit["id"], "free_acknowledgment")
        if not template:
            raise ValueError(f"No free_acknowledgment template configured for {unit['slug']}")
        whatsapp_client = resolve_whatsapp_client(unit, template)
        ctx["template_name"] = template["template_name"]
        if whatsapp_client.number:
            ctx["whatsapp_number_id"] = whatsapp_client.number.get("id")
        default_region = (whatsapp_client.number or {}).get("default_region", "ZA")
        phone = normalize_phone_e164(phone, default_region)
        if not phone:
            raise ValueError(f"Phone number on file for person {person_id} ({first_name} {last_name}) is not a valid phone number")
        ctx["phone"] = phone

        available_fields = {
            "first_name": first_name,
            "event_name": signup["name"],
        }
        calendar_link_suffix = _calendar_link_suffix(signup, phone, ical_events)
        if calendar_link_suffix:
            available_fields["calendar_link_suffix"] = calendar_link_suffix

        body_values = _resolve_body_values(unit, template, available_fields)
        button_values = _resolve_button_values(
            unit, template["template_name"], template.get("button_variables") or [], available_fields,
        )

        await whatsapp_client.send_free_acknowledgment_template(
            to_phone_e164=phone,
            template_name=template["template_name"],
            registrant_first_name=first_name,
            event_name=signup["name"],
            header_image_url=template.get("header_image_url"),
            button_values=button_values,
            body_values=body_values,
        )
        logger.info(
            "[%s] Sent FREE acknowledgment WhatsApp for registration %s (%s) to %s",
            unit["slug"], registration_id, signup["name"], phone,
        )
