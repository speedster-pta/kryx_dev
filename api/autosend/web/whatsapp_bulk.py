"""WhatsApp Graph API calls for bulk campaigns.

Ported from wa-campaign-manager's whatsapp_client.py. Kept as a separate,
synchronous (requests-based) client rather than reusing
integrations/whatsapp.py's async WhatsAppClient, because campaign sends run
in a background thread (see campaigns_router._run_campaign) so they don't
block the request/reply cycle or tie up the asyncio event loop for
potentially thousands of sequential, rate-limited sends.

Uses the graph API version pinned on autosend.integrations.whatsapp
so both the transactional and bulk paths stay in sync.
"""
import logging
import requests

from autosend.config import settings
from autosend.integrations.whatsapp import BASE_URL as _ASYNC_BASE_URL
from autosend.integrations.whatsapp_payload import build_button_components, sanitize_param_text

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
API_VERSION = _ASYNC_BASE_URL.rsplit("/", 1)[-1]  # e.g. "v21.0"


def fetch_templates(token: str, waba_id: str):
    url = f"{GRAPH_BASE}/{API_VERSION}/{waba_id}/message_templates"
    params = {"limit": 100}
    headers = {"Authorization": f"Bearer {token}"}
    templates = []

    while url:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch templates: {resp.status_code} {resp.text}")
        data = resp.json()
        templates.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = None

    return templates


def upload_media(token: str, phone_number_id: str, file_bytes: bytes, filename: str, mime_type: str):
    if settings.dry_run:
        logger.info("[SIMULATION MODE / DRY RUN] Intercepted media upload for %s (%s)", filename, mime_type)
        return "simulated_media_id_12345"

    url = f"{GRAPH_BASE}/{API_VERSION}/{phone_number_id}/media"
    headers = {"Authorization": f"Bearer {token}"}
    files = {"file": (filename, file_bytes, mime_type)}
    data = {"messaging_product": "whatsapp", "type": mime_type}
    resp = requests.post(url, headers=headers, data=data, files=files, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Media upload failed: {resp.status_code} {resp.text}")
    return resp.json()["id"]


def build_payload(phone, template_name, lang, body_values, image_media_id=None, button_values=None):
    """button_values, if given, is a list parallel to the template's button
    list (one entry per button position) - only entries for dynamic URL
    buttons need a value, everything else should be falsy (None or '')."""
    components = []
    if image_media_id:
        components.append({
            "type": "header",
            "parameters": [{"type": "image", "image": {"id": image_media_id}}],
        })
    if body_values:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": sanitize_param_text(v)} for v in body_values],
        })
    components.extend(build_button_components(button_values))
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
            "components": components,
        },
    }


def send_message(token: str, phone_number_id: str, payload: dict):
    if settings.dry_run:
        to_phone = payload.get("to", "unknown")
        logger.info("[SIMULATION MODE / DRY RUN] Intercepted bulk message to %s: %s", to_phone, payload)
        return True, {
            "messaging_product": "whatsapp",
            "contacts": [{"input": to_phone, "wa_id": to_phone}],
            "messages": [{"id": f"wamid.simulated_bulk_{to_phone}"}],
        }

    url = f"{GRAPH_BASE}/{API_VERSION}/{phone_number_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code == 200:
        return True, resp.json()
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text}
    return False, body

