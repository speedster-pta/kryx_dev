"""
integrations/sme_metrics

Email-to-WhatsApp automator for smeMetrics (https://www.smemetrics.com)
booking/appointment notification emails: parses the confirmation emails
and sends the extracted values through a WhatsApp template, reusing the
same async client (integrations/whatsapp.py) and
whatsapp_templates/body_variable_order machinery every other automation
in this codebase uses.

Gated on organisation_modules.sme_metrics, same two-tier grant/enable
shape as MODULE_PCO (see storage/modules.py).

Formerly the *only* provider inside a generic "email_wa" module (back
when "Email-to-WhatsApp" and "SME Metrics" meant the same thing) - split
out into its own permanently pre-configured integration so it can be
sold/enabled per-org independently of the genuinely generic,
provider-agnostic Email-to-WhatsApp module now being built from scratch
under integrations/email_wa/ (which reuses the "email_wa" module key
this one used to hold - see storage/modules.py's
migrate_legacy_email_wa_module_key). This package's own schema/webhook/
provider code should never need to import PCO or the generic email_wa
integration's internals.

The inbound webhook route (webhook.py), its path-secret config field
(settings.email_wa_webhook_secret) and the informational inbound-domain
setting (settings.email_wa_inbound_domain) kept their "email_wa" names on
purpose even after this split - they're wire-format (an already-deployed
SendGrid Inbound Parse route points at that exact path/secret), not
branding, and renaming them would silently break real inbound mail until
someone reconfigured SendGrid to match. Same reasoning as the
whatsapp_templates.template_type prefix ("email_wa:<provider>:
<email_type>") in storage/sme_metrics.py: an internal DB key scheme, not
worth a data migration just to match the new product name.
"""
