"""24-hour rolling WhatsApp messaging-limit tracking and throttle gating.

Meta pools messaging limits at the WhatsApp Business Portfolio level (shared
across every phone number under the same WABA) since October 2025, not per
phone number - see
https://developers.facebook.com/documentation/business-messaging/whatsapp/messaging-limits.
So all counting/throttling here keys on waba_id, not phone_number_id. A
WhatsAppNumber without a waba_id on file falls back to being its own
isolated pool (keyed by phone_number_id) since there's no portfolio ID to
group it under - see _limit_key().

Two independent pieces of state, both keyed the same way:
  - messaging_limit_tier: the *ceiling* Meta currently allows. Synced from
    Meta's Graph API at most once per UTC calendar day (see _ensure_fresh_tier)
    - there's no "remaining capacity" field, this is only ever the cap, not
    usage.
  - message_log rows: the *actual usage*. This is only ever tracked locally
    (Meta doesn't expose a "how much have I sent" endpoint) by logging every
    outbound template send, then counting distinct recipients sent to in the
    trailing 24h window at check time.

Only business-initiated template messages count against the limit - replies
inside an open customer service window don't - so only template sends
should ever call record_send(). Both the bulk-campaign path
(web/whatsapp_bulk.py) and the transactional path
(integrations/whatsapp.py's WhatsAppClient) draw from the same real-world
Meta-side pool, so both need to call gate_send()/record_send() around every
template send.
"""
import logging
from datetime import datetime, timedelta, timezone

import requests

from autosend import storage
from autosend.integrations.whatsapp import BASE_URL as _ASYNC_BASE_URL

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"
API_VERSION = _ASYNC_BASE_URL.rsplit("/", 1)[-1]  # stay in sync with the async client's pinned version

# Meta's tiers. TIER_UNLIMITED has no numeric cap. Both the older
# TIER_50/TIER_1K/TIER_10K/TIER_100K naming and the newer
# TIER_2K/TIER_2000/TIER_10000/TIER_100000 naming are kept as live entries
# (not one deprecated in favour of the other) since a lookup miss here
# silently falls back to DEFAULT_TIER's 250 cap via cap_for_tier() below -
# a real WABA has been observed reporting TIER_2K instead of the
# documented TIER_2000 for the same cap, so both names are mapped rather
# than betting on only one being right.
TIER_LIMITS = {
    "TIER_50": 50,
    "TIER_250": 250,
    "TIER_1K": 1000,
    "TIER_2K": 2000,
    "TIER_2000": 2000,
    "TIER_10K": 10000,
    "TIER_10000": 10000,
    "TIER_100K": 100000,
    "TIER_100000": 100000,
    "TIER_UNLIMITED": None,
}
DEFAULT_TIER = "TIER_250"  # Meta's baseline for a newly created portfolio
WINDOW_HOURS = 24

# Bulk campaigns check against this fraction of the tier cap, not the full
# cap, reserving the remainder for transactional sends (registration
# confirmations, payment reminders) which always check against the full
# cap. See gate_send()'s reserve_fraction param.
CAMPAIGN_RESERVE_FRACTION = 0.05

# Meta doesn't consistently document one single error code for "you've hit
# your 24h messaging limit" across API versions/BSPs, so this matches on
# the error message text instead of a hardcoded code - more robust than
# betting on one code number being right. Deliberately narrow (only the
# limit/restriction phrasing) so unrelated failures - bad phone number,
# template mismatch, media fetch errors, etc - never trip this.
_LIMIT_REJECTION_PHRASES = (
    "restrictions on how many messages",
    "messaging limit",
    "reached its messaging limit",
)

