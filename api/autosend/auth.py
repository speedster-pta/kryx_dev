"""
Auth for the /ops/* diagnostic routes (X-Admin-Key header).

Simple shared-secret check via the X-Admin-Key header, not a full auth
system - this is a single-operator internal tool sitting behind nginx on
a public domain, so the goal is just "not wide open to anyone who finds
the URL", not multi-user access control.
"""

import hmac

from fastapi import Header, HTTPException

from autosend.config import settings


async def require_admin_key(x_admin_key: str | None = Header(default=None)) -> None:
    if not x_admin_key or not hmac.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Admin-Key")


async def require_booking_service_key(x_service_key: str | None = Header(default=None)) -> None:
    """Gates POST /integrations/external-send - see
    settings.booking_service_api_key's docstring for why this is a
    separate key from admin_api_key rather than reusing it. Blank
    booking_service_api_key (the default) always rejects, same "not
    configured yet" behaviour as an empty admin_api_key would."""
    if not settings.booking_service_api_key or not x_service_key or not hmac.compare_digest(
        x_service_key, settings.booking_service_api_key
    ):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Service-Key")
