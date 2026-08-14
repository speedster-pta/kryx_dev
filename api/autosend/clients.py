"""
Shared API clients, keyed per unit - each unit has its own
PCO and WhatsApp credentials. Clients are created lazily on first use and
cached here so we don't open a new connection pool on every webhook/poll.
Closed in main.py's lifespan shutdown.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from autosend.admin import engine, PCOOrganizationSettings
from autosend.integrations.planning_center import PlanningCenterClient
from autosend.integrations.whatsapp import WhatsAppClient

_whatsapp_clients_by_number: dict[int, WhatsAppClient] = {}
_pco_clients: dict[int, PlanningCenterClient] = {}
# PCO token id/secret are per-organisation (PCOOrganizationSettings has
# one row per org_id, not per-unit) - cached per org_id the same lazy,
# no-invalidation way as the client dicts above. Editing a token in
# SQLAdmin needs an app restart to take effect, same caveat as everywhere
# else in this file.
_pco_org_creds: dict[int, tuple[str, str]] = {}


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


def _get_pco_org_credentials(org_id: int) -> tuple[str, str]:
    if org_id not in _pco_org_creds:
        with Session(engine) as session:
            org_settings = session.execute(
                select(PCOOrganizationSettings).where(PCOOrganizationSettings.org_id == org_id)
            ).scalars().first()
        if org_settings is None:
            raise ValueError(
                f"No PCO organization settings configured for org {org_id}. Set the PCO "
                "token ID/secret in SQLAdmin under PCO Organization Settings."
            )
        _pco_org_creds[org_id] = (org_settings.pco_token_id, org_settings.pco_token_secret)
    return _pco_org_creds[org_id]


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
        token_id, token_secret = _get_pco_org_credentials(unit["org_id"])
        _pco_clients[cid] = PlanningCenterClient(
            token_id=token_id,
            token_secret=token_secret,
            campus_id=unit["pco_campus_id"],
        )
    return _pco_clients[cid]


async def close_clients() -> None:
    for client in _whatsapp_clients_by_number.values():
        await client.client.aclose()
    for client in _pco_clients.values():
        await client.client.aclose()