# Meta error codes that mean "you're sending too fast right now", not "this
# recipient/message is invalid" or "you're over your 24h messaging limit".
# Deliberately narrow (only Meta's documented throughput-throttle codes) so
# a genuine per-recipient failure (bad number, disallowed category, etc.)
# never gets retried, and so a real 24h-limit rejection (handled separately
# above via _is_limit_rejection/record_rejection) never gets treated as
# instantly-retryable.
TRANSIENT_RETRY_CODES = {130429, 131056}
TRANSIENT_MAX_ATTEMPTS = 3  # 1 initial send + 2 retries
TRANSIENT_BACKOFF_SECONDS = (5, 10)  # sleep before retry 1, retry 2

# Meta's (code, subcode) for "this phone_number_id doesn't exist / isn't
# accessible to this access token" - e.g. the number was deleted, unlinked,
# or had its permission grant revoked in Meta Business Manager. Unlike the
# messaging-limit rejection above, Meta documents this as a fixed pair
# rather than free-text, so match on that instead of message wording.
_NUMBER_DISCONNECTED_CODE = 100
_NUMBER_DISCONNECTED_SUBCODE = 33


def is_transient_rejection(error_body: dict) -> bool:
    code = (error_body or {}).get("error", {}).get("code")
    return code in TRANSIENT_RETRY_CODES


def is_number_disconnected_error(error_body: dict) -> bool:
    """True if `error_body` (a parsed Graph API error response) is Meta
    telling us a phone_number_id no longer exists or isn't accessible to
    our access token - used by both the background tier/quality/display
    sync below (via _fetch_phone_number_fields) and
    integrations/whatsapp.py's live-send error handling, so either path
    flags a dead number as soon as it's seen rather than waiting on the
    other."""
    error = (error_body or {}).get("error", {})
    return (
        error.get("code") == _NUMBER_DISCONNECTED_CODE
        and error.get("error_subcode") == _NUMBER_DISCONNECTED_SUBCODE
    )


def reserve_fraction_for(number: dict) -> float:
    """The fraction of `number`'s tier cap that bulk campaigns should treat
    as off-limits, reserved for transactional sends. Reads
    campaign_reserve_percent off the WhatsAppNumber row (set in SQLAdmin
    under WhatsApp Numbers); None means "no override, use
    CAMPAIGN_RESERVE_FRACTION". Clamped to [0, 1] as a second line of
    defense alongside the admin form's own NumberRange(0, 100) validator -
    a row written directly to the DB (or from before that validator
    existed) shouldn't be able to make gate_send()'s effective cap
    negative or exceed the real cap."""
    percent = number.get("campaign_reserve_percent")
    if percent is None:
        return CAMPAIGN_RESERVE_FRACTION
    return max(0.0, min(1.0, percent / 100))


def _is_limit_rejection(error_body: dict) -> bool:
    message = (error_body or {}).get("error", {}).get("message", "").lower()
    return any(phrase in message for phrase in _LIMIT_REJECTION_PHRASES)


def cap_for_tier(tier: str | None) -> int | None:
    """Resolves a cached messaging_limit_tier value to a numeric cap.
    Handles two different shapes that end up in that column: the TIER_*
    label strings sync_tier_from_meta() gets from the phone_numbers Graph
    API field (e.g. "TIER_2K"), and the raw numeric cap (e.g. 2000, stored
    as a plain digit string) that Meta's business_capability_update webhook
    delivers instead - see record_capability_update() and
    integrations/webhooks.py's handling of that field. The webhook is often
    the *only* way a WABA's cap gets refreshed before its first send ever
    goes out, and it never sends a TIER_* label, only a number."""
    if tier and tier.isdigit():
        return int(tier)
    return TIER_LIMITS.get(tier, TIER_LIMITS[DEFAULT_TIER])


def record_capability_update(waba_id: str, cap: int) -> None:
    """Called by integrations/webhooks.py's business_capability_update
    handler when Meta pushes a live cap change for a WABA's pooled
    messaging limit, so the dashboard/gate reflects it immediately instead
    of waiting on _ensure_fresh_tier()'s once-a-day poll-on-send (or, for a
    WABA that never sends, never at all - a WABA sitting on Meta's real
    higher cap could otherwise keep showing/enforcing the TIER_250 default
    indefinitely because nothing had ever triggered a sync). Stores the raw
    numeric cap directly (not translated to a TIER_* label - Meta's webhook
    payload never includes one) so cap_for_tier() resolves it straight back
    without a label<->number translation table."""
    storage.upsert_waba_limit_tier(waba_id, str(cap), datetime.now(timezone.utc).isoformat())


