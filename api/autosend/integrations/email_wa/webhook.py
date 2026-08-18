"""
integrations/email_wa/webhook.py

SendGrid Inbound Parse delivers here. Unlike PCO/Meta's webhooks
(integrations/webhooks.py), Inbound Parse has no signing scheme at all -
there's no HMAC to verify a payload against - and it's configured per MX
hostname, not per unit/org, so there is exactly one URL for every
organisation's inbound mail rather than one per unit. Consequences of
that, from the design discussion this module was built from:
  - the {secret} path segment is a platform-wide shared secret
    (settings.generic_email_wa_webhook_secret) - the same "not a real
    authz boundary, just a bar against opportunistic discovery" caveat as
    auth.py's admin_api_key, not a substitute for it
  - the per-integration local_part token is what actually has to be
    unguessable, since it's the only thing distinguishing one
    org/unit/email_type's mail from another's once a request reaches
    this URL at all

Deliberately a separate route/secret/MX hostname from
integrations/sme_metrics/webhook.py, even though the logic is nearly
identical - Inbound Parse routes by hostname, and these are two
independent, independently-gated modules with their own receiving
addresses (see integrations/email_wa/__init__.py). Needs its own SendGrid
Inbound Parse route configured against generic_email_wa_inbound_domain
before it can receive real mail - this module has no providers registered
yet (see providers/__init__.py), so there is nothing for it to route to
in the meantime regardless.
"""

import hashlib
import hmac
import re

from email.utils import parseaddr

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException

from autosend.config import settings
from autosend.services.email_wa import process_inbound_email
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/webhooks/generic-email", tags=["email_wa"])

_MESSAGE_ID_RE = re.compile(r"^Message-ID:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _dedup_key(to_addr: str, from_addr: str, subject: str, raw_headers: str) -> str:
    """Message-Id when SendGrid's `headers` field has one - falling back
    to a hash of the rest of the envelope, since Inbound Parse doesn't
    guarantee a stable message ID. Same weaker-than-processed_form_submissions
    tradeoff as integrations/sme_metrics/webhook.py, not an oversight."""
    match = _MESSAGE_ID_RE.search(raw_headers) if raw_headers else None
    if match:
        return match.group(1).strip()
    basis = f"{to_addr}|{from_addr}|{subject}|{raw_headers}"
    return "sha256:" + hashlib.sha256(basis.encode()).hexdigest()


@router.post("/inbound/{secret}")
async def inbound_email(
    secret: str,
    background_tasks: BackgroundTasks,
    to: str = Form(""),
    sender: str = Form("", alias="from"),
    subject: str = Form(""),
    text: str = Form(""),
    headers: str = Form(""),
):
    if not hmac.compare_digest(secret, settings.generic_email_wa_webhook_secret):
        # 404, not 401 - this route's only defence is that its path
        # segment isn't guessable, so a wrong value should look like "this
        # route doesn't exist" rather than confirm a valid-but-unauthorised
        # path exists.
        raise HTTPException(status_code=404, detail="Not found")

    _, to_addr = parseaddr(to)
    local_part = to_addr.split("@", 1)[0] if "@" in to_addr else ""
    if not local_part:
        logger.warning("Inbound email with no parseable recipient address: to=%r", to)
        return {"status": "accepted"}

    dedup_key = _dedup_key(to_addr, sender, subject, headers)

    # Ack immediately - SendGrid retries Inbound Parse on a non-2xx, same
    # reasoning as the PCO people-form webhook: the real work (integration
    # lookup, parsing, WhatsApp send) is too slow to do inline safely.
    background_tasks.add_task(process_inbound_email, local_part, text, dedup_key)

    return {"status": "accepted"}
