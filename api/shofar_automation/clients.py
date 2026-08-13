"""
Shared API clients, keyed per unit - each unit has its own
PCO and WhatsApp credentials. Clients are created lazily on first use and
cached here so we don't open a new connection pool on every webhook/poll.
Closed in main.py's lifespan shutdown.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from shofar_automation.admin import engine, PCOOrganizationSettings
from shofar_automation.integrations.planning_center import PlanningCenterClient
from shofar_automation.integrations.whatsapp import WhatsAppClient

_whatsapp_clients: dict[int, WhatsAppClient] = {}
_whatsapp_clients_by_number: dict[int, WhatsAppClient] = {}
_pco_clients: dict[int, PlanningCenterClient] = {}
# PCO token id/secret are org-wide now (PCOOrganizationSettings singleton
# in admin.py), not per-unit - cached the same lazy,
# no-invalidation way as the client dicts above. Editing the token in
# SQLAdmin needs an app restart to take effect, same caveat as everywhere
# else in this file.
_pco_org_creds: tuple[str, str] | None = None


def _resolve_primary_number(unit: dict) -> dict | None:
    """get_whatsapp_client() only receives the unit dict, which
    carries the primary number's access_token/phone_number_id flattened
    onto it but not waba_id - and waba_id is what the messaging-limit
    tracker actually keys on (see whatsapp_limits._limit_key). Look the
    full WhatsAppNumber row back up so WhatsAppClient gets everything it
    needs to gate/log against the right pool.

    If no number is flagged `is_primary` for this unit (e.g. it
    was never set during the multi-number migration), fall back to the
    single active number if there's exactly one - this keeps the send
    gated/logged instead of silently bypassing the 24h limit tracker,
    which is what happened before this fallback existed. If that's still
    ambiguous, return None and log loudly so it's visible in the logs
    rather than discovered later as an unexplained ungated send."""
    from shofar_automation import storage
    from shofar_automation.utils.logging import get_logger

    logger = get_logger(__name__)
    numbers = storage.get_whatsapp_numbers(None)
    unit_numbers = [n for n in numbers if n["unit_id"] == unit["id"]]

    matches = [n for n in unit_numbers if n.get("is_primary")]
    if matches:
        return matches[0]

    active = [n for n in unit_numbers if n.get("active")]
    if len(active) == 1:
        logger.warning(
            "[%s] No WhatsApp number is marked primary - falling back to the "
            "single active number '%s'. Mark a primary number in SQLAdmin "
            "under WhatsApp Numbers to fix this properly.",
            unit.get("slug", unit["id"]), active[0].get("label", active[0]["id"]),
        )
        return active[0]

    logger.error(
        "[%s] Can't resolve a primary WhatsApp number: none marked primary "
        "and %d active candidate(s) found. Sends via the unit-primary "
        "fallback path will be UNGATED (no 24h messaging-limit tracking) "
        "until a primary number is set in SQLAdmin under WhatsApp Numbers.",
        unit.get("slug", unit["id"]), len(active),
    )
    return None


def get_whatsapp_client(unit: dict) -> WhatsAppClient:
    cid = unit["id"]
    if cid not in _whatsapp_clients:
        if not unit.get("whatsapp_access_token") or not unit.get("whatsapp_phone_number_id"):
            raise ValueError(
                f"Unit '{unit.get('slug', cid)}' has no primary WhatsApp number "
                "configured. Set one as primary in SQLAdmin under WhatsApp Numbers."
            )
        number = _resolve_primary_number(unit)
        client = WhatsAppClient(
            access_token=unit["whatsapp_access_token"],
            phone_number_id=unit["whatsapp_phone_number_id"],
            number=number,
        )
        if number is None:
            # Deliberately not cached: this client is ungated/unlogged, and
            # caching it here would lock that in until an app restart even
            # after someone fixes the primary-number flag in SQLAdmin.
            # Every call re-resolves until _resolve_primary_number succeeds.
            return client
        _whatsapp_clients[cid] = client
    return _whatsapp_clients[cid]


def get_whatsapp_client_for_number(number: dict) -> WhatsAppClient:
    """Same idea as get_whatsapp_client() above, but for a specific
    whatsapp_numbers row rather than always the unit's primary -
    this is what lets each Automations entry (Free/Paid Registration or
    Form Response) send from whichever number was picked for it."""
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
    """The number an automation actually sends from: the one explicitly
    picked for it on the Automations page (template["whatsapp_number_id"]),
    falling back to the unit's primary number for older
    automations saved before that field existed, or if the selected
    number was later deleted/deactivated."""
    from shofar_automation import storage
    from shofar_automation.utils.logging import get_logger

    logger = get_logger(__name__)
    number_id = template.get("whatsapp_number_id")
    if number_id:
        number = storage.get_whatsapp_number_by_id(number_id)
        if number and number.get("active"):
            return get_whatsapp_client_for_number(number)
        logger.warning(
            "[%s] Automation '%s' points at WhatsApp number id %s, which is missing or "
            "inactive - falling back to the unit's primary number",
            unit.get("slug", unit.get("id")), template.get("template_name"), number_id,
        )
    return get_whatsapp_client(unit)


def _get_pco_org_credentials() -> tuple[str, str]:
    global _pco_org_creds
    if _pco_org_creds is None:
        with Session(engine) as session:
            org_settings = session.execute(select(PCOOrganizationSettings)).scalars().first()
        if org_settings is None:
            raise ValueError(
                "No PCO organization settings configured. Set the PCO token ID/secret "
                "in SQLAdmin under PCO Organization Settings."
            )
        _pco_org_creds = (org_settings.pco_token_id, org_settings.pco_token_secret)
    return _pco_org_creds


def get_pco_client(unit: dict) -> PlanningCenterClient:
    cid = unit["id"]
    if cid not in _pco_clients:
        if not unit.get("pco_campus_id"):
            raise ValueError(
                f"Unit '{unit.get('slug', cid)}' has no PCO campus configured, "
                "so PCO automation (registration polling, form responses) can't run for it. "
                "Set one in SQLAdmin under Units, or use the campaign sender instead, "
                "which doesn't need a PCO campus."
            )
        token_id, token_secret = _get_pco_org_credentials()
        _pco_clients[cid] = PlanningCenterClient(
            token_id=token_id,
            token_secret=token_secret,
            campus_id=unit["pco_campus_id"],
        )
    return _pco_clients[cid]


async def close_clients() -> None:
    for client in _whatsapp_clients.values():
        await client.client.aclose()
    for client in _whatsapp_clients_by_number.values():
        await client.client.aclose()
    for client in _pco_clients.values():
        await client.client.aclose()
