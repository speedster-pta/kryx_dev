"""Meta template-list fetch, shared by bulk campaigns/onboarding
(web/whatsapp_bulk.py re-exports fetch_templates from here for
numbers_router.py) and by the transactional/serving-reminder automation
paths (services/*.py) that need a template's raw BODY text purely to
render a real display string when mirroring a successful send into the
Inbox (see storage.mirror_outbound_to_inbox and
template_variables.render_template_body).

Kept synchronous (requests, not httpx) - this was moved out of
web/whatsapp_bulk.py, whose own docstring explains why bulk sends use a
sync client. Async callers on the event loop must run these via
anyio.to_thread.run_sync, the same way integrations/whatsapp.py's own
_gate() offloads its sync Meta calls.
"""
import logging

import requests

from autosend.integrations.whatsapp import BASE_URL as _ASYNC_BASE_URL

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


def get_template_body_text(
    token: str, waba_id: str, template_name: str, cache: dict | None = None,
) -> str | None:
    """Returns the approved template's raw BODY text (with {{n}}
    placeholders), for rendering a real display string when mirroring an
    automated/campaign send into the Inbox (see
    template_variables.render_template_body). `cache`, if given, is a
    plain dict the caller owns and reuses across every recipient in one
    run/batch - keyed by waba_id, holding that WABA's full template list -
    so a reminder rule or campaign addressing hundreds of people fetches
    Meta's template list once per run, not once per recipient. Returns
    None (never raises) on any failure - this is display-only enrichment
    for a send that has already gone out, and must never block or fail
    the caller."""
    if not token or not waba_id:
        # Nothing to fetch from - e.g. a number that hasn't completed
        # onboarding yet, or (in tests) a fake number with no real
        # credentials. Skip the network round trip entirely rather than
        # attempting (and failing) a call Meta would reject anyway.
        return None
    try:
        if cache is not None and waba_id in cache:
            templates = cache[waba_id]
        else:
            templates = fetch_templates(token, waba_id)
            if cache is not None:
                cache[waba_id] = templates
    except Exception:
        logger.warning(
            "Failed to fetch templates from Meta for Inbox display rendering (waba_id=%s)",
            waba_id, exc_info=True,
        )
        return None

    for template in templates:
        if template.get("name") != template_name:
            continue
        for component in template.get("components", []):
            if component.get("type") == "BODY":
                return component.get("text")
    return None
