from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Kryx Automation"
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    # Simulation / Development Environment Overrides
    dry_run: bool = False
    enable_poller: bool = False
    database_path: str = "/data/autosend.db"

    registration_poll_interval_minutes: int = 5

    admin_api_key: str

    # Signs both the SQLAdmin session and the /login session that now fronts
    # the whole app (bulk campaigns + SQLAdmin). Kept separate from
    # admin_api_key (which is the X-Admin-Key header for the /ops/* JSON
    # endpoints in main.py) since they protect different things.
    session_secret_key: str

    # Encrypts WhatsAppNumber.access_token at rest (Fernet, so it's
    # recoverable - unlike password_hash, this has to be usable to call the
    # Graph API). Also used for MetaPlatformSettings.app_secret/system_token/
    # webhook_verify_token and PCOOrganizationSettings.pco_token_secret -
    # one key for every Fernet-encrypted credential column across the app. Generate with:
    #   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_key: str

    # Default delay between sends in a bulk campaign; kept low enough to
    # avoid WhatsApp rate limits without needing per-unit tuning.
    # Overridable per-campaign in the UI.
    bulk_campaign_default_delay_seconds: float = 1.0

    # Error monitoring (Sentry). Blank disables it entirely - safe default
    # for local dev. Get a DSN from sentry.io (or a self-hosted GlitchTip
    # instance) and set it per-deployment via .env.
    sentry_dsn: str = ""
    environment: str = "production"

    # Path-secret gate for the SME Metrics SendGrid Inbound Parse webhook
    # (/webhooks/email/inbound/{email_wa_webhook_secret}). Inbound Parse
    # POSTs to one fixed URL for an entire MX hostname (see
    # integrations/sme_metrics/webhook.py) and, unlike PCO/Meta's
    # webhooks, has no signing scheme at all - there's no HMAC to verify a
    # payload against. This is the same "not a real authz boundary, just a
    # bar against opportunistic discovery" caveat as auth.py's
    # admin_api_key, not a substitute for it.
    #
    # Field name kept its pre-split "email_wa" spelling on purpose even
    # though it now belongs to the SME Metrics integration specifically -
    # see integrations/sme_metrics/__init__.py. The unrelated, genuinely
    # generic Email-to-WhatsApp integration has its own, separately-named
    # webhook secret below (generic_email_wa_webhook_secret) - don't
    # confuse the two.
    email_wa_webhook_secret: str

    # The MX hostname SendGrid Inbound Parse is configured against for
    # SME Metrics - purely informational, used only to display each
    # email_integrations row's full receiving address
    # ("{local_part}@{email_wa_inbound_domain}") in the admin UI so users
    # can copy it into their booking platform's notification settings.
    # Not read anywhere in the actual receive path (SendGrid already routed
    # the request here by the time integrations/sme_metrics/webhook.py
    # sees it) - changing this doesn't change what mail is actually
    # accepted.
    email_wa_inbound_domain: str = "mail.kryx.app"

    # Same two settings as above, but for the separate, genuinely generic
    # Email-to-WhatsApp integration (integrations/email_wa/) - a distinct
    # SendGrid Inbound Parse route/MX hostname from SME Metrics', since
    # Inbound Parse is configured per hostname and these are two
    # independent per-org modules with their own receiving addresses.
    generic_email_wa_webhook_secret: str
    generic_email_wa_inbound_domain: str = "mail-generic.kryx.app"

    # Platform subscription billing (Paystack) - a single, platform-wide
    # secret, not per-organisation (unlike e.g.
    # PCOOrganizationSettings.pco_token_secret, which genuinely is
    # per-org) - Kryx itself is the Paystack merchant, billing every
    # organisation through one account. Paystack has no separate
    # webhook-only secret in its dashboard/API (unlike Meta/PCO) - it
    # signs webhooks with this same secret key, see
    # billing/paystack.py::verify_webhook_signature. Blank by default:
    # calls through billing/paystack.py will 401 until a real key is set
    # via that environment's own .env (never committed here).
    paystack_secret_key: str = ""

    model_config = SettingsConfigDict(env_file=(".env", ".env.local"), extra="ignore")


settings = Settings()

