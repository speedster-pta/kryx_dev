import time

import httpx

# Fixed Stitch Money base URL that a full payment link is always prefixed
# with. Named here (rather than left as a docstring-only comment) so admin
# pages that need to show the full link to a user building a
# template - e.g. the "Calendar invite"-style presets in the WhatsApp
# template builder - have one canonical constant to import instead of
# re-typing the literal.
STITCH_BASE_URL = "https://express.stitch.money/"

# ASSUMPTION - VERIFY against Stitch's own API docs / a live test call
# before relying on this in production: OAuth2 client-credentials token
# endpoint and the payment-links endpoint are both hosted under
# STITCH_BASE_URL's api/v1 path. Adjust if Stitch documents a different
# host/path for either.
_TOKEN_URL = STITCH_BASE_URL + "api/v1/oauth/token"
_PAYMENT_LINKS_URL = STITCH_BASE_URL + "api/v1/payment-links"

# Stitch issues a token valid for 15 minutes - refreshed a little early
# (30s margin) rather than waiting for a 401 mid-request.
_TOKEN_LIFETIME_SECONDS = 15 * 60
_TOKEN_REFRESH_MARGIN_SECONDS = 30


def build_reference(event_name: str, first_name: str, last_name: str) -> str:
    """
    Human-typeable reference for manual EFT/Stitch entry, e.g.
    "Men's Camp" + "John" + "Doe" -> "MEN-JDoe"

    First 3 alphabetic characters of the event name (uppercased) +
    underscore + first initial + surname (spaces/punctuation stripped
    from surname so double-barrelled names stay reasonably short).
    """
    alpha_only = "".join(ch for ch in event_name if ch.isalpha())
    event_code = alpha_only[:3].upper() or "EVT"
    initial = (first_name[:1] or "").upper()
    surname = "".join(ch for ch in last_name if ch.isalpha())
    return f"{event_code}-{initial}{surname}"


def format_amount_due(total_due_cents: int) -> str:
    """Cents -> display string for {{3}}, e.g. 50000 -> "R500"."""
    rands = total_due_cents // 100
    return f"R{rands:,}"


def build_payer_name(first_name: str, last_name: str) -> str:
    """Stitch rejects a payerName shorter than 3 characters. Most PCO
    registrants pass with first_name alone; a short first name (e.g. "Jo",
    "Al") falls back to "first_name last_name" instead. If the combined
    name is still under 3 characters (short first name + no surname on
    file), this still returns something too short and the Stitch call
    will fail - left unhandled deliberately since it's rare enough
    (PCO registrations normally capture both) not to warrant inventing
    padding."""
    first_name = (first_name or "").strip()
    if len(first_name) >= 3:
        return first_name
    last_name = (last_name or "").strip()
    return f"{first_name} {last_name}".strip()


def extract_link_suffix(payment_link: str) -> str:
    """Strips the fixed STITCH_BASE_URL prefix off a full payment link
    returned by the API, so it fits the existing WhatsApp button's dynamic
    URL variable (the button's static prefix is already STITCH_BASE_URL -
    see the "Stitch payment link" preset in the template builder). Falls
    back to the full link unchanged if it doesn't start with the expected
    prefix, rather than raising - a send with a full URL "suffix" would
    produce a broken button, but that's still better than a hard failure."""
    if payment_link.startswith(STITCH_BASE_URL):
        return payment_link[len(STITCH_BASE_URL):]
    return payment_link


class StitchClient:
    """One client per unit (see clients.get_stitch_client) - credentials
    are a unit's own Stitch Express client_id/client_secret (SQLAdmin's
    Stitch Credentials page), not shared platform-wide. Mirrors the
    async-httpx style of integrations/whatsapp.py rather than
    web/whatsapp_bulk.py's sync client, since this is only ever called
    from the async registration-poller path, never from a bulk-campaign
    background thread."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.client = httpx.AsyncClient(timeout=30.0)
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    async def _get_access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        response = await self.client.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        response.raise_for_status()
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.monotonic() + _TOKEN_LIFETIME_SECONDS - _TOKEN_REFRESH_MARGIN_SECONDS
        return self._token

    async def create_payment_link(self, amount_cents: int, payer_name: str, merchant_reference: str) -> str:
        """Returns the full payment link URL (STITCH_BASE_URL + Stitch's
        own suffix) - callers that need just the button's dynamic-URL
        variable should pass this through extract_link_suffix()."""
        token = await self._get_access_token()
        rands = amount_cents // 100
        response = await self.client.post(
            _PAYMENT_LINKS_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "amount": rands,
                "payerName": payer_name,
                "merchantReference": merchant_reference,
            },
        )
        response.raise_for_status()
        payload = response.json()
        return payload["paymentLink"]