def _limit_key(number: dict) -> str:
    """The pool a number draws its 24h capacity from. waba_id when known
    (that's the real pool per Meta's Oct-2025 pooling change); falls back to
    the number's own phone_number_id as an isolated pool when waba_id
    hasn't been filled in yet on that WhatsAppNumber record."""
    return number.get("waba_id") or f"number:{number['phone_number_id']}"


def _fetch_phone_number_fields(
    access_token: str, phone_number_id: str, fields: str, log_context: str
) -> dict | None:
    """GETs the given Graph API `fields` for this phone_number_id. Returns
    the raw JSON dict on success (and clears any previously-set
    disconnected flag for this number, in case it's been reconnected since),
    or None on any failure - every caller below treats None the same way
    (keep whatever cached/previously-good value it has), so a sync hiccup
    never blocks sending or blanks out a good value.

    One failure is treated specially: if Meta's response is
    is_number_disconnected_error() (code 100/subcode 33 - the number was
    deleted, unlinked, or deauthorised in Meta Business Manager, not a
    transient API hiccup), this flags the number via
    storage.mark_whatsapp_number_disconnected() so it surfaces on
    GET /ops/failures instead of only ever showing up as repeated,
    easy-to-miss log lines."""
    url = f"{GRAPH_BASE}/{API_VERSION}/{phone_number_id}"
    params = {"fields": fields}
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        storage.clear_whatsapp_number_disconnected(phone_number_id)
        return resp.json()
    except requests.exceptions.HTTPError as e:
        try:
            body = e.response.json()
        except Exception:
            body = {}
        if is_number_disconnected_error(body):
            logger.error(
                "WhatsApp number %s appears disconnected from Meta (Graph API "
                "code 100/33 - object doesn't exist or access token lacks "
                "permission) - flagging it; check Meta Business Manager",
                phone_number_id,
            )
            storage.mark_whatsapp_number_disconnected(
                phone_number_id, datetime.now(timezone.utc).isoformat()
            )
        else:
            logger.exception("Failed to sync %s for %s", log_context, phone_number_id)
        return None
    except Exception:
        logger.exception("Failed to sync %s for %s", log_context, phone_number_id)
        return None


def sync_tier_from_meta(access_token: str, phone_number_id: str) -> str | None:
    """Pulls the current messaging-limit tier for this number's portfolio.
    Requests both the new and deprecated field names in one call, since not
    every WABA has been cut over to the new field yet - prefers the new one
    when both are present. Returns None (caller keeps whatever cached tier
    it has) on any failure - a sync hiccup shouldn't block sending."""
    data = _fetch_phone_number_fields(
        access_token, phone_number_id,
        "whatsapp_business_manager_messaging_limit,messaging_limit_tier",
        "messaging limit tier",
    )
    if data is None:
        return None
    return data.get("whatsapp_business_manager_messaging_limit") or data.get("messaging_limit_tier")


def sync_quality_from_meta(access_token: str, phone_number_id: str) -> str | None:
    """Pulls this number's current quality_rating (GREEN/YELLOW/RED)
    straight from Meta. Unlike sync_tier_from_meta, this is never pooled
    across a WABA - it's a genuine per-phone_number_id field. Returns
    None (caller keeps whatever cached rating it has) on any failure."""
    data = _fetch_phone_number_fields(access_token, phone_number_id, "quality_rating", "quality rating")
    return data.get("quality_rating") if data else None


