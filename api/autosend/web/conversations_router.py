"""WhatsApp Inbox JSON API - conversation list, thread, reply, and an
authenticated media-serving endpoint.

Registered in main.py alongside the other plain routers (not a BaseView
page shell) - see admin_pages.InboxView for the page shell itself, same
split as templates_router.py/automations_router.py vs their BaseViews.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from autosend import clients, storage
from autosend.integrations.whatsapp import MessagingLimitExceeded, WhatsAppSendError
from autosend.utils.logging import get_logger
from autosend.web.auth import get_current_web_user

router = APIRouter()
logger = get_logger(__name__)


def _accessible_unit_ids(user: dict) -> list[int] | None:
    return None if user["is_superadmin"] else user["unit_ids"]


def _get_conversation_if_authorized(user: dict, conversation_id: int) -> dict:
    conversation = storage.get_conversation(conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversation not found")
    unit_ids = _accessible_unit_ids(user)
    if unit_ids is not None and conversation["unit_id"] not in unit_ids:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.get("/api/conversations")
def api_list_conversations(
    whatsapp_number_id: int | None = None, limit: int = 50, offset: int = 0,
    user: dict = Depends(get_current_web_user),
):
    return storage.list_conversations(
        _accessible_unit_ids(user), whatsapp_number_id=whatsapp_number_id,
        limit=min(limit, 200), offset=max(offset, 0),
    )


@router.get("/api/conversations/{conversation_id}/messages")
def api_list_messages(conversation_id: int, user: dict = Depends(get_current_web_user)):
    conversation = _get_conversation_if_authorized(user, conversation_id)
    return {
        "conversation": conversation,
        "session_window_open": storage.is_session_window_open(conversation),
        "messages": storage.list_messages(conversation_id),
    }


@router.post("/api/conversations/{conversation_id}/read")
def api_mark_read(conversation_id: int, user: dict = Depends(get_current_web_user)):
    _get_conversation_if_authorized(user, conversation_id)
    storage.mark_conversation_read(conversation_id)
    return {"ok": True}


_AI_STATUSES = ("active", "escalated", "paused")


class AIStatusIn(BaseModel):
    ai_status: str


@router.post("/api/conversations/{conversation_id}/ai-status")
def api_set_ai_status(conversation_id: int, payload: AIStatusIn, user: dict = Depends(get_current_web_user)):
    """Lets staff pause the AI Assistant on a thread (take over manually)
    or resume it after handling an escalation - see services/ai_reply.py
    for how ai_status gates automated replies."""
    _get_conversation_if_authorized(user, conversation_id)
    if payload.ai_status not in _AI_STATUSES:
        raise HTTPException(status_code=400, detail=f"ai_status must be one of {_AI_STATUSES}")
    storage.set_conversation_ai_status(conversation_id, payload.ai_status)
    return {"ai_status": payload.ai_status}


class ReplyIn(BaseModel):
    type: str = "text"  # "text" (default, backward-compatible) or "template"
    # For type="text", the freeform message itself. For type="template",
    # the client's own rendering of the template's BODY text with {{n}}
    # placeholders already filled in from `variables` - stored purely so
    # the thread displays what was actually said instead of a generic
    # "[template message]" placeholder; the real Graph API send below
    # never reads this field for a template, only template_name/variables/
    # language.
    text: str | None = None
    template_name: str | None = None
    language: str | None = None
    variables: list[str] | None = None
    header_image_url: str | None = None
    button_values: list[str | None] | None = None


@router.post("/api/conversations/{conversation_id}/reply")
async def api_reply(conversation_id: int, payload: ReplyIn, user: dict = Depends(get_current_web_user)):
    """Freeform text is only valid inside the 24h session window
    (is_session_window_open); a template send is Meta's only way to reach
    a contact once that window has closed, and is allowed regardless of
    whether the window is open - see storage.conversations for the rule."""
    conversation = _get_conversation_if_authorized(user, conversation_id)
    number = storage.get_whatsapp_number_by_id(conversation["whatsapp_number_id"])
    if not number or not number["active"]:
        raise HTTPException(status_code=400, detail="This WhatsApp number is no longer active")
    client = clients.get_whatsapp_client_for_number(number)

    if payload.type == "template":
        if not payload.template_name:
            raise HTTPException(status_code=400, detail="template_name is required")

        try:
            result = await client.send_template(
                conversation["contact_wa_id"], payload.template_name, *(payload.variables or []),
                header_image_url=payload.header_image_url, button_values=payload.button_values,
                language=payload.language or "en",
            )
        except MessagingLimitExceeded as exc:
            storage.record_outbound_message(
                conversation_id, sender_type="staff", message_type="template", body=payload.text,
                template_name=payload.template_name, status="deferred", error_message=str(exc),
                sent_by_user_id=user["id"],
            )
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except WhatsAppSendError as exc:
            storage.record_outbound_message(
                conversation_id, sender_type="staff", message_type="template", body=payload.text,
                template_name=payload.template_name, status="failed", error_message=exc.message,
                sent_by_user_id=user["id"],
            )
            raise HTTPException(status_code=502, detail=exc.message) from exc

        wamid = (result.get("messages") or [{}])[0].get("id")
        message_id = storage.record_outbound_message(
            conversation_id, sender_type="staff", message_type="template", body=payload.text,
            template_name=payload.template_name, wamid=wamid, status="sent", sent_by_user_id=user["id"],
        )
        return {"id": message_id, "wamid": wamid}

    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message text is required")

    if not storage.is_session_window_open(conversation):
        raise HTTPException(
            status_code=400,
            detail=(
                "This conversation's 24-hour WhatsApp session window is closed. "
                "Send a pre-approved template message instead, not free text."
            ),
        )

    try:
        result = await client.send_text(conversation["contact_wa_id"], text)
    except MessagingLimitExceeded as exc:
        storage.record_outbound_message(
            conversation_id, sender_type="staff", message_type="text", body=text,
            status="deferred", error_message=str(exc), sent_by_user_id=user["id"],
        )
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except WhatsAppSendError as exc:
        storage.record_outbound_message(
            conversation_id, sender_type="staff", message_type="text", body=text,
            status="failed", error_message=exc.message, sent_by_user_id=user["id"],
        )
        raise HTTPException(status_code=502, detail=exc.message) from exc

    wamid = (result.get("messages") or [{}])[0].get("id")
    message_id = storage.record_outbound_message(
        conversation_id, sender_type="staff", message_type="text", body=text,
        wamid=wamid, status="sent", sent_by_user_id=user["id"],
    )
    return {"id": message_id, "wamid": wamid}


@router.post("/api/conversations/{conversation_id}/messages/{message_id}/send")
async def send_draft_message(
    conversation_id: int, message_id: int, user: dict = Depends(get_current_web_user),
):
    """Staff approving an AI-drafted reply/handoff (status='draft', see
    services/ai_reply.py's draft-mode write) - sends it as a normal
    freeform text message and flips the row to status='sent'. Re-checks
    the session window server-side, same as api_reply above, since a
    draft can sit unreviewed long enough for the window to close after it
    was written."""
    conversation = _get_conversation_if_authorized(user, conversation_id)
    message = storage.get_message_with_conversation(message_id)
    if not message or message["conversation_id"] != conversation_id:
        raise HTTPException(status_code=404, detail="Draft message not found")
    if message["status"] != "draft":
        raise HTTPException(status_code=400, detail="This message is not a pending draft")
    if not storage.is_session_window_open(conversation):
        raise HTTPException(
            status_code=409,
            detail=(
                "This conversation's 24-hour WhatsApp session window is closed. "
                "This draft can no longer be sent as free text."
            ),
        )

    number = storage.get_whatsapp_number_by_id(conversation["whatsapp_number_id"])
    if not number or not number["active"]:
        raise HTTPException(status_code=400, detail="This WhatsApp number is no longer active")

    client = clients.get_whatsapp_client_for_number(number)
    try:
        result = await client.send_text(conversation["contact_wa_id"], message["body"])
    except (MessagingLimitExceeded, WhatsAppSendError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    wamid = (result.get("messages") or [{}])[0].get("id")
    storage.mark_draft_sent(message_id, conversation_id, message["body"], wamid)
    return {"id": message_id, "status": "sent"}


@router.delete("/api/conversations/{conversation_id}/messages/{message_id}")
def discard_draft_message(conversation_id: int, message_id: int, user: dict = Depends(get_current_web_user)):
    """Staff rejecting an AI-drafted reply/handoff - deletes it outright
    rather than marking it discarded, since (unlike send_log's append-only
    "every attempt" history) a draft the contact never saw never happened
    from their side of the thread; delete_draft_message's own WHERE clause
    is the real safety net against ever removing a genuinely sent/received
    message."""
    _get_conversation_if_authorized(user, conversation_id)
    message = storage.get_message_with_conversation(message_id)
    if not message or message["conversation_id"] != conversation_id:
        raise HTTPException(status_code=404, detail="Draft message not found")
    if message["status"] != "draft":
        raise HTTPException(status_code=400, detail="Only a pending draft can be discarded")
    storage.delete_draft_message(message_id)
    return {"discarded": True}


@router.get("/api/conversations/media/{message_id}")
def api_conversation_media(message_id: int, user: dict = Depends(get_current_web_user)):
    message = storage.get_message_with_unit(message_id)
    if not message:
        raise HTTPException(status_code=404, detail="Media not found")
    unit_ids = _accessible_unit_ids(user)
    if unit_ids is not None and message["unit_id"] not in unit_ids:
        raise HTTPException(status_code=404, detail="Media not found")
    if message["media_download_status"] != "downloaded" or not message["media_local_path"]:
        raise HTTPException(status_code=404, detail="Media not available")
    return FileResponse(
        message["media_local_path"],
        media_type=message["media_mime_type"] or "application/octet-stream",
    )
