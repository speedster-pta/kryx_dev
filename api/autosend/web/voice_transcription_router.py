"""Per-WhatsApp-number Voice Transcription settings JSON API - which
number is assigned to receive forwarded voice notes, and that number's
whitelist of activated sending numbers
(services/voice_transcription_reply.py only replies to a whitelisted
sender).

Reuses numbers_router.py's _get_number_if_authorized for unit-scoped
access checks on a number, same precedent as ai_settings_router.py.
"""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from autosend import storage
from autosend.web.auth import get_current_web_user
from autosend.web.numbers_router import _get_number_if_authorized

router = APIRouter(prefix="/api/voice-transcription", tags=["voice-transcription"])


def _require_module(number: dict, user: dict) -> None:
    if user["is_superadmin"]:
        return
    if not storage.is_enabled(number["org_id"], storage.MODULE_VOICE_TRANSCRIPTION):
        raise HTTPException(
            status_code=403, detail="The Voice Note Transcription module isn't enabled for this organisation",
        )


def _normalize_wa_id(raw: str) -> str:
    # WhatsApp's own wa_id (msg["from"] on an inbound webhook event) is
    # always digits only, no "+"/spaces/dashes - strip those here so a
    # sender typed as "+27 82 123 4567" still matches the wa_id an
    # inbound voice note actually carries.
    return re.sub(r"\D", "", raw)


@router.get("/{number_id}")
def api_get_settings(number_id: int, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    _require_module(number, user)
    return {
        "whatsapp_number_id": number_id,
        "label": number["label"],
        "voice_transcription_enabled": bool(number.get("voice_transcription_enabled")),
    }


class SettingsIn(BaseModel):
    voice_transcription_enabled: bool


@router.post("/{number_id}")
def api_save_settings(number_id: int, payload: SettingsIn, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    _require_module(number, user)
    storage.set_voice_transcription_enabled(number_id, payload.voice_transcription_enabled)
    return {"ok": True}


@router.get("/{number_id}/senders")
def api_list_senders(number_id: int, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    _require_module(number, user)
    return storage.list_voice_transcription_allowed_senders(number_id)


class SenderIn(BaseModel):
    wa_id: str
    label: str | None = None


def _sender_or_404(number_id: int, sender_id: int) -> dict:
    sender = storage.get_voice_transcription_allowed_sender(sender_id)
    if not sender or sender["whatsapp_number_id"] != number_id:
        raise HTTPException(status_code=404, detail="Sender not found")
    return sender


@router.post("/{number_id}/senders")
def api_add_sender(number_id: int, payload: SenderIn, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    _require_module(number, user)
    wa_id = _normalize_wa_id(payload.wa_id)
    if not wa_id:
        raise HTTPException(status_code=400, detail="A valid sending number is required")
    if storage.is_voice_transcription_sender_allowed(number_id, wa_id):
        raise HTTPException(status_code=400, detail="This sending number is already activated")
    label = payload.label.strip() if payload.label else None
    sender_id = storage.add_voice_transcription_allowed_sender(number_id, wa_id, label)
    return {"id": sender_id}


@router.delete("/{number_id}/senders/{sender_id}")
def api_delete_sender(number_id: int, sender_id: int, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    _require_module(number, user)
    _sender_or_404(number_id, sender_id)
    storage.delete_voice_transcription_allowed_sender(sender_id)
    return {"deleted": sender_id}
