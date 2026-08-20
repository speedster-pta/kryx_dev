"""
Rate limiting for the public /ical/{token}.ics endpoint (web/ical_router.py).

Reuses login_security.py's storage (the generic identifier-keyed
login_attempts table) and its security_logger/log_event/get_client_ip
plumbing, rather than standing up a second lockout table and a second log
file - the mechanism is identical, only the key prefix and the failure
definition differ.

The failure definition here is deliberately NOT "any non-200 response",
unlike login (where wrong-password is the only failure mode). A token
lookup has three outcomes with very different meanings:
  - not found at all / malformed shape: nobody produces this
    legitimately - only guessing or scanning does. Counts as a failure.
  - found, but expired/revoked/event past its own expiry: this is a REAL
    link that used to work - most likely someone reopening an old
    WhatsApp thread, or a corrected/cancelled invite. Expected traffic,
    not an attack signal, so it must NOT count toward the same lockout an
    attacker would trip.
  - found and live: not a failure at all.
Given the token's 256 bits of entropy, the actual threat this defends
against isn't "attacker guesses the real token" (computationally
irrelevant) - it's scanning bots probing link-shaped URLs, and volume-based
abuse of the per-request DB/.ics-generation cost. Both of those only ever
produce the first outcome, which is why only that one is penalised.
"""
from fastapi import Request

from autosend import storage
from autosend.web.login_security import (
    get_client_ip,
    log_event,
    _now,
    _sanitize_for_log,
)
from datetime import datetime, timedelta

MAX_FAILED_ATTEMPTS = 10
ATTEMPT_WINDOW_MINUTES = 15
LOCKOUT_MINUTES = 30


def ical_ip_key(ip: str) -> str:
    # Separate bucket from login's ip_key()/signup_ip_key() - scanning for
    # calendar links shouldn't lock an IP out of /login, and vice versa.
    return f"ical_ip:{ip}"


def check_lockout(ip: str) -> int | None:
    """Returns remaining lockout minutes if this IP is currently locked
    out of the .ics endpoint, else None. Mirrors login_security.
    check_lockout - same storage.get_lockout() call, different key."""
    locked_until_raw = storage.get_lockout(ical_ip_key(ip))
    if not locked_until_raw:
        return None
    locked_until = datetime.fromisoformat(locked_until_raw)
    remaining = locked_until - _now()
    if remaining.total_seconds() <= 0:
        return None
    return max(1, int(remaining.total_seconds() // 60) + 1)


def record_invalid_token(ip: str) -> None:
    """Call only for a genuine not-found/malformed token - never for an
    expired/revoked one (see module docstring)."""
    identifier = ical_ip_key(ip)
    row = storage.get_login_attempt_row(identifier)

    now = _now()
    if row:
        last_attempt = datetime.fromisoformat(row["last_attempt_at"])
        if now - last_attempt > timedelta(minutes=ATTEMPT_WINDOW_MINUTES):
            failed_count = 1
        else:
            failed_count = row["failed_count"] + 1
    else:
        failed_count = 1

    locked_until = None
    if failed_count >= MAX_FAILED_ATTEMPTS:
        locked_until = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()

    storage.record_login_attempt(identifier, failed_count, locked_until)


def log_access(event: str, request: Request, *, token_prefix: str, **extra):
    """token_prefix: first few chars of the token only - enough to
    correlate repeated hits on the same link in the log without ever
    writing a full, still-potentially-live bearer token to disk."""
    ip = get_client_ip(request)
    log_event(event, ip, "-", token=_sanitize_for_log(token_prefix) + "...", **extra)
