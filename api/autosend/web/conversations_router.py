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


class ReplyIn(BaseModel):
    text: str


@router.post("/api/conversations/{conversation_id}/reply")
async def api_reply(conversation_id: int, payload: ReplyIn, user: dict = Depends(get_current_web_user)):
    conversation = _get_conversation_if_authorized(user, conversation_id)
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message text is required")

    if not storage.is_session_window_open(conversation):
        raise HTTPException(
            status_code=400,
            detail=(
                "This conversation's 24-hour WhatsApp session window is closed - "
                "only a pre-approved template message can be sent now, not free text."
            ),
        )

    number = storage.get_whatsapp_number_by_id(conversation["whatsapp_number_id"])
    if not number or not number["active"]:
        raise HTTPException(status_code=400, detail="This WhatsApp number is no longer active")

    client = clients.get_whatsapp_client_for_number(number)
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
