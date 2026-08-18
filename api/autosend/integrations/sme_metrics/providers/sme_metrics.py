"""
integrations/sme_metrics/providers/sme_metrics.py

Parser for smeMetrics (https://www.smemetrics.com) booking/appointment
notification emails. smeMetrics sends a family of plain-text emails per
booking lifecycle event, each with different field labels despite the
shared overall layout - built from two real samples (a booking request
and a cancellation). A booking-confirmed email almost certainly also
exists (the request email's own text says "a time will be confirmed with
you") but no sample of it has been seen yet, so it is deliberately NOT
registered in EMAIL_TYPES below - guessing its label wording would risk
silently mis-extracting once someone configures it. Add it here the same
way as the two below once a real sample is available.

Every email is plain text (Content-Type: text/plain), so this parses
whichever plain-text body SendGrid's Inbound Parse gives us - no HTML
handling needed for this provider.
"""

from __future__ import annotations

from autosend.integrations.sme_metrics.providers import EmailTypeSpec, UnparseableEmail

PROVIDER_KEY = "sme_metrics"
LABEL = "smeMetrics"

EMAIL_TYPES: dict[str, EmailTypeSpec] = {
    "booking_request": EmailTypeSpec(
        label="Requests",
        fields=["name", "phone", "email", "subject", "location", "time_raw", "timezone", "cancel_url"],
        phone_field="phone",
    ),
    "cancelled": EmailTypeSpec(
        label="Cancelled",
        fields=[
            "name", "phone", "email", "subject", "location", "time_raw", "timezone",
            "tracking_id", "cancellation_reason",
        ],
        phone_field="phone",
    ),
}

# "booking_confirmed" almost certainly exists (see the module docstring -
# the request email's own text says "a time will be confirmed with you")
# but no sample has been seen yet, so there's no parser for it above.
# Listed here purely so the admin UI can show a disabled "Accepted" tab in
# its correct lifecycle position (Requests -> Accepted -> Cancelled)
# instead of silently omitting a trigger staff will expect to configure
# eventually - add the real EMAIL_TYPES entry above once a sample email is
# available, then this placeholder becomes unnecessary.
PLANNED_EMAIL_TYPES: list[tuple[str, str]] = [("booking_confirmed", "Accepted")]

# Explicit display order for the admin UI's sub-tabs, combining real and
# planned types - EMAIL_TYPES' own dict insertion order doesn't say where
# an unregistered planned type belongs.
EMAIL_TYPE_TAB_ORDER: list[str] = ["booking_request", "booking_confirmed", "cancelled"]

# Label -> canonical field key, one per email_type since smeMetrics uses
# different wording for the same concept across lifecycle events (e.g.
# "Your name" on a request vs "Customer name" on a cancellation).
_BOOKING_REQUEST_LABELS = {
    "Subject": "subject",
    "Location": "location",
    "Requested time": "time_raw",
    "Time Zone": "timezone",
    "Your name": "name",
    "Your mobile phone": "phone",
    "Your email address": "email",
}

_CANCELLED_LABELS = {
    "Subject": "subject",
    "Location": "location",
    "Appointment Time": "time_raw",
    "Time Zone": "timezone",
    "Tracking ID": "tracking_id",
    "Customer name": "name",
    "Customer phone": "phone",
    "Customer email": "email",
}

# Body headings used to tell the email types apart - more reliable than
# the Subject: header, which mixes this fixed phrase with free text the
# org itself chose (e.g. "...with Janetta Boshoff Occupational Therapy
# for Wellness counselling").
_CANCELLED_HEADING = "Canceled Appointment request details"
_BOOKING_REQUEST_HEADING = "Booking request details"


def identify_email_type(text: str) -> str | None:
    if _CANCELLED_HEADING in text:
        return "cancelled"
    if _BOOKING_REQUEST_HEADING in text:
        return "booking_request"
    return None


def parse(email_type: str, text: str) -> dict[str, str]:
    if email_type == "booking_request":
        fields = _extract_labelled_fields(text, _BOOKING_REQUEST_LABELS)
        cancel_url = _value_on_next_nonblank_line(text, "Cancel the booking request:")
        if cancel_url:
            fields["cancel_url"] = cancel_url
    elif email_type == "cancelled":
        fields = _extract_labelled_fields(text, _CANCELLED_LABELS)
        reason = _value_on_next_nonblank_line(text, "Cancellation reason:")
        if reason and reason != "-":
            fields["cancellation_reason"] = reason
    else:
        raise UnparseableEmail(f"sme_metrics has no parser for email_type '{email_type}'")

    missing_required = {"name", "phone"} - fields.keys()
    if missing_required:
        raise UnparseableEmail(
            f"sme_metrics {email_type} email is missing required field(s) "
            f"{sorted(missing_required)} - template may have changed"
        )
    return fields


def _extract_labelled_fields(text: str, label_map: dict[str, str]) -> dict[str, str]:
    """Every field here is a `Label: value` line on its own - line-anchored
    literal-prefix matching, not a regex over the whole body, so a stray
    blank line or reordering elsewhere can't break unrelated fields (and
    there's no ReDoS surface, since labels are fixed strings, not
    patterns)."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        for label, key in label_map.items():
            prefix = f"{label}:"
            if stripped.startswith(prefix):
                values[key] = stripped[len(prefix):].strip()
                break
    return values


def _value_on_next_nonblank_line(text: str, heading_line: str) -> str | None:
    """For the two fields that aren't `Label: value` pairs - the
    cancel-booking URL and the free-text cancellation reason each sit on
    their own line directly under a fixed heading line."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == heading_line:
            for following in lines[i + 1:]:
                if following.strip():
                    return following.strip()
            return None
    return None
