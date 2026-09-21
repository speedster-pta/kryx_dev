"""Voice Transcription module (storage.MODULE_VOICE_TRANSCRIPTION):
cleans up a Groq Whisper transcript via Claude and replies with it, but
only to a sender the assigned number has explicitly activated
(voice_transcription_allowed_senders).

maybe_reply_with_transcription() is called by
services/audio_transcription.py right after a transcript is available,
before it considers falling back to maybe_generate_ai_reply for the same
message - see that module's docstring for why a voice note only ever
triggers one or the other, never both, for a single number.

Every message this module claims is recorded to voice_transcription_log
(storage.record_voice_transcription), regardless of whether the reply
was actually delivered - this powers the /usage page's per-number
"Voice Transcriptions" counter (storage.voice_transcription_counts_by_number).
"""
from __future__ import annotations

from autosend import clients, storage
from autosend.config import settings
from autosend.integrations.whatsapp import MessagingLimitExceeded, WhatsAppSendError
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

_CLEANUP_SYSTEM_PROMPT = (
    "You clean up a raw voice-to-text transcript of a WhatsApp voice note for "
    "delivery back to the sender as a text message. Fix punctuation, capitalisation "
    "and obvious transcription errors, and remove filler words (um, uh) - but keep "
    "the speaker's own words and meaning intact, don't summarise or answer it. "
    "Reply with the cleaned transcript only - no preamble, no quotation marks."
)


def _model_supports_effort(model: str) -> bool:
    # Same Haiku restriction as services/ai_reply.py::_model_supports_effort -
    # those models 400 if output_config.effort is sent at all.
    return "haiku" not in model.lower()


async def _clean_up_transcript(transcript: str) -> str | None:
    """Returns None (rather than raising) on any failure - the caller
    treats that as "module handled this message, but couldn't produce a
    reply", same as a WhatsApp send failure, not a reason to fall back to
    maybe_generate_ai_reply."""
    settings_row = storage.get_voice_transcription_settings()
    model = settings_row.get("model") if settings_row else None
    if not model:
        logger.error(
            "Cannot clean up voice transcription - no model configured (a superadmin "
            "needs to set this under Voice Transcription Settings)"
        )
        return None

    if settings.dry_run:
        logger.info(
            "[SIMULATION MODE / DRY RUN] Intercepted voice transcription clean-up. Raw: %r",
            transcript,
        )
        return f"[SIMULATED] {transcript}"

    try:
        client = clients.get_anthropic_client()
    except ValueError:
        logger.exception("Cannot clean up voice transcription - AI credentials aren't configured")
        return None

    call_kwargs = {}
    effort = settings_row.get("effort")
    if effort and _model_supports_effort(model):
        call_kwargs["output_config"] = {"effort": effort}

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=1024,
            system=_CLEANUP_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": transcript}],
            **call_kwargs,
        )
    except Exception:
        logger.exception("Claude clean-up call failed for voice transcription")
        return None

    cleaned = "".join(block.text for block in response.content if block.type == "text").strip()
    return cleaned or transcript


async def _clean_up_and_deliver(conversation: dict, number: dict, transcript: str) -> bool:
    """Returns whether the cleaned-up transcript actually reached the
    contact - the caller logs this as voice_transcription_log.sent
    regardless of outcome, since every path here still counts as a
    processed request."""
    cleaned = await _clean_up_transcript(transcript)
    if cleaned is None:
        return False

    # Automated replies can only use free text (never a template), so this
    # fails cleanly - no send attempted - if the 24h session window is
    # already closed. Same pattern as services/ai_reply.py::_deliver_reply.
    if not storage.is_session_window_open(conversation):
        logger.warning(
            "Skipping voice transcription reply for conversation %s - WhatsApp session window is closed",
            conversation["id"],
        )
        storage.record_outbound_message(
            conversation["id"], sender_type="ai", message_type="text", body=cleaned,
            status="failed", error_message="WhatsApp 24h session window is closed",
        )
        return False

    client = clients.get_whatsapp_client_for_number(number)
    try:
        result = await client.send_text(conversation["contact_wa_id"], cleaned)
    except (MessagingLimitExceeded, WhatsAppSendError) as exc:
        storage.record_outbound_message(
            conversation["id"], sender_type="ai", message_type="text", body=cleaned,
            status="failed", error_message=str(exc),
        )
        return False

    wamid = (result.get("messages") or [{}])[0].get("id")
    storage.record_outbound_message(
        conversation["id"], sender_type="ai", message_type="text", body=cleaned,
        wamid=wamid, status="sent",
    )
    return True


async def maybe_reply_with_transcription(
    conversation: dict, number: dict, transcript: str, inbound_message_id: int | None = None,
) -> bool:
    """Returns True if this module claimed the message (so the caller
    must not also run maybe_generate_ai_reply for it), regardless of
    whether the reply actually reached the contact. False means the
    module isn't applicable here - number not assigned, module not
    enabled, or sender not activated - and the caller should fall through
    to its normal handling."""
    if not number.get("voice_transcription_enabled"):
        return False
    if not storage.is_enabled(number["org_id"], storage.MODULE_VOICE_TRANSCRIPTION):
        return False
    if not storage.is_voice_transcription_sender_allowed(number["id"], conversation["contact_wa_id"]):
        return False

    sent = await _clean_up_and_deliver(conversation, number, transcript)
    storage.record_voice_transcription(
        whatsapp_number_id=number["id"], conversation_id=conversation["id"],
        inbound_message_id=inbound_message_id, sent=sent,
    )
    return True
