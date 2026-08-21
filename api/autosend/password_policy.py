"""Shared password-strength rule, enforced at every place a password is set
or changed (admin UI, self-serve signup, self-service password change,
CLI account creation) - see validate_password_strength() docstring for the
rule itself.
"""
import re

MIN_PASSWORD_LENGTH = 8
MIN_CHARACTER_CLASSES = 3

_CHARACTER_CLASSES = (
    re.compile(r"[a-z]"),
    re.compile(r"[A-Z]"),
    re.compile(r"[0-9]"),
    re.compile(r"[^a-zA-Z0-9]"),
)


def validate_password_strength(password: str) -> None:
    """Raise ValueError with a user-facing message if password does not meet
    the minimum strength bar: at least MIN_PASSWORD_LENGTH characters, and
    characters from at least MIN_CHARACTER_CLASSES of {lowercase, uppercase,
    digit, symbol}.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")

    classes_present = sum(1 for pattern in _CHARACTER_CLASSES if pattern.search(password))
    if classes_present < MIN_CHARACTER_CLASSES:
        raise ValueError(
            "Password must contain at least "
            f"{MIN_CHARACTER_CLASSES} of the following: lowercase letters, "
            "uppercase letters, numbers, symbols"
        )
