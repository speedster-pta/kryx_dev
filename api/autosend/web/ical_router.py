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

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from autosend import storage
from autosend.integrations.ical.builder import build_ics
from autosend.utils.logging import get_logger
from autosend.web import ical_link_security
from autosend.web.login_security import get_client_ip

logger = get_logger(__name__)

router = APIRouter()

_NOT_FOUND = PlainTextResponse("Not found", status_code=404)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        headers={"Content-Disposition": 'attachment; filename="event.ics"'},
    )
