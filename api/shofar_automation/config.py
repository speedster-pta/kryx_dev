from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Shofar Automation"
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    # Simulation / Development Environment Overrides
    dry_run: bool = False
    enable_poller: bool = False
    database_path: str = "/data/shofar_automation.db"

    registration_poll_interval_minutes: int = 5

    admin_api_key: str = "dev_admin_api_key_12345"

    # Signs both the SQLAdmin session and the /login session that now fronts
    # the whole app (bulk campaigns + SQLAdmin). Kept separate from
    # admin_api_key (which is the X-Admin-Key header for the /ops/* JSON
    # endpoints in main.py) since they protect different things.
    session_secret_key: str = "dev_session_secret_key_12345"

    # Encrypts WhatsAppNumber.access_token at rest (Fernet, so it's
    # recoverable - unlike password_hash, this has to be usable to call the
    # Graph API). Also used for MetaPlatformSettings.app_secret/system_token/
    # webhook_verify_token and PCOOrganizationSettings.pco_token_secret -
    # one key for every Fernet-encrypted credential column across the app. Generate with:
    #   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    token_encryption_key: str = "Wk1Yc0RROVR5VjFmUTZkU1lHdHhzVjA0THNudVJxd3E="

    # Default delay between sends in a bulk campaign; kept low enough to
    # avoid WhatsApp rate limits without needing per-unit tuning.
    # Overridable per-campaign in the UI.
    bulk_campaign_default_delay_seconds: float = 1.0

    model_config = SettingsConfigDict(env_file=(".env", ".env.local"), extra="ignore")


settings = Settings()

