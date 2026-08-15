"""
integrations/email_wa

Email-to-WhatsApp automator: parses automated confirmation emails from
booking/event platforms and sends the extracted values through a
WhatsApp template, reusing the same async client
(integrations/whatsapp.py) and whatsapp_templates/body_variable_order
machinery every other automation in this codebase uses.

Gated on organisation_modules.email_wa, same two-tier grant/enable shape
as MODULE_PCO (see storage/modules.py).

Split from integrations/pco/ per the same reasoning that split PCO out
originally: this module's schema/webhook/provider code should never need
to import PCO internals, and an org without this module enabled should
pay no cost for tables/routes it doesn't use.
"""
