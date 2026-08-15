"""
Free-text phone number normalisation, shared by any integration that
receives a phone number as human-typed text rather than a platform API
already returning E.164 (PCO's People API does the latter - see
integrations/planning_center.py - so it has never needed this). First
consumer is integrations/email_wa/, expected to be reused by future
integrations with the same problem.
"""

import phonenumbers


def normalize_phone_e164(raw: str, default_region: str) -> str | None:
    """default_region is an ISO 3166-1 alpha-2 code (e.g. "ZA"), used only
    to interpret a number with no country code - a number that already
    has one (e.g. "+44...") is unaffected by it. Returns None (never
    raises) for anything that doesn't parse as a plausible number, so
    callers can treat a bad phone field the same as a missing one."""
    try:
        parsed = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
