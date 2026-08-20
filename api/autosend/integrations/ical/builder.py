"""
integrations/ical/builder.py

Renders one or more ical_events rows into a single RFC 5545 iCalendar
(.ics) document - one VCALENDAR containing one VEVENT per event, so
"Add to calendar" imports all of them in one tap (e.g. a volunteer's
whole month of scheduled services combined into one link - see
services/serving_reminder.py). A single-event link is just the
one-element-list case; there's no separate "single" vs "bundle" function.

Hand-rolled rather than a dependency (e.g. the `icalendar` package) -
this app's requirements.txt has no calendar library, and the format
needed here (no recurrence, no attendees/RSVP - see storage/ical.py's
docstring on why ORGANIZER/ATTENDEE is out of scope) is small enough not
to justify adding one.

starts_at/ends_at on ical_events are expected to already be timezone-aware
ISO 8601 strings (any offset) - normalising a source's local time to UTC
is each upstream provider's job (see storage/ical.py), not this module's,
since only the provider knows the source's own timezone convention.
"""

from __future__ import annotations

from datetime import datetime, timezone

_PRODID = "-//Kryx//iCalendar//EN"


def _to_utc_stamp(value: str) -> str:
    """ISO 8601 (any offset, or naive-as-UTC) -> RFC 5545 UTC DATE-TIME
    ('20260901T150000Z'). Naive datetimes are treated as already UTC
    (upstream providers are expected to normalise before calling
    storage.ical, so this is a defensive fallback, not the primary path)."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape_text(value: str) -> str:
    """RFC 5545 §3.3.11 TEXT escaping - backslash, semicolon, comma, and
    newline are the only characters that need it."""
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 §3.1 line folding: no content line may exceed 75 octets;
    continuation lines start with a single space. Splits on byte length
    (UTF-8), not character count, per the spec."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    parts = []
    start = 0
    limit = 75
    while start < len(encoded):
        # Back off from a split that would land mid-codepoint (a UTF-8
        # continuation byte has its top bit set and its second-highest
        # bit clear, i.e. 0b10xxxxxx).
        end = min(start + limit, len(encoded))
        while end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        parts.append(encoded[start:end].decode("utf-8"))
        start = end
        limit = 74  # continuation lines lose one octet to the leading space
    return "\r\n ".join(parts)


def _render_vevent(event: dict, dtstamp: str) -> list[str]:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{event['uid']}",
        f"SEQUENCE:{event['sequence']}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART:{_to_utc_stamp(event['starts_at'])}",
    ]
    if event.get("ends_at"):
        lines.append(f"DTEND:{_to_utc_stamp(event['ends_at'])}")
    lines.append(f"SUMMARY:{_escape_text(event['title'])}")
    if event.get("location"):
        lines.append(f"LOCATION:{_escape_text(event['location'])}")
    if event.get("description"):
        lines.append(f"DESCRIPTION:{_escape_text(event['description'])}")
    lines.append(f"STATUS:{'CANCELLED' if event.get('status') == 'cancelled' else 'CONFIRMED'}")
    lines.append("END:VEVENT")
    return lines


def build_ics(events: list[dict]) -> str:
    """events: one or more rows from storage.ical (uid, sequence, title,
    description, location, starts_at, ends_at, status), each rendered as
    its own VEVENT sharing one VCALENDAR - order is preserved as given
    (callers pass them pre-sorted by starts_at, see
    storage.get_ical_link_with_events). Returns the full .ics document as
    a string, CRLF line endings per RFC 5545 §1."""
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    for event in events:
        lines.extend(_render_vevent(event, dtstamp))
    lines.append("END:VCALENDAR")

    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
