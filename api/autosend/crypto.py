"""
Fernet encryption for credentials at rest (WhatsAppNumber.access_token,
MetaPlatformSettings.app_secret/system_token/webhook_verify_token,
PCOOrganizationSettings.pco_token_secret, units.pco_webhook_secret) -
one key (settings.token_encryption_key), one place credential handling
can be audited.
"""

from cryptography.fernet import Fernet, InvalidToken

from autosend.config import settings

_fernet = Fernet(settings.token_encryption_key.encode())


def encrypt_token(plaintext: str | None) -> str | None:
    if not plaintext:
        return plaintext
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return ciphertext
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        # Not Fernet ciphertext - kryx-dev has no legacy-plaintext era, so this
        # is a defensive backstop only, against a credential ever ending up
        # unencrypted in the DB (a direct write, an admin bug). Return it
        # unchanged rather than raising, so a bad row doesn't break sending.
        return ciphertext
