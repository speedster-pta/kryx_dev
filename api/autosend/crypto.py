"""
Fernet encryption for credentials at rest (WhatsAppNumber.access_token,
MetaPlatformSettings.app_secret/system_token/webhook_verify_token,
PCOOrganizationSettings.pco_token_secret, units.pco_webhook_secret) -
one key (settings.token_encryption_key), one place credential handling
can be audited.
"""

from cryptography.fernet import Fernet

from autosend.config import settings

_fernet = Fernet(settings.token_encryption_key.encode())


def encrypt_token(plaintext: str | None) -> str | None:
    if not plaintext:
        return plaintext
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return ciphertext
    return _fernet.decrypt(ciphertext.encode()).decode()
