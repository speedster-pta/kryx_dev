"""Shared helpers for resolving a stored automation-template variable slot
into the literal text to send. A slot's stored value is either a known
field key (e.g. "first_name", looked up in that send's available_fields
dict) or a user-entered literal string prefixed with CUSTOM_PREFIX (e.g.
"custom:Welcome to our team!"), added via the "Custom Text" option in each
variable dropdown on the Automations page.

Used by all three automation send paths - registration_poller.py,
form_response.py, serving_reminder.py - for both body and button
variables, so "Custom Text" behaves identically everywhere the
Automations UI offers it.
"""

CUSTOM_PREFIX = "custom:"


def is_custom_variable(key: str) -> bool:
    return key.startswith(CUSTOM_PREFIX)


def resolve_variable_strict(key: str, available_fields: dict) -> str:
    """For body variables. A custom:<text> key returns the literal text
    as-is. Any other key is looked up in available_fields via plain dict
    indexing - raises KeyError(key) if missing, same as available_fields[key]
    would, so callers' existing `except KeyError as missing` handling
    (form_response.py, serving_reminder.py, and now registration_poller.py)
    is unchanged."""
    if is_custom_variable(key):
        return key[len(CUSTOM_PREFIX):]
    return available_fields[key]


def resolve_variable_lenient(key: str | None, available_fields: dict) -> str | None:
    """For button variables. An empty/falsy key means "no variable for
    this button" (None). A custom:<text> key returns the literal text.
    Any other key is looked up in available_fields, returning None
    (not raising) if missing - matches the existing skip-that-one-button
    behavior used across all three send paths (the caller is expected to
    log the miss itself, same as registration_poller.py's
    _resolve_button_values already does)."""
    if not key:
        return None
    if is_custom_variable(key):
        return key[len(CUSTOM_PREFIX):]
    return available_fields.get(key)
