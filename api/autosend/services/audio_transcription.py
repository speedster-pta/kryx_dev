"""Speech-to-text transcription of inbound WhatsApp voice notes, via
either Groq Whisper (default) or ElevenLabs Scribe - the provider is a
platform-wide choice (voice_transcription_settings.transcription_provider,
storage.get_transcription_provider()), not per-number or per-message, set
from the Voice Transcription Settings admin page.

Runs as a FastAPI BackgroundTask, scheduled right after the voice note's
media finishes downloading (see integrations/webhooks.py/
integrations/whatsapp_media.py) - never scheduled alongside the generic
maybe_generate_ai_reply background task for the same message, since this
function's own completion re-invokes that itself once a transcript
exists. Scheduling both would double up the AI/keyword reply for every
voice note.

Once a transcript exists, it's first offered to the Voice Transcription
module (services/voice_transcription_reply.py) - if that number is
assigned to the module and the sender is on its whitelist, that module's
own clean-up-and-reply pipeline handles the message and
maybe_generate_ai_reply is skipped entirely, same "never both" invariant
as above.

whatsapp_numbers.voice_transcription_language (set on the Voice
Transcription settings page, web/voice_transcription_router.py, stored as
a comma-joined list of ISO-639-1 codes) shapes the provider call the same
way regardless of which one is active: none selected leaves it pure
auto-detect (unchanged behaviour); exactly one selected is passed as that
provider's own language-lock parameter, which hard-locks the whole clip
to it (the most reliable option, but wrong for a note in a different
language). More than one selected is deliberately NOT turned into a
free-text prompt/hint for either provider - an earlier version built one
for Groq (e.g. "This WhatsApp voice note may mix English and
Afrikaans.") to nudge code-switch retention, but in production that exact
sentence started showing up verbatim, repeated, inside real transcripts:
Whisper's decoder mistook the prompt text for spoken dialogue, looped on
it, and appeared to skip large stretches of the actual audio as a result
(a 3.5-minute note came back as ~45 seconds of content). The nudge's
benefit was never confirmed; the damage was reproducible. Multi-language
numbers now get plain auto-detect at the provider stage for both
providers - language mixing is handled entirely by Claude's clean-up
pass instead (see services/voice_transcription_reply.py::_language_hint).
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

# ElevenLabs' speech-to-text endpoint also infers format from the uploaded
# filename - its supported container set is broader than Groq's, but kept
# to the same shortlist here (plus webm/ogg, WhatsApp's actual formats) so
# unexpected extensions fall back the same predictable way as the Groq path.
_ELEVENLABS_SUPPORTED_EXTENSIONS = {"flac", "mp3", "mp4", "mpeg", "mpga", "m4a", "ogg", "wav", "webm"}


def _pick_filename(message_id: int, path: Path, supported_extensions: set[str]) -> str:
    extension = path.suffix.lstrip(".").lower()
    if extension not in supported_extensions:
        extension = "ogg"
    return f"{message_id}.{extension}"


async def _transcribe_via_groq(message_id: int, path: Path, languages: list[str]) -> tuple[str | None, float | None]:
    credentials = storage.get_groq_credentials()
    if not credentials or not credentials.get("api_key") or not credentials.get("model"):
        logger.error(
            "Cannot transcribe message %s - Groq credentials/model aren't configured yet "
            "(a superadmin needs to set this up under Groq Credentials)", message_id,
        )
        return None, None

    filename = _pick_filename(message_id, path, _GROQ_SUPPORTED_EXTENSIONS)

    transcribe_kwargs = {}
    if len(languages) == 1:
        # Single selection: hard-lock the whole clip to it. Most reliable
        # option when this number only ever gets one language, and what
        # fixes the Afrikaans-read-as-Dutch case for a single-language number.
        transcribe_kwargs["language"] = languages[0]
    # More than one selection: no `language` (Groq can't force multiple)
    # and deliberately no `prompt` either - see module docstring for why a
    # free-text language-mix hint was removed.

    logger.info("Transcribing message %s via Groq with kwargs=%s", message_id, transcribe_kwargs)

    from autosend.clients import get_groq_client

    client = get_groq_client()
    transcription = await client.audio.transcriptions.create(
        model=credentials["model"],
        file=(filename, path.read_bytes()),
        **transcribe_kwargs,
    )
    # Duration isn't reported here - Groq only includes it in
    # response_format="verbose_json", which this call doesn't request (the
    # default json response is all callers need for the transcript text
    # itself). storage.record_transcription_call() still logs the call with
    # audio_duration_secs=None; only the /usage page's ElevenLabs card sums
    # that column, so a Groq call simply doesn't contribute to it.
    return (transcription.text or "").strip(), None


async def _transcribe_via_elevenlabs(message_id: int, path: Path, languages: list[str]) -> tuple[str | None, float | None]:
    credentials = storage.get_elevenlabs_credentials()
    if not credentials or not credentials.get("api_key") or not credentials.get("model"):
        logger.error(
            "Cannot transcribe message %s - ElevenLabs credentials/model aren't configured yet "
            "(a superadmin needs to set this up under ElevenLabs Credentials)", message_id,
        )
        return None, None

    filename = _pick_filename(message_id, path, _ELEVENLABS_SUPPORTED_EXTENSIONS)

    data = {"model_id": credentials["model"]}
    if len(languages) == 1:
        # Single selection: hard-lock the whole clip to it, same rationale
        # as the Groq `language` parameter above. ElevenLabs' language_code
        # accepts ISO-639-1/639-3 codes, same codes used for Groq.
        data["language_code"] = languages[0]
    # More than one selection: no language_code, plain auto-detect - same
    # "no free-text hint" reasoning as the Groq path above.

    logger.info("Transcribing message %s via ElevenLabs with data=%s", message_id, data)

    from autosend.clients import get_elevenlabs_client

    client = get_elevenlabs_client()
    response = await client.post(
        "/v1/speech-to-text",
        data=data,
        files={"file": (filename, path.read_bytes())},
    )
    response.raise_for_status()
    body = response.json()
    # audio_duration_secs is what storage.elevenlabs_usage_by_org() sums for
    # the /usage page's ElevenLabs card - ElevenLabs bills Scribe by audio
    # duration, not tokens, and the API doesn't report a separate character
    # count or cost figure in this response.
    return (body.get("text") or "").strip(), body.get("audio_duration_secs")


async def transcribe_inbound_audio(message_id: int) -> None:
    message = storage.get_message_with_unit(message_id)
    if not message or message["message_type"] != "audio":
        return
    if message["media_download_status"] != "downloaded" or not message["media_local_path"]:
        logger.warning("Cannot transcribe message %s - audio media not downloaded", message_id)
        return

    path = Path(message["media_local_path"])

    conversation = storage.get_conversation(message["conversation_id"])
    number = storage.get_whatsapp_number_by_id(conversation["whatsapp_number_id"]) if conversation else None

    languages = storage.parse_voice_transcription_languages(
        number.get("voice_transcription_language") if number else None
    )

    provider = storage.get_transcription_provider()
    transcribe = _transcribe_via_elevenlabs if provider == "elevenlabs" else _transcribe_via_groq

    try:
        transcript, audio_duration_secs = await transcribe(message_id, path, languages)
    except Exception:
        logger.exception("Failed to transcribe message %s via %s", message_id, provider)
        return

    if not transcript:
        return

    # Logged here, at the actual provider-call site, rather than inside the
    # Voice Transcription module below - a voice note gets transcribed for
    # every org regardless of whether that module claims it (see module
    # docstring), so per-org provider usage (the /usage page's ElevenLabs
    # card) has to be tracked unconditionally, not folded into
    # voice_transcription_log.
    storage.record_transcription_call(
        whatsapp_number_id=number["id"] if number else None,
        conversation_id=conversation["id"] if conversation else None,
        inbound_message_id=message_id,
        provider=provider,
        audio_duration_secs=audio_duration_secs,
    )

    storage.set_message_body(message_id, transcript)

    if conversation and number:
        from autosend.services.voice_transcription_reply import maybe_reply_with_transcription

        handled = await maybe_reply_with_transcription(conversation, number, transcript, message_id)
        if handled:
            return

    from autosend.services.ai_reply import maybe_generate_ai_reply
    await maybe_generate_ai_reply(message["conversation_id"], message_id)
