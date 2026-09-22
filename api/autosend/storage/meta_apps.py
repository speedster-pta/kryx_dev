"""
Extra Meta Apps whose webhook signatures /webhooks/whatsapp should also
accept, alongside the meta_platform_settings singleton (see schema.py's
meta_apps table docstring for why a WABA can end up delivering live events
via a second app). Platform-wide, not tenant-scoped, same as
meta_platform_settings itself - rows are managed via
admin_pages.MetaSettingsView's SQLAlchemy-backed card list; this module
only covers the read path application code needs.
"""

from ._db import _connect


def get_meta_app_secrets_decrypted() -> list[str]:
    """Decrypted app_secret for every extra Meta app on file, in no
    particular order. Used exclusively by integrations/webhooks.py's
    signature verification, never returned from any HTTP response."""
    from autosend import crypto

    with _connect() as conn:
        rows = conn.execute(
            "SELECT app_secret FROM meta_apps WHERE app_secret IS NOT NULL"
        ).fetchall()
        return [crypto.decrypt_token(r[0]) for r in rows if r[0]]
