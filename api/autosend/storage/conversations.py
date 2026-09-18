"""WhatsApp Inbox: conversations (one per WhatsApp number + contact) and
their messages, in either direction.

Scoped via unit_id on conversations only (not a denormalised org_id) -
same convention as every other unit-scoped table in schema.py.
conversation_messages carries no tenant column at all - it scopes via
conversation_id -> conversations.unit_id, same as e.g.
campaign_recipients -> campaigns.unit_id.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ._db import _connect

SESSION_WINDOW_HOURS = 24

# WhatsApp delivery-status progression - used to reject an out-of-order or
# redelivered webhook event that would otherwise downgrade an
# already-delivered/-read message back to an earlier state.
_DELIVERY_STATUS_RANK = {"failed": 0, "sent": 1, "delivered": 2, "read": 3}

_MESSAGE_TYPE_PREVIEWS = {
    "image": "Photo",
    "audio": "Voice message",
    "video": "Video",
    "document": "Document",
    "location": "Location",
    "sticker": "Sticker",
    "contacts": "Contact card",
}

_CONVERSATION_COLUMNS = [
    "id", "unit_id", "unit_name", "whatsapp_number_id", "number_label",
    "contact_wa_id", "contact_name", "last_inbound_at", "last_outbound_at",
    "last_message_at", "last_message_preview", "unread_count", "ai_status",
    "created_at", "ai_auto_reply_enabled",
]

_CONVERSATION_SELECT = """
    SELECT c.id, c.unit_id, u.name AS unit_name, c.whatsapp_number_id, n.label AS number_label,
           c.contact_wa_id, c.contact_name, c.last_inbound_at, c.last_outbound_at,
           c.last_message_at, c.last_message_preview, c.unread_count, c.ai_status,
           c.created_at, n.ai_auto_reply_enabled
    FROM conversations c
    JOIN units u ON u.id = c.unit_id
    LEFT JOIN whatsapp_numbers n ON n.id = c.whatsapp_number_id
"""

_MESSAGE_COLUMNS = [
    "id", "conversation_id", "direction", "sender_type", "wamid", "message_type", "body",
    "media_id", "media_mime_type", "media_sha256", "media_file_size", "media_local_path",
    "media_download_status", "media_download_error", "template_name", "status",
    "error_message", "delivery_status", "delivery_updated_at", "delivery_error_message",
    "sent_by_user_id", "created_at",
]


def _preview_text(message_type: str, body: str | None) -> str:
    if message_type == "text" and body:
        return body[:120]
    return _MESSAGE_TYPE_PREVIEWS.get(message_type, message_type)


def _row_to_dict(columns: list[str], row) -> dict:
    return dict(zip(columns, row))


def list_conversations(
    unit_ids: list[int] | None, whatsapp_number_id: int | None = None,
    limit: int = 50, offset: int = 0,
) -> list[dict]:
    """unit_ids=None means unrestricted (superadmin), same convention as
    every other unit-scoped list function."""
    from .scoping import unit_scope_clause

    with _connect() as conn:
        scope = unit_scope_clause("c.unit_id", unit_ids, joiner="WHERE")
        if scope is None:
            return []
        clause, params = scope
        if whatsapp_number_id is not None:
            joiner = "AND" if clause else "WHERE"
            clause = f"{clause} {joiner} c.whatsapp_number_id = ?"
            params = [*params, whatsapp_number_id]
        rows = conn.execute(
            _CONVERSATION_SELECT + clause +
            " ORDER BY COALESCE(c.last_message_at, c.created_at) DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_dict(_CONVERSATION_COLUMNS, r) for r in rows]


def get_conversation(conversation_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(_CONVERSATION_SELECT + " WHERE c.id = ?", (conversation_id,)).fetchone()
        if not row:
            return None
        return _row_to_dict(_CONVERSATION_COLUMNS, row)


def get_or_create_conversation(
    unit_id: int, whatsapp_number_id: int, contact_wa_id: str, contact_name: str | None = None,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO conversations (unit_id, whatsapp_number_id, contact_wa_id, contact_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(whatsapp_number_id, contact_wa_id) DO UPDATE SET
                contact_name = COALESCE(excluded.contact_name, conversations.contact_name)
            """,
            (unit_id, whatsapp_number_id, contact_wa_id, contact_name, now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM conversations WHERE whatsapp_number_id = ? AND contact_wa_id = ?",
            (whatsapp_number_id, contact_wa_id),
        ).fetchone()
    return get_conversation(row[0])


