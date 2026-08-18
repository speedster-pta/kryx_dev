"""
integrations/email_wa/providers/

Provider registry - a provider is code (a parser), not admin-authored
config, mirroring storage.modules.AVAILABLE_MODULES: adding a new
provider is a deploy (new module + one registry entry below), not a
schema change or a per-org regex-configuration screen. Same design as
integrations/sme_metrics/providers/ (see that package's own docstring for
the full rationale) - this registry starts genuinely empty, since this
module was built as the real, generic Email-to-WhatsApp framework rather
than one CRM's parser wearing a generic name (that was SME Metrics' old
role - see integrations/email_wa/__init__.py).

Each provider module exposes:
    PROVIDER_KEY: str
    LABEL: str
    EMAIL_TYPES: dict[str, EmailTypeSpec]
    identify_email_type(text: str) -> str | None
    parse(email_type: str, text: str) -> dict[str, str]   # raises UnparseableEmail

Add the first real provider here the same way
integrations/sme_metrics/providers/sme_metrics.py was added: a new module
in this package, plus one entry in PROVIDERS below.
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
    the dict key, since the two can legitimately differ.

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


# Empty on purpose - see this file's module docstring. automations.html
# and admin_pages.py already handle an empty provider list gracefully
# (an "Email-to-WhatsApp" Automations page with no configurable sections
# yet), so there's nothing else to wire up before the first real provider
# is registered here.
PROVIDERS: dict[str, object] = {}


def build_email_type_tabs(provider) -> list[dict]:
    """Ordered list of {key, label, fields, enabled} describing the
    sub-tabs the Automations page's Email-to-WhatsApp section (and
    /api/email-wa/providers) should render for one provider - single
    source of truth shared by admin_pages.py's server-rendered tabs and
    web/email_wa_router.py's API, so the two can't drift apart. Identical
    to integrations/sme_metrics/providers/build_email_type_tabs - kept as
    a duplicate function (not a shared helper) for the same reason the
    two packages have separate schemas: this module should never need to
    import integrations/sme_metrics internals, or vice versa."""
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
