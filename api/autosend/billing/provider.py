"""
billing/provider.py

Abstract payment-provider interface - one adapter exists today
(billing/paystack.py::PaystackProvider), but the billing engine
(billing/engine.py) only ever talks to this Protocol, never to Paystack
specifics directly, so a second provider could be swapped in later
without touching engine.py.

This codebase doesn't otherwise have a formal Protocol-based client
interface (integrations/whatsapp.py's WhatsAppClient and
integrations/planning_center.py's client are both concrete classes with
no shared abstract base, since each only ever has one real
implementation) - a Protocol is used here specifically because "swap the
payment provider later" is a named, plausible future need in a way
neither of those is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class TransactionResult:
    success: bool
    reference: str
    authorization_code: str | None
    amount_cents: int
    raw: dict


class PaymentProvider(Protocol):
    async def create_customer(self, email: str, org_id: int) -> str:
        """Returns the provider's customer code."""
        ...

    async def initialize_transaction(
        self, email: str, amount_cents: int, callback_url: str
    ) -> str:
        """Returns the checkout URL to redirect the payer's browser to.
        Takes the payer's email, not a customer code - Paystack's
        /transaction/initialize matches/creates the customer by email
        itself; it has no parameter for an existing customer_code."""
        ...

    async def charge_authorization(
        self, authorization_code: str, amount_cents: int, email: str
    ) -> TransactionResult:
        """Charges a previously-authorized card for recurring billing.
        email must match the address the authorization_code was
        originally created under - Paystack rejects the charge
        otherwise (confirmed against the live API, not assumed)."""
        ...

    async def verify_transaction(self, reference: str) -> TransactionResult:
        """Confirms the outcome of a transaction by its reference."""
        ...

    async def cancel(self, subscription_id: int) -> None:
        ...

    async def refund(self, provider_reference: str) -> None:
        ...
