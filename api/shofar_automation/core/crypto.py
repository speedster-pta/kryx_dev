"""
core/crypto.py

Fernet encryption for all credentials at rest, per the inherited
engineering principle. Integrations (e.g. integrations/pco/storage.py
for pco_token_secret, pco_webhook_secret) import encrypt/decrypt from
here rather than rolling their own — one key, one place credential
handling can be audited.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet


def _get_fernet() -> Fernet:
    key = os.environ.get("KRYX_FERNET_KEY")
    if not key:
        raise RuntimeError(
            "KRYX_FERNET_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and set it in the "
            "environment before starting the app."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    if plaintext is None:
        return None
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    if ciphertext is None:
        return None
    return _get_fernet().decrypt(ciphertext.encode()).decode()
