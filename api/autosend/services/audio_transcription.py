"""Groq Whisper transcription of inbound WhatsApp voice notes.

Runs as a FastAPI BackgroundTask, scheduled right after the voice note's
media finishes downloading (see integrations/webhooks.py/
integrations/whatsapp_media.py) - never scheduled alongside the generic
maybe_generate_ai_reply background task for the same message, since this
function's own completion re-invokes that itself once a transcript
exists. Scheduling both would double up the AI/keyword reply for every
voice note.
"""
from pathlib import Path

from autosend import storage
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

# Groq's transcription endpoint infers audio format from the uploaded
# filename's extension - map anything outside its supported container set
# to the closest one it understands rather than failing outright. WhatsApp
# voice notes are audio/ogg (Opus) in the overwhelming majority of cases.
_GROQ_SUPPORTED_EXTENSIONS = {"flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm"}


async def transcribe_inbound_audio(message_id: int) -> None:
    message = storage.get_message_with_unit(message_id)
    if not message or message["message_type"] != "audio":
        return
    if message["media_download_status"] != "downloaded" or not message["media_local_path"]:
        logger.warning("Cannot transcribe message %s - audio media not downloaded", message_id)
        return

    credentials = storage.get_groq_credentials()
    if not credentials or not credentials.get("api_key") or not credentials.get("model"):
        logger.error(
            "Cannot transcribe message %s - Groq credentials/model aren't configured yet "
            "(a superadmin needs to set this up under Groq Credentials)", message_id,
        )
        return

    path = Path(message["media_local_path"])
    extension = path.suffix.lstrip(".").lower()
    if extension not in _GROQ_SUPPORTED_EXTENSIONS:
        extension = "ogg"
    filename = f"{message_id}.{extension}"

    try:
        from autosend.clients import get_groq_client

        client = get_groq_client()
        transcription = await client.audio.transcriptions.create(
            model=credentials["model"],
            file=(filename, path.read_bytes()),
        )
        transcript = (transcription.text or "").strip()
    except Exception:
        logger.exception("Failed to transcribe message %s", message_id)
        return

    if not transcript:
        return

    storage.set_message_body(message_id, transcript)

    from autosend.services.ai_reply import maybe_generate_ai_reply
    await maybe_generate_ai_reply(message["conversation_id"], message_id)
