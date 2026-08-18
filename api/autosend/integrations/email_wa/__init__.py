"""
integrations/email_wa

Genuinely generic Email-to-WhatsApp automator: parses automated
confirmation emails from arbitrary booking/event/CRM platforms and sends
the extracted values through a WhatsApp template, reusing the same async
client (integrations/whatsapp.py) and whatsapp_templates/
body_variable_order machinery every other automation in this codebase
uses.

Gated on organisation_modules.email_wa, same two-tier grant/enable shape
as MODULE_PCO (see storage/modules.py).

Built from scratch, with an intentionally empty provider registry (see
providers/__init__.py) - this is NOT the original "email_wa" module,
which was really only ever SME Metrics wearing a generic name. That
integration has since been split out into its own permanently
pre-configured module (integrations/sme_metrics/, storage.MODULE_SME_METRICS),
freeing this module key for the real thing: a provider-agnostic
framework any future CRM-specific parser (a "special case" of this
integration, per the original design discussion) can register into,
without needing its own dedicated module/schema/webhook the way
SME Metrics currently still does.

This package's own schema/webhook/provider code should never need to
import PCO or integrations/sme_metrics internals - same split reasoning
as every other integration package in this codebase.
"""