def sync_display_number_from_meta(access_token: str, phone_number_id: str) -> str | None:
    """Pulls this number's human-readable MSISDN (display_phone_number)
    straight from Meta, for backfilling WhatsAppNumber rows that predate
    capturing it at onboarding time. Unlike sync_tier_from_meta/
    sync_quality_from_meta this isn't called on a "sync on first use each
    day" cadence - a number's MSISDN doesn't change, so callers (POST
    /ops/sync-phone-numbers) just call this once per row missing it.
    Returns None (caller leaves the row untouched) on any failure."""
    data = _fetch_phone_number_fields(access_token, phone_number_id, "display_phone_number", "display phone number")
    return data.get("display_phone_number") if data else None


def _ensure_fresh_tier(number: dict, key: str) -> str:
    """Syncs the tier from Meta at most once per UTC calendar day per pool
    (per your "sync on first use each day" idea) - Meta itself only
    re-evaluates tier eligibility every ~6h, so a daily pull is plenty, and
    this is a *ceiling* check, not the live usage count. Returns the tier to
    use (freshly synced, cached, or the conservative default)."""
    today = datetime.now(timezone.utc).date().isoformat()
    row = storage.get_waba_limit(key)
    if row and (row.get("limit_synced_at") or "")[:10] == today:
        return row["messaging_limit_tier"] or DEFAULT_TIER

    tier = sync_tier_from_meta(number["access_token"], number["phone_number_id"])
    if tier:
        storage.upsert_waba_limit_tier(key, tier, datetime.now(timezone.utc).isoformat())
        return tier

    # Sync failed - keep using whatever was cached before rather than
    # falling back to the conservative default, so a transient Meta API
    # blip doesn't unnecessarily throttle a number that's actually on a
    # higher tier.
    return row["messaging_limit_tier"] if row and row.get("messaging_limit_tier") else DEFAULT_TIER


def available_capacity(number: dict, reserve_fraction: float = 0.0) -> tuple[int | None, str | None]:
    """How many more unique recipients `number`'s 24h pool can currently
    absorb. Returns (remaining, reason):
      - (None, None): unlimited tier (TIER_UNLIMITED) - no cap to size a
        batch against.
      - (0, reason): no headroom right now, either because Meta has
        already rejected a send for this pool (restricted_until still in
        the future) or the rolling-window count has reached the
        (optionally reserve_fraction-scaled) cap. `reason` is a
        human-readable explanation for logs/UI.
      - (N, None): N more unique recipients can be messaged before the
        cap. This is a point-in-time read, not capacity held/reserved for
        the caller - concurrent sends from this same pool (another
        campaign, or a transactional send) can consume it in between a
        check and when it's actually used. Callers that batch sends (see
        web/campaign_runner.py::_run_campaign) should size each batch to
        at most this number, not just check it once and fire everything.

    Same two checks as gate_send() (restricted_until, then rolling-window
    count vs. tier cap) - gate_send() is now a thin wrapper around this.
    """
    key = _limit_key(number)

    row = storage.get_waba_limit(key)
    if row and row.get("restricted_until"):
        restricted_until = datetime.fromisoformat(row["restricted_until"])
        if datetime.now(timezone.utc) < restricted_until:
            return 0, f"WhatsApp rejected a recent send for exceeding the messaging limit; restricted until {row['restricted_until']}"

    tier = _ensure_fresh_tier(number, key)
    cap = cap_for_tier(tier)
    if cap is None:
        return None, None
    effective_cap = int(cap * (1 - reserve_fraction))

    used = storage.count_recent_unique_recipients(key, WINDOW_HOURS)
    remaining = effective_cap - used
    if remaining <= 0:
        return 0, f"24h messaging limit reached ({used}/{effective_cap} unique recipients, tier {tier} capped at {cap})"
    return remaining, None


def gate_send(number: dict, reserve_fraction: float = 0.0) -> tuple[bool, str | None]:
    """Checks whether `number` currently has 24h capacity to send one more
    business-initiated template message. Returns (allowed, reason). Thin
    wrapper around available_capacity() for callers (e.g. the
    transactional send path) that only need a yes/no, not a count."""
    remaining, reason = available_capacity(number, reserve_fraction)
    if remaining == 0:
        return False, reason
    return True, None


