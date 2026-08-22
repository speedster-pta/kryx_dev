"""
Shared API clients, keyed per unit - each unit has its own
PCO and WhatsApp credentials. Clients are created lazily on first use and
cached here so we don't open a new connection pool on every webhook/poll.
Closed in main.py's lifespan shutdown.
"""

from datetime import datetime, timedelta, timezone

from autosend.integrations.planning_center import PlanningCenterClient
from autosend.integrations.stitch import StitchClient
from autosend.integrations.whatsapp import WhatsAppClient

_whatsapp_clients_by_number: dict[int, WhatsAppClient] = {}
_pco_clients: dict[int, PlanningCenterClient] = {}
# Org-wide PCO clients (get_pco_org_client) - keyed by org_id rather than
# unit id like _pco_clients above, for calls that aren't scoped to any
# one unit's campus.
_pco_org_clients: dict[int, PlanningCenterClient] = {}
_stitch_clients: dict[int, StitchClient] = {}
# PCO credentials are per-organisation (PCOOrganizationSettings has one
# row per org_id, not per-unit) - cached per org_id the same lazy,
# no-invalidation way as the client dicts above. Editing a PAT in
# SQLAdmin needs an app restart to take effect, same caveat as everywhere
# else in this file. For an OAuth-connected org this holds the current
# access token too, which - unlike a PAT - genuinely does change over
# time (refreshed below); the cached client's Authorization header is
# updated in place via set_bearer_token() rather than needing a restart.
_pco_org_creds: dict[int, dict] = {}

# How much slack to leave before an OAuth access token's real expiry
# before treating it as due for refresh - avoids a request racing an
# expiry that happens mid-flight.
_OAUTH_REFRESH_SKEW_MINUTES = 5


def get_whatsapp_client_for_number(number: dict) -> WhatsAppClient:
    """Builds/caches a client for one specific whatsapp_numbers row - this
    is what lets each Automations entry (Free/Paid Registration or Form
    Response) or campaign send from whichever number was explicitly picked
    for it."""
    number_id = number["id"]
    if number_id not in _whatsapp_clients_by_number:
        if not number.get("access_token") or not number.get("phone_number_id"):
            raise ValueError(
                f"WhatsApp number '{number.get('label', number_id)}' has no access token on "
                "file yet. Add one in SQLAdmin under WhatsApp Numbers."
            )
        _whatsapp_clients_by_number[number_id] = WhatsAppClient(
            access_token=number["access_token"],
            phone_number_id=number["phone_number_id"],
            number=number,
        )
    return _whatsapp_clients_by_number[number_id]


def resolve_whatsapp_client(unit: dict, template: dict) -> WhatsAppClient:
    """The number an automation sends from: the one explicitly picked for
    it on the Automations page (template["whatsapp_number_id"]). There is
    no default/fallback number - if none was picked, or the one that was
    picked has since been deleted/deactivated, this raises rather than
    guessing which number to send from. Callers already treat this the
    same as any other pre-send failure (see registration_poller.py/
    serving_reminder.py/form_response.py)."""
    from autosend import storage

    number_id = template.get("whatsapp_number_id")
    if not number_id:
        raise ValueError(
            f"[{unit.get('slug', unit.get('id'))}] Automation '{template.get('template_name')}' "
            "has no WhatsApp number selected. Choose one on the Automations page before this can send."
        )
    number = storage.get_whatsapp_number_by_id(number_id)
    if not number or not number.get("active"):
        raise ValueError(
            f"[{unit.get('slug', unit.get('id'))}] Automation '{template.get('template_name')}' "
            f"points at WhatsApp number id {number_id}, which is missing or inactive. "
            "Choose an active number on the Automations page before this can send."
        )
    return get_whatsapp_client_for_number(number)


def _refresh_pco_oauth_token(org_id: int, refresh_token: str) -> dict:
    """Synchronous refresh call (plain httpx, not the app's async client) -
    deliberately kept sync so get_pco_client below doesn't have to become
    async and ripple out to every one of its call sites
    (automations_router.py, registration_poller.py, serving_reminder.py,
    form_response.py), none of which need anything else about this call
    to be awaited. Raises ValueError if the platform's own PCO OAuth app
    isn't configured - same "fail loudly with a clear admin-facing
    message" convention as every other credential-missing case in this
    file."""
    import httpx as _httpx
    from autosend import storage
    from autosend.integrations.planning_center_oauth import TOKEN_URL

    platform_settings = storage.get_pco_platform_settings()
    if platform_settings is None:
        raise ValueError(
            "No PCO OAuth app configured. Set the client ID/secret in SQLAdmin "
            "under PCO Platform Settings."
        )
    response = _httpx.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": platform_settings["client_id"],
            "client_secret": platform_settings["client_secret"],
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=data["expires_in"])
    ).isoformat()
    storage.save_pco_oauth_tokens(
        org_id, data["access_token"], data["refresh_token"], expires_at
    )
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": expires_at,
    }


