"""
web/ical_router.py

Public, unauthenticated endpoint that serves a generated .ics file for a
given bearer token. No session/org auth here by design (see
integrations/ical/schema.py's docstring on ical_links.token) - the token
itself is the entire authorization boundary, so this router's only real
job is: don't leak whether a token ever existed, and rate-limit genuine
guessing (web/ical_link_security.py).

A link's own expiry/revocation gates the whole response; the individual
events it bundles are served as-is regardless of whether their own
starts_at has already passed (deliberate choice - a monthly digest link
keeps serving its full original set of services all month, rather than
shrinking as dates go by).

Registered in main.py before setup_admin(app), like every other plain
route in this app.
"""

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.templating import Jinja2Templates

from autosend import storage
from autosend.integrations.ical.builder import build_ics
from autosend.utils.logging import get_logger
from autosend.web import ical_link_security
from autosend.web.login_security import get_client_ip

logger = get_logger(__name__)

router = APIRouter()

# Own Jinja2Templates instance pointed at the same directory main.py uses -
# importing main.py's instance directly would be a circular import (see
# signup_router.py for the same pattern).
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "web" / "sqladmin_theme"))

_NOT_FOUND = PlainTextResponse("Not found", status_code=404)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gcal_stamp(value: str) -> str:
    """Same UTC DATE-TIME format Google Calendar's render URL expects for
    `dates=` - identical rule to builder.py's _to_utc_stamp (any-offset or
    naive-as-UTC ISO 8601 in, 'YYYYMMDDTHHMMSSZ' out), duplicated rather
    than imported since that one is builder.py's private helper."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _google_calendar_url(event: dict) -> str:
    """Google Calendar has no reliable way to import an arbitrary .ics on
    Android (no universal "open .ics in Calendar" intent), but does support
    a direct "add this one event" URL - only for a single event, hence this
    is only offered by the landing route when a link bundles exactly one."""
    start = _gcal_stamp(event["starts_at"])
    end = _gcal_stamp(event["ends_at"]) if event.get("ends_at") else start
    params = {"action": "TEMPLATE", "text": event["title"], "dates": f"{start}/{end}"}
    if event.get("location"):
        params["location"] = event["location"]
    if event.get("description"):
        params["details"] = event["description"]
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


@router.get("/ical/{token}.ics")
async def get_ical_file(token: str, request: Request):
    client_ip = get_client_ip(request)

    if ical_link_security.check_lockout(client_ip) is not None:
        return _NOT_FOUND

    link = storage.get_ical_link_with_events(token)

    if not link or not link["events"]:
        # No events attached is treated the same as not-found (shouldn't
        # normally happen - see get_ical_link_with_events's docstring -
        # but there is nothing useful to serve either way).
        ical_link_security.record_invalid_token(client_ip)
        ical_link_security.log_access("ical_link_not_found", request, token_prefix=token[:8])
        return _NOT_FOUND

    now_iso = _now_iso()

    if link["revoked_at"] or link["expires_at"] < now_iso:
        # A real link that's simply expired/revoked is NOT a guessing
        # signal - do not record it as a failed attempt (see
        # ical_link_security's docstring).
        ical_link_security.log_access("ical_link_expired_or_revoked", request, token_prefix=token[:8])
        return _NOT_FOUND

    storage.mark_ical_link_accessed(link["id"])
    ical_link_security.log_access(
        "ical_link_served", request, token_prefix=token[:8], event_count=len(link["events"]),
    )

    ics_body = build_ics(link["events"])
    return Response(
        content=ics_body,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="event.ics"'},
    )


@router.get("/ical/{token}")
async def get_ical_landing(token: str, request: Request):
    """Human-facing "Add to calendar" landing page - what WhatsApp message
    templates should link to instead of the raw .ics endpoint above.
    Android has no reliable way to hand an arbitrary .ics file off to a
    calendar app, so this offers a direct Google Calendar link (only
    possible for a single-event link - Google's render URL takes one event)
    plus the .ics download as a fallback for iOS/Outlook/desktop."""
    client_ip = get_client_ip(request)

    if ical_link_security.check_lockout(client_ip) is not None:
        return _NOT_FOUND

    link = storage.get_ical_link_with_events(token)

    if not link or not link["events"]:
        ical_link_security.record_invalid_token(client_ip)
        ical_link_security.log_access("ical_link_not_found", request, token_prefix=token[:8])
        return _NOT_FOUND

    now_iso = _now_iso()

    if link["revoked_at"] or link["expires_at"] < now_iso:
        ical_link_security.log_access("ical_link_expired_or_revoked", request, token_prefix=token[:8])
        return _NOT_FOUND

    storage.mark_ical_link_accessed(link["id"])
    ical_link_security.log_access(
        "ical_link_served", request, token_prefix=token[:8], event_count=len(link["events"]),
    )

    events = link["events"]
    google_url = _google_calendar_url(events[0]) if len(events) == 1 else None

    return templates.TemplateResponse(
        request,
        "ical_add_to_calendar.html",
        {
            "google_url": google_url,
            "ics_url": f"/ical/{token}.ics",
            "event_count": len(events),
            "single_title": events[0]["title"] if len(events) == 1 else None,
        },
    )
