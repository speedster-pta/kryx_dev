"""
integrations/email_wa/providers/

Provider registry - a provider is code (a parser), not admin-authored
config, mirroring storage.modules.AVAILABLE_MODULES: adding a new
provider is a deploy (new module + one registry entry below), not a
schema change or a per-org regex-configuration screen. This is the
design choice that replaced an earlier draft where org-admins wrote
their own extraction regex per unit - moving parsing into developer-owned
code removes that ReDoS/trust-boundary problem entirely, since the input
(a specific provider's email structure) is now known and bounded per
parser, not attacker/org-adjacent.

Each provider module exposes:
    PROVIDER_KEY: str
    LABEL: str
    EMAIL_TYPES: dict[str, EmailTypeSpec]
    identify_email_type(text: str) -> str | None
    parse(email_type: str, text: str) -> dict[str, str]   # raises UnparseableEmail
"""

from __future__ import annotations

from dataclasses import dataclass


class UnparseableEmail(Exception):
    """Raised by a provider's parse() when this email doesn't match the
    structure expected for the given email_type - e.g. the provider
    changed their template, or email_type was misidentified upstream.
    Callers (services/email_wa.py) treat this as a parse failure: logged
    to send_log, no send attempted, no retry (retrying can't change a
    static email's content)."""


@dataclass(frozen=True)
class EmailTypeSpec:
    """label: display name for this trigger in the admin UI's sub-tabs
    (Automations page's Email-to-WhatsApp section) - deliberately a
    separate, provider-authored string rather than a humanized version of
    the dict key, since the two can legitimately differ (e.g.
    "booking_request" displaying as "Requests", to read naturally
    alongside a provider's other lifecycle stages).

    fields: canonical field keys this email_type can expose, in the
    order they should be offered when configuring a WhatsApp template's
    body_variable_order/button_variables for this integration - the same
    "available_fields" vocabulary mechanism registration_poller.py/
    form_response.py already use for PCO automations.

    phone_field: which of those keys is the WhatsApp recipient. Fixed by
    the provider's own parser, never admin-selected - so misconfiguring a
    template's variable mapping can never accidentally point a send at
    the wrong field, since the destination number isn't part of that
    mapping at all."""

    label: str
    fields: list[str]
    phone_field: str


from . import sme_metrics  # noqa: E402 - after the shared types above, which this imports

PROVIDERS = {
    sme_metrics.PROVIDER_KEY: sme_metrics,
}


def build_email_type_tabs(provider) -> list[dict]:
    """Ordered list of {key, label, fields, enabled} describing the
    sub-tabs the Automations page's Email-to-WhatsApp section (and
    /api/email-wa/providers) should render for one provider - single
    source of truth shared by admin_pages.py's server-rendered tabs and
    email_wa_router.py's API, so the two can't drift apart.

    Combines the provider's real, registered EMAIL_TYPES with its
    optional PLANNED_EMAIL_TYPES (lifecycle stages known to exist but not
    yet parseable - see sme_metrics.py's "booking_confirmed" gap) into
    one list, ordered by the provider's optional EMAIL_TYPE_TAB_ORDER
    (falling back to EMAIL_TYPES' own dict order if the provider hasn't
    defined one - true today only for a provider with nothing planned).
    A planned entry is `enabled: False` with no fields - the admin UI
    renders it as a disabled placeholder tab rather than a working
    integration, since there is no parser yet to back one."""
    planned = dict(getattr(provider, "PLANNED_EMAIL_TYPES", []))
    order = getattr(provider, "EMAIL_TYPE_TAB_ORDER", list(provider.EMAIL_TYPES.keys()))
    tabs = []
    for key in order:
        spec = provider.EMAIL_TYPES.get(key)
        if spec is not None:
            tabs.append({"key": key, "label": spec.label, "fields": spec.fields, "enabled": True})
        elif key in planned:
            tabs.append({"key": key, "label": planned[key], "fields": [], "enabled": False})
    return tabs