def set_conversation_ai_status(conversation_id: int, ai_status: str) -> None:
    """ai_status: 'active' (AI Assistant may reply freely) | 'escalated'
    (the AI itself deferred to a human - see services/ai_reply.py) |
    'paused' (a staff member manually took over). Setting back to
    'active' is a manual staff action (via the Inbox), not automated by
    anything here."""
    with _connect() as conn:
        conn.execute("UPDATE conversations SET ai_status = ? WHERE id = ?", (ai_status, conversation_id))
        conn.commit()


def mark_conversation_read(conversation_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE conversations SET unread_count = 0 WHERE id = ?", (conversation_id,))
        conn.commit()


def is_session_window_open(conversation: dict) -> bool:
    """WhatsApp's 24h customer-service-window rule: a free-text reply is
    only allowed within 24h of the contact's last inbound message -
    outside that window, only a pre-approved template send is permitted
    by Meta."""
    last_inbound_at = conversation.get("last_inbound_at")
    if not last_inbound_at:
        return False
    last_inbound = datetime.fromisoformat(last_inbound_at)
    if last_inbound.tzinfo is None:
        last_inbound = last_inbound.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_inbound < timedelta(hours=SESSION_WINDOW_HOURS)


def list_messages(conversation_id: int, limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_MESSAGE_COLUMNS)} FROM conversation_messages "
            "WHERE conversation_id = ? ORDER BY created_at ASC, id ASC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
        return [_row_to_dict(_MESSAGE_COLUMNS, r) for r in rows]