def record_rejection(number: dict, error_body: dict) -> bool:
    """Call this whenever a template send comes back as a failure, passing
    the parsed error response body. If it's Meta telling us we've hit the
    24h messaging limit, marks this pool as authoritatively restricted for
    24h (Meta's own guidance: wait at least 24h before resending after this
    rejection, since retrying sooner just produces the same error) so
    gate_send() stops allowing further attempts immediately rather than
    waiting for the local counter to (never) catch up. Returns True if this
    was recognized as a limit rejection."""
    if not _is_limit_rejection(error_body):
        return False
    key = _limit_key(number)
    restricted_until = (datetime.now(timezone.utc) + timedelta(hours=WINDOW_HOURS)).isoformat()
    storage.set_waba_restricted(key, restricted_until)
    logger.warning(
        "WhatsApp rejected a send for %s citing the messaging limit - "
        "marking restricted until %s", key, restricted_until,
    )
    return True


def record_send(number: dict, recipient_phone: str, campaign_id: int | None = None) -> None:
    """Logs a business-initiated template send so it counts against the
    rolling 24h window. Call once per successful template send."""
    storage.log_sent_message(_limit_key(number), recipient_phone, campaign_id)


def usage_summary(number: dict) -> dict:
    """For admin/dashboard display: current tier, cap, usage, and an
    estimated resume time if throttled. If Meta has explicitly restricted
    this pool (see record_rejection), that resume time is authoritative;
    otherwise it's a live estimate (oldest in-window message's timestamp +
    24h), recomputed every call rather than stored - the rolling window
    means the real answer only ever depends on message_log."""
    key = _limit_key(number)
    row = storage.get_waba_limit(key)
    tier = (row["messaging_limit_tier"] if row else None) or DEFAULT_TIER
    cap = cap_for_tier(tier)
    used = storage.count_recent_unique_recipients(key, WINDOW_HOURS)

    restricted_until = row.get("restricted_until") if row else None
    if restricted_until and datetime.fromisoformat(restricted_until) > datetime.now(timezone.utc):
        return {
            "tier": tier, "cap": cap, "used": used,
            "throttled": True, "estimated_resume": restricted_until,
            "synced_at": row["limit_synced_at"] if row else None,
        }

    throttled = cap is not None and used >= cap
    estimated_resume = None
    if throttled:
        oldest = storage.oldest_message_in_window(key, WINDOW_HOURS)
        if oldest:
            estimated_resume = (
                datetime.fromisoformat(oldest) + timedelta(hours=WINDOW_HOURS)
            ).isoformat()
    return {
        "tier": tier,
        "cap": cap,
        "used": used,
        "throttled": throttled,
        "estimated_resume": estimated_resume,
        "synced_at": row["limit_synced_at"] if row else None,
    }


def quality_summary(number: dict) -> dict:
    """For dashboard display: this number's Meta quality_rating
    (GREEN/YELLOW/RED), refreshed from Meta at most once per UTC calendar
    day (same "sync on first use each day" pattern as _ensure_fresh_tier),
    then cached on the whatsapp_numbers row itself via
    storage.update_whatsapp_number_quality() - not in waba_limits,
    since quality_rating isn't pooled across a WABA the way the tier is.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    synced_at = number.get("quality_synced_at")
    if synced_at and synced_at[:10] == today:
        return {"quality_rating": number.get("quality_rating"), "synced_at": synced_at}

    rating = sync_quality_from_meta(number["access_token"], number["phone_number_id"])
    if rating:
        now_iso = datetime.now(timezone.utc).isoformat()
        storage.update_whatsapp_number_quality(number["id"], rating, now_iso)
        return {"quality_rating": rating, "synced_at": now_iso}

    # Sync failed - keep whatever was cached before rather than reporting
    # nothing, same fallback philosophy as _ensure_fresh_tier.
    return {"quality_rating": number.get("quality_rating"), "synced_at": synced_at}
