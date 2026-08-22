"""Brute-force protection for the /login endpoint.

Ported from wa-campaign-manager's login_security.py. Same logic, just
reads/writes through autosend.storage's login_attempts table
instead of holding its own sqlite connection, since this app already has
one shared DB file.

Tracks failed login attempts per-username and per-IP. After
MAX_FAILED_ATTEMPTS failures within ATTEMPT_WINDOW_MINUTES, that identifier
is locked out for LOCKOUT_MINUTES. Both identifiers are checked/recorded on
every attempt:
- username lockout stops an attacker hammering one account from many IPs
- IP lockout stops an attacker spraying many usernames from one IP

Login events are also written to a dedicated log file in a stable,
single-line format so a tool like fail2ban can tail it with a simple regex.
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Optional

from fastapi import Request

from autosend import storage

MAX_FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW_MINUTES = 15
LOCKOUT_MINUTES = 15

AUTH_LOG_PATH = os.environ.get("AUTH_LOG_PATH", "/var/log/kryx/auth.log")


class _UTCFormatter(logging.Formatter):
    """Formats timestamps in UTC with an explicit +00:00 offset regardless
    of the container's local TZ, so a host-side log tailer (e.g. fail2ban)
    doesn't silently misinterpret them as local time."""

    converter = time.gmtime


security_logger = logging.getLogger("autosend.security")
security_logger.setLevel(logging.INFO)
security_logger.propagate = False

if not security_logger.handlers:
    try:
        os.makedirs(os.path.dirname(AUTH_LOG_PATH), exist_ok=True)
        _handler = RotatingFileHandler(AUTH_LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=5)
    except OSError:
        logging.getLogger(__name__).warning(
            "Could not open %s for writing, falling back to stderr for security logs", AUTH_LOG_PATH
        )
        _handler = logging.StreamHandler()

    _handler.setFormatter(_UTCFormatter("%(asctime)s+00:00 %(message)s"))
    security_logger.addHandler(_handler)


def _sanitize_for_log(value) -> str:
    """Strips newlines/control chars so a malicious username can't inject
    fake extra log lines to forge a bogus entry for an innocent IP."""
    text = str(value)
    return "".join(ch if ch.isprintable() and ch not in "\r\n" else "\\x%02x" % ord(ch) for ch in text)[:200]


def log_event(event: str, ip: str, username: str, **extra):
    safe_ip = _sanitize_for_log(ip)
    safe_username = _sanitize_for_log(username)
    fields = " ".join(f"{k}={_sanitize_for_log(v)}" for k, v in extra.items())
    security_logger.info("%s ip=%s user=%s %s", event, safe_ip, safe_username, fields)


def _now():
    return datetime.now(timezone.utc)


def get_client_ip(request: Request) -> str:
    # Trust X-Forwarded-For since this sits behind nginx. Only used to
    # bucket the IP-based lockout, not for access control, so a spoofed
    # header just makes that bucket less effective - it doesn't bypass the
    # username-based lockout.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def username_key(username: str) -> str:
    return f"user:{username.strip().lower()}"


def ip_key(ip: str) -> str:
    return f"ip:{ip}"


def signup_ip_key(ip: str) -> str:
    # Separate bucket from ip_key() - a mistyped login password shouldn't
    # lock an IP out of /signup, and vice versa.
    return f"signup_ip:{ip}"


def check_lockout(identifier: str) -> Optional[int]:
    """Returns remaining lockout minutes if identifier is currently locked, else None."""
    locked_until_raw = storage.get_lockout(identifier)
    if not locked_until_raw:
        return None

    locked_until = datetime.fromisoformat(locked_until_raw)
    remaining = locked_until - _now()
    if remaining.total_seconds() <= 0:
        return None
    return max(1, int(remaining.total_seconds() // 60) + 1)


def record_failed_attempt(identifier: str):
    row = storage.get_login_attempt_row(identifier)

    now = _now()
    if row:
        last_attempt = datetime.fromisoformat(row["last_attempt_at"])
        if now - last_attempt > timedelta(minutes=ATTEMPT_WINDOW_MINUTES):
            failed_count = 1  # previous streak expired, start over
        else:
            failed_count = row["failed_count"] + 1
    else:
        failed_count = 1

    locked_until = None
    if failed_count >= MAX_FAILED_ATTEMPTS:
        locked_until = (now + timedelta(minutes=LOCKOUT_MINUTES)).isoformat()

    storage.record_login_attempt(identifier, failed_count, locked_until)


def clear_attempts(identifier: str):
    storage.clear_login_attempts(identifier)


def lockout_message(request: Request) -> Optional[str]:
    """Friendly warning for the login page when the caller's IP is
    currently rate-limited, so a legitimate user sees why login keeps
    failing instead of just a generic "Invalid credentials." on every
    attempt. Safe to reveal: the IP bucket is shared across every username
    tried from it (record_failed_attempt() fires on any failure regardless
    of whether the username exists), so this doesn't tell an attacker
    anything about which usernames are valid - only that this connection
    tripped the same rate limit a real user would."""
    ip = get_client_ip(request)
    remaining = check_lockout(ip_key(ip))
    if remaining is None:
        return None
    unit = "minute" if remaining == 1 else "minutes"
    return f"Too many failed login attempts from this connection. Try again in {remaining} {unit}."