def get_message_with_unit(message_id: int) -> dict | None:
    """A single message plus its owning conversation's unit_id, for the
    authenticated media-serving endpoint to check scope against without a
    second round trip."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join('m.' + c for c in _MESSAGE_COLUMNS)}, c.unit_id "
            "FROM conversation_messages m "
            "JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.id = ?",
            (message_id,),
        ).fetchone()
        if not row:
            return None
        return _row_to_dict(_MESSAGE_COLUMNS + ["unit_id"], row)


def record_inbound_message(
    conversation_id: int, *, wamid: str | None, message_type: str, body: str | None = None,
    media_id: str | None = None, media_mime_type: str | None = None,
    contact_name: str | None = None,
) -> int:
    """Idempotent on wamid - Meta retries webhook delivery on a timeout/
    non-2xx response, and this must not double-count an already-recorded
    message (extra unread, duplicate preview) on a redelivered event."""
    with _connect() as conn:
        if wamid:
            existing = conn.execute(
                "SELECT id FROM conversation_messages WHERE wamid = ?", (wamid,)
            ).fetchone()
            if existing:
                return existing[0]

        now = datetime.now(timezone.utc).isoformat()
        preview = _preview_text(message_type, body)
        cur = conn.execute(
            """
            INSERT INTO conversation_messages
                (conversation_id, direction, sender_type, wamid, message_type, body,
                 media_id, media_mime_type, media_download_status, status, created_at)
            VALUES (?, 'in', 'contact', ?, ?, ?, ?, ?, ?, 'received', ?)
            """,
            (
                conversation_id, wamid, message_type, body, media_id, media_mime_type,
                "pending" if media_id else None, now,
            ),
        )
        message_id = cur.lastrowid
        conn.execute(
            """
            UPDATE conversations
            SET last_inbound_at = ?, last_message_at = ?, last_message_preview = ?,
                unread_count = unread_count + 1,
                contact_name = COALESCE(?, contact_name)
            WHERE id = ?
            """,
            (now, now, preview, contact_name, conversation_id),
        )
        conn.commit()
        return message_id


def record_outbound_echo(
    conversation_id: int, *, wamid: str | None, message_type: str, body: str | None = None,
    media_id: str | None = None, media_mime_type: str | None = None,
) -> int | None:
    """A message staff sent from the linked WhatsApp Business App/Web
    itself (Coexistence's `smb_message_echoes` webhook field - see
    integrations/webhooks.py's _handle_message_echoes), not through this
    app, so there's no send-time DB write the way record_outbound_message's
    callers get one - this is that write, arriving asynchronously via the
    echo instead. Idempotent on wamid the same way record_inbound_message
    is, since Meta can redeliver this webhook too; sender_type='device'
    distinguishes it from a staff/AI reply sent through this app. Returns
    None on a redelivered wamid (a no-op, unlike record_outbound_message
    which always inserts) so the caller can skip scheduling a redundant
    media download."""
    with _connect() as conn:
        if wamid:
            existing = conn.execute(
                "SELECT id FROM conversation_messages WHERE wamid = ?", (wamid,)
            ).fetchone()
            if existing:
                return None

        now = datetime.now(timezone.utc).isoformat()
        preview = _preview_text(message_type, body)
        cur = conn.execute(
            """
            INSERT INTO conversation_messages
                (conversation_id, direction, sender_type, wamid, message_type, body,
                 media_id, media_mime_type, media_download_status, status, created_at)
            VALUES (?, 'out', 'device', ?, ?, ?, ?, ?, ?, 'sent', ?)
            """,
            (
                conversation_id, wamid, message_type, body, media_id, media_mime_type,
                "pending" if media_id else None, now,
            ),
        )
        message_id = cur.lastrowid
        conn.execute(
            """
            UPDATE conversations
            SET last_outbound_at = ?, last_message_at = ?, last_message_preview = ?
            WHERE id = ?
            """,
            (now, now, preview, conversation_id),
        )
        conn.commit()
        return message_id


def record_outbound_message(
    conversation_id: int, *, sender_type: str, message_type: str = "text",
    body: str | None = None, wamid: str | None = None, status: str = "sent",
    error_message: str | None = None, sent_by_user_id: int | None = None,
    template_name: str | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    preview = _preview_text(message_type, body)
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO conversation_messages
                (conversation_id, direction, sender_type, wamid, message_type, body,
                 template_name, status, error_message, sent_by_user_id, created_at)
            VALUES (?, 'out', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id, sender_type, wamid, message_type, body, template_name,
                status, error_message, sent_by_user_id, now,
            ),
        )
        message_id = cur.lastrowid
        if status == "sent":
            conn.execute(
                """
                UPDATE conversations
                SET last_outbound_at = ?, last_message_at = ?, last_message_preview = ?
                WHERE id = ?
                """,
                (now, now, preview, conversation_id),
            )
        conn.commit()
        return message_id


def set_message_body(message_id: int, body: str) -> None:
    """Fills in a message's body after the fact - used once a voice
    note's transcript is ready (services/audio_transcription.py). Also
    refreshes the owning conversation's last_message_preview when this is
    that conversation's most recent message, so the Inbox list shows the
    transcript instead of a bare "Voice message" placeholder."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT conversation_id FROM conversation_messages WHERE id = ?", (message_id,)
        ).fetchone()
        if not row:
            return
        conversation_id = row[0]
        conn.execute("UPDATE conversation_messages SET body = ? WHERE id = ?", (body, message_id))

        latest = conn.execute(
            "SELECT id FROM conversation_messages WHERE conversation_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if latest and latest[0] == message_id:
            conn.execute(
                "UPDATE conversations SET last_message_preview = ? WHERE id = ?",
                (_preview_text("text", body), conversation_id),
            )
        conn.commit()


def update_delivery_status(wamid: str, status: str, error_message: str | None = None) -> bool:
    """Applies a webhook delivery-status event to the matching outbound
    conversation_messages row, by wamid. Returns False (no-op) if no
    matching Inbox message is found - e.g. the wamid belongs to a
    campaign or transactional-automation send, which don't record
    delivery status against this table - or if the new status wouldn't be
    a forward move (an out-of-order/redelivered webhook must never
    downgrade an already-delivered/-read status back to an earlier one)."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, delivery_status FROM conversation_messages WHERE wamid = ?", (wamid,)
        ).fetchone()
        if not row:
            return False
        message_id, current_status = row
        if _DELIVERY_STATUS_RANK.get(status, -1) < _DELIVERY_STATUS_RANK.get(current_status, -1):
            return False
        conn.execute(
            """
            UPDATE conversation_messages
            SET delivery_status = ?, delivery_updated_at = ?, delivery_error_message = ?
            WHERE id = ?
            """,
            (status, now, error_message, message_id),
        )
        conn.commit()
        return True


def get_message_with_conversation(message_id: int) -> dict | None:
    """A single conversation_messages row plus its parent conversation's
    unit_id/whatsapp_number_id/contact_wa_id, for staff actions on a
    specific message (send-draft, discard-draft - see
    web/conversations_router.py) that need both the message itself and
    enough of its conversation to authorize/act on it without a second
    round trip through get_conversation()."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT cm.id, cm.conversation_id, cm.direction, cm.status, cm.message_type, cm.body,
                   cm.sender_type, c.unit_id, c.whatsapp_number_id, c.contact_wa_id
            FROM conversation_messages cm
            JOIN conversations c ON c.id = cm.conversation_id
            WHERE cm.id = ?
            """,
            (message_id,),
        ).fetchone()
        if not row:
            return None
        columns = [
            "id", "conversation_id", "direction", "status", "message_type", "body", "sender_type",
            "unit_id", "whatsapp_number_id", "contact_wa_id",
        ]
        return _row_to_dict(columns, row)


def mark_draft_sent(message_id: int, conversation_id: int, body: str | None, wamid: str | None) -> None:
    """Staff clicked "Send" on an AI-drafted message (status='draft', see
    maybe_generate_ai_reply's draft-mode write in services/ai_reply.py) -
    flips it to a normal sent row and updates the conversation's
    last_outbound_at/last_message_at/preview the same way
    record_outbound_message does for a fresh send, since a draft's
    original insert deliberately did NOT touch those (the contact hadn't
    seen it yet)."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE conversation_messages SET status = 'sent', wamid = ? WHERE id = ?",
            (wamid, message_id),
        )
        preview = _preview_text("text", body)
        conn.execute(
            "UPDATE conversations SET last_outbound_at = ?, last_message_at = ?, last_message_preview = ? WHERE id = ?",
            (now, now, preview, conversation_id),
        )
        conn.commit()


def delete_draft_message(message_id: int) -> None:
    """Staff clicked "Discard" on an AI-drafted message. Scoped to
    status='draft' in the WHERE clause itself (not just checked by the
    caller) so this can never delete a real sent/received message even if
    called with a stale/wrong id."""
    with _connect() as conn:
        conn.execute("DELETE FROM conversation_messages WHERE id = ? AND status = 'draft'", (message_id,))
        conn.commit()


def update_message_media_download(
    message_id: int, *, status: str, local_path: str | None = None,
    error: str | None = None, sha256: str | None = None, file_size: int | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversation_messages
            SET media_download_status = ?,
                media_local_path = COALESCE(?, media_local_path),
                media_download_error = ?,
                media_sha256 = COALESCE(?, media_sha256),
                media_file_size = COALESCE(?, media_file_size)
            WHERE id = ?
            """,
            (status, local_path, error, sha256, file_size, message_id),
        )
        conn.commit()