def _get_pco_org_credentials(org_id: int) -> dict:
    """Returns {"auth_method": "pat", "token_id": ..., "token_secret": ...}
    or {"auth_method": "oauth", "access_token": ...} - refreshing the
    OAuth access token first if it's at/near expiry. Cached per org_id;
    an OAuth refresh updates both the DB (via storage.save_pco_oauth_tokens)
    and this cache, then also pushes the new token onto any already-built
    PlanningCenterClient for a unit in this org (see get_pco_client)."""
    from autosend import storage

    cached = _pco_org_creds.get(org_id)
    if cached is None:
        org_settings = storage.get_pco_org_settings(org_id)
        if org_settings is None:
            raise ValueError(
                f"No PCO organization settings configured for org {org_id}. Connect via "
                "Planning Center OAuth, or set a token ID/secret, in SQLAdmin under "
                "PCO Organization Settings."
            )
        if org_settings["pco_auth_method"] == "oauth":
            cached = {
                "auth_method": "oauth",
                "access_token": org_settings["pco_access_token"],
                "refresh_token": org_settings["pco_refresh_token"],
                "expires_at": org_settings["pco_token_expires_at"],
            }
        else:
            cached = {
                "auth_method": "pat",
                "token_id": org_settings["pco_token_id"],
                "token_secret": org_settings["pco_token_secret"],
            }
        _pco_org_creds[org_id] = cached

    if cached["auth_method"] == "oauth":
        expires_at = cached.get("expires_at")
        due_for_refresh = (
            not expires_at
            or datetime.fromisoformat(expires_at)
            <= datetime.now(timezone.utc) + timedelta(minutes=_OAUTH_REFRESH_SKEW_MINUTES)
        )
        if due_for_refresh:
            refreshed = _refresh_pco_oauth_token(org_id, cached["refresh_token"])
            cached.update(auth_method="oauth", **refreshed)
            for client in list(_pco_clients.values()) + list(_pco_org_clients.values()):
                if getattr(client, "org_id", None) == org_id:
                    client.set_bearer_token(refreshed["access_token"])
    return cached


def _build_pco_client(creds: dict, org_id: int, campus_id: str) -> PlanningCenterClient:
    if creds["auth_method"] == "oauth":
        client = PlanningCenterClient(campus_id=campus_id, access_token=creds["access_token"])
    else:
        client = PlanningCenterClient(
            campus_id=campus_id, token_id=creds["token_id"], token_secret=creds["token_secret"],
        )
    client.org_id = org_id
    return client


def get_pco_client(unit: dict) -> PlanningCenterClient:
    cid = unit["id"]
    creds = _get_pco_org_credentials(unit["org_id"])
    if cid not in _pco_clients:
        if not unit.get("pco_campus_id"):
            raise ValueError(
                f"Unit '{unit.get('slug', cid)}' has no PCO campus configured, "
                "so PCO automation (registration polling, form responses) can't run for it. "
                "Set one in SQLAdmin under Units, or use the campaign sender instead, "
                "which doesn't need a PCO campus."
            )
        _pco_clients[cid] = _build_pco_client(creds, unit["org_id"], unit["pco_campus_id"])
    return _pco_clients[cid]


def get_pco_org_client(org_id: int) -> PlanningCenterClient:
    """Same cached-client machinery as get_pco_client, but for calls that
    are genuinely org-wide rather than scoped to one unit's campus - right
    now just PlanningCenterClient.get_campuses() (the "pick your campus"
    dropdown on the PCO Settings page, admin_org_pages.py). Deliberately
    doesn't require any unit to have a pco_campus_id set yet - that would
    be backwards for this call, since listing campuses is how an admin
    finds the id to set in the first place. campus_id is left blank on
    the client this builds; every method that actually depends on it
    (get_eligible_signups, get_service_types_for_campus) is simply never
    called through this path."""
    if org_id not in _pco_org_clients:
        creds = _get_pco_org_credentials(org_id)
        _pco_org_clients[org_id] = _build_pco_client(creds, org_id, campus_id="")
    return _pco_org_clients[org_id]


def get_stitch_client(unit: dict) -> StitchClient:
    """Builds/caches a StitchClient for one unit's own Stitch Express
    credentials (SQLAdmin's Stitch Credentials page) - what
    registration_poller.py calls to generate a real payment link for a
    paid registration's payment reminder."""
    from autosend import storage

    unit_id = unit["id"]
    if unit_id not in _stitch_clients:
        creds = storage.get_stitch_credentials(unit_id)
        if not creds or not creds.get("client_secret"):
            raise ValueError(
                f"Unit '{unit.get('slug', unit_id)}' has no Stitch credentials configured, "
                "so payment links can't be generated. Add one in SQLAdmin under Stitch Credentials."
            )
        _stitch_clients[unit_id] = StitchClient(
            client_id=creds["client_id"],
            client_secret=creds["client_secret"],
        )
    return _stitch_clients[unit_id]


async def close_clients() -> None:
    for client in _whatsapp_clients_by_number.values():
        await client.client.aclose()
    for client in _pco_clients.values():
        await client.client.aclose()
    for client in _pco_org_clients.values():
        await client.client.aclose()
    for client in _stitch_clients.values():
        await client.client.aclose()
