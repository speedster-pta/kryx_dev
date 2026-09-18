"""Shared rules for applying/classifying WhatsApp delivery-status webhook
events (sent/delivered/read/failed, from integrations/webhooks.py) against a
send_log or campaign_recipients row. Both tables' update-by-wamid functions
(storage/send_log.py, storage/campaigns.py) import
should_apply_delivery_status so the two tables can't drift into different
rules for handling duplicate or out-of-order events - Meta can redeliver the
same webhook, and delivery events aren't guaranteed to arrive in order.

Ported from the single-tenant parent project (dev-whatsapp) - see that
project's storage/message_status.py. conversation_messages (storage/
conversations.py) keeps its own separate _DELIVERY_STATUS_RANK/
update_delivery_status rather than sharing this module: it predates this
port and follows a slightly different convention (failed ranks lowest
rather than being handled as a special case), so it is left as-is rather
than folded in here.
"""

_RANK = {"sent": 0, "delivered": 1, "read": 2}

# Counts attached wherever a delivery-outcome summary is shown - computed
# live from status/delivery_status rather than maintained as running
# counters, since delivery events arrive asynchronously from the webhook,
# sometimes long after the original send, and that code path only knows a
# wamid, not which summary it should update.
EMPTY_DELIVERY_COUNTS = {"delivered": 0, "read": 0, "delivery_failed": 0, "pending": 0}


def classify_delivery(status: str, delivery_status: str | None) -> str | None:
    """Classifies one send_log/campaign_recipients row into a delivery-
    outcome bucket. 'delivery_failed' folds together two distinct failure
    points - the Graph API itself rejecting the send (status='failed'),
    and Meta accepting the send but later reporting it failed to deliver
    (status='sent', delivery_status='failed') - since from a "did this
    person get the message" standpoint they're the same outcome. 'pending'
    means Meta accepted the send but no delivery event has arrived yet -
    could mean the webhook hasn't fired yet, isn't configured for this
    number, or (specifically for 'read') the recipient has read receipts
    turned off in WhatsApp, which means a message can sit at 'delivered'
    forever and never technically become 'read' even though it was.
    Returns None for a skipped (blank-phone) row, or a still-in-flight
    'deferred' send, neither of which is a delivery outcome at all."""
    if status == "failed" or delivery_status == "failed":
        return "delivery_failed"
    if delivery_status == "read":
        return "read"
    if delivery_status == "delivered":
        return "delivered"
    if status == "sent":
        return "pending"
    return None


def should_apply_delivery_status(new_status: str, current_status: str | None) -> bool:
    """A `sent` event arriving after `read` (e.g. a retried webhook
    delivery) must not clobber the more advanced state already recorded.
    `failed` is terminal for delivery purposes and can only apply while
    nothing more advanced than `sent` has been recorded - a message Meta
    already reported delivered/read plainly did not fail to send, and
    once `failed` is recorded no later event should overwrite it."""
    if current_status == "failed":
        return False
    if new_status == "failed":
        return current_status in (None, "sent")
    if current_status is None:
        return True
    return _RANK.get(new_status, -1) >= _RANK.get(current_status, -1)
