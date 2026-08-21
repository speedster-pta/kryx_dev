"""
billing/paystack.py

Paystack REST adapter implementing billing.provider.PaymentProvider.
Built against Paystack's real, documented API shapes even though no live
PAYSTACK_SECRET_KEY exists yet in dev (config.py leaves it blank by
default) - every call here will get a 401 from Paystack until a real key
is set via that environment's .env. That's expected in dev right now, not
a bug to work around.

httpx.AsyncClient usage mirrors integrations/whatsapp.py's WhatsAppClient
(async client held on the instance, Authorization header set once at
construction, one shared base_url) - same style, different API.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from autosend.billing.provider import PaymentProvider, TransactionResult
from autosend.config import settings

BASE_URL = "https://api.paystack.co"

logger = logging.getLogger(__name__)


class PaystackError(Exception):
    """Raised when Paystack rejects a request - carries the provider's own
    message rather than letting a bare httpx.HTTPStatusError surface, same
    reasoning as integrations/whatsapp.py's WhatsAppSendError."""

    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


class PaystackProvider(PaymentProvider):
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {settings.paystack_secret_key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        response = await self.client.request(method, path, **kwargs)
        if response.status_code >= 400:
            # No PAYSTACK_SECRET_KEY configured in dev yet -> Paystack
            # returns 401 for every call. That's an expected, known state
            # right now, not something to special-case here; it surfaces
            # as a PaystackError like any other rejection.
            logger.error("Paystack API error %s: %s", response.status_code, response.text)
            try:
                body = response.json()
                message = body.get("message", "Paystack API error")
            except Exception:
                message = "Paystack API error"
            raise PaystackError(message, status_code=response.status_code)
        return response.json()

    async def create_customer(self, email: str, org_id: int) -> str:
        body = await self._request(
            "POST",
            "/customer",
            json={"email": email, "metadata": {"org_id": org_id}},
        )
        return body["data"]["customer_code"]

    async def initialize_transaction(
        self, email: str, amount_cents: int, callback_url: str
    ) -> str:
        body = await self._request(
            "POST",
            "/transaction/initialize",
            json={
                "email": email,
                "amount": amount_cents,
                "callback_url": callback_url,
            },
        )
        return body["data"]["authorization_url"]

    async def charge_authorization(
        self, authorization_code: str, amount_cents: int, email: str
    ) -> TransactionResult:
        # Confirmed against the live Paystack test API (not a
        # hypothetical): a placeholder email here gets the charge
        # rejected outright with "Email does not match Authorization
        # code" - Paystack validates this against the email the
        # authorization_code was originally created under. The caller
        # (billing.engine.run_recurring_billing) must pass the same
        # email stored on the subscription at checkout time
        # (subscriptions.billing_email), not a made-up one.
        body = await self._request(
            "POST",
            "/transaction/charge_authorization",
            json={
                "authorization_code": authorization_code,
                "amount": amount_cents,
                "email": email,
            },
        )
        data = body.get("data", {})
        status = data.get("status")
        return TransactionResult(
            success=status == "success",
            reference=data.get("reference", ""),
            authorization_code=(data.get("authorization") or {}).get("authorization_code"),
            amount_cents=data.get("amount", amount_cents),
            raw=body,
        )

    async def verify_transaction(self, reference: str) -> TransactionResult:
        body = await self._request("GET", f"/transaction/verify/{reference}")
        data = body.get("data", {})
        status = data.get("status")
        return TransactionResult(
            success=status == "success",
            reference=data.get("reference", reference),
            authorization_code=(data.get("authorization") or {}).get("authorization_code"),
            amount_cents=data.get("amount", 0),
            raw=body,
        )

    async def cancel(self, subscription_id: int) -> None:
        # No Paystack-side recurring "subscription" object is created by
        # this adapter (billing runs its own charge_authorization loop from
        # engine.run_recurring_billing rather than Paystack's native
        # Subscriptions API), so there is nothing to cancel provider-side -
        # cancellation is purely a local status flip (subscriptions.status
        # = 'cancelled'), handled by the caller in engine.py. Kept as a
        # real method (rather than omitted) to satisfy the PaymentProvider
        # protocol and give a future provider swap a real hook.
        return None

    async def refund(self, provider_reference: str) -> None:
        await self._request("POST", "/refund", json={"transaction": provider_reference})

    async def aclose(self) -> None:
        await self.client.aclose()


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """Paystack signs each webhook delivery with HMAC-SHA512 over the raw
    request body, keyed by the integration's own secret key
    (PAYSTACK_SECRET_KEY) - there is no separate webhook-only secret in
    Paystack's model (unlike Meta/PCO, its dashboard has no field for
    one; confirmed against the live "API Configuration" screen, which
    only exposes the secret key, public key, callback URL and webhook
    URL). See integrations/webhooks.py::_verify_pco_signature for the
    equivalent HMAC-SHA256 pattern this mirrors. Reject anything that
    doesn't match rather than trusting whatever is POSTed here, same
    reasoning as that function: this ultimately flips a subscription to
    'active'."""
    if not signature:
        return False
    expected = hmac.new(
        settings.paystack_secret_key.encode(), body, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected, signature)
