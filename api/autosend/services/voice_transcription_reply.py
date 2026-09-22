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

_language_hint() appends the number's configured
voice_transcription_language(s) to whatever system prompt is in effect
(superadmin default or override), so Claude knows which language(s) it's
correcting toward - this is on top of, not instead of, the language hint
already passed to Groq at transcription time (see
services/audio_transcription.py); the raw transcript Claude sees here can
still contain a Whisper mistranscription in the wrong language even when
that hint was sent, since Groq's `prompt` only nudges rather than locks.

The "fix Whisper's spelling artefact" instruction is deliberately scoped
per-language via _CONFUSABLE_SPELLING (currently just Afrikaans, which
Whisper frequently mistranscribes using Dutch spelling conventions - a
closely related language, not one the org selected) rather than phrased
as "correct a word toward whichever selected language was intended". An
earlier version did the latter for multi-language numbers and it back-
fired in production: given English+Afrikaans, Claude read that as licence
to translate an entire English sentence into Afrikaans rather than just
fix spelling, which is not what "correct a transcription artefact" was
meant to authorise. English and Afrikaans aren't a close/parallel
language pair - only actually-confusable pairs (Afrikaans/Dutch) get the
correction licence; the multi-language case instead gets an explicit
"never translate between these" instruction.

This "fix Whisper's spelling artefact" instruction is Whisper-specific in
practice, not just in name: _language_hint() only appends it when
voice_transcription_settings.transcription_provider is "groq". A same-note
comparison of Groq Whisper vs. ElevenLabs Scribe raw transcripts showed
ElevenLabs doesn't reproduce the Dutch-spelling habit this hint corrects
for - so on ElevenLabs, the correction examples mostly describe errors
that aren't in the raw transcript, and leaving the hint active risks
Claude "correcting" already-right words instead of doing nothing.
"""
from __future__ import annotations

from autosend import clients, storage
from autosend.config import settings
from autosend.integrations.whatsapp import MessagingLimitExceeded, WhatsAppSendError
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_CLEANUP_PROMPT = (
    "You clean up a raw voice-to-text transcript of a WhatsApp voice note for "
    "delivery back to the sender as a text message. Fix punctuation, capitalisation "
    "and obvious transcription errors, and remove filler words (um, uh) - but keep "
    "the speaker's own words and meaning intact, don't summarise or answer it. "
    "Reply with the cleaned transcript only - no preamble, no quotation marks."
)

# The two hardcoded prompt fragments _language_hint() appends to the
# cleanup prompt above - superadmin-editable via the Voice Transcription
# Settings admin page (voice_transcription_settings.multi_language_hint /
# .confusable_spelling_hint), same "blank means built-in default" pattern
# as _DEFAULT_CLEANUP_PROMPT. Kept as format-string templates (not raw
# text) so an override still gets the actual per-number language(s)/
# pattern data substituted in, rather than having to hand-write those
# per number.
_DEFAULT_MULTI_LANGUAGE_HINT = (
    " The speaker may switch between {languages} within the same message - keep "
    "whichever of these languages is actually being spoken at each point rather than "
    "forcing everything into one. Never translate a word or sentence from one of these "
    "languages into another - if the speaker said it in one, keep it in that one."
)

_DEFAULT_CONFUSABLE_SPELLING_HINT = (
    " Whisper's speech-to-text step frequently mistranscribes {name} words using "
    "{confusable} spelling - a different but closely related language, not one "
    "the speaker is using here. Common patterns to correct: {patterns} Apply "
    "these systematically to every matching word in the transcript, not just the first "
    "one you notice. Correcting these is fixing a known transcription artefact, not "
    "guessing at unclear content, so apply it even if you're generally told to leave "
    "uncertain wording alone. This only licenses a spelling fix within {name} itself - "
    "never use it to change the word's language or meaning."
)


def _model_supports_effort(model: str) -> bool:
    # Same Haiku restriction as services/ai_reply.py::_model_supports_effort -
    # those models 400 if output_config.effort is sent at all.
    return "haiku" not in model.lower()


# Selectable voice_transcription_language codes (storage.LANGUAGE_CHOICES)
# that Whisper is known to mistranscribe using a *different, closely
# related* language's spelling conventions - not one the org selected,
# just one close enough that Whisper's model leans on it. Afrikaans and
# Dutch share most of their vocabulary and a lot of orthography, so
# Whisper frequently spells an Afrikaans word the Dutch way. This is
# deliberately not "any two selected languages are close" - English and
# Afrikaans, say, are both spoken by the same person but aren't a
# close/parallel pair, and treating them as one caused Claude to
# translate rather than just fix spelling (see module docstring). Only
# genuinely confusable pairs, not every language combo an org might
# select together, belong here.
#
# Looked up per language via storage.get_voice_transcription_confusable_spelling
# (table voice_transcription_confusable_spellings) rather than a hardcoded
# dict - a superadmin can add a new confusable pair via the Voice
# Transcription Confusable Spellings admin screen. The one pair this
# module shipped with (Afrikaans mistranscribed as Dutch) is seeded once
# into that table (storage.seed_default_confusable_spelling) with
# concrete Dutch->Afrikaans orthographic shifts spelled out - a bare
# "watch out for Dutch spelling" instruction measurably under-corrected in
# production (misschien, gedachte, gemakkelijk, verskrikkelijk and licht
# all passed through uncorrected in one real transcript, and it even
# introduced a Dutch spelling - "opvangst" -> "ontvangst" instead of
# Afrikaans "ontvangs" - while "fixing" a typo). Concrete letter-pattern
# rules with examples give Claude something to pattern-match against
# instead of relying on it to spot every instance unprompted - keep that
# in mind when editing/adding a row via the admin screen.


def _language_hint(number: dict, settings_row: dict | None) -> str:
    """Extra system-prompt text built from this number's configured
    voice_transcription_language(s) - separate from the superadmin-edited
    base prompt so a custom prompt still gets this appended, rather than
    having to repeat it in every custom prompt. Empty string (no hint
    appended) when the number has no languages configured, same
    auto-detect-only behaviour as before this existed.

    Two independent things, not one blended instruction: (1) for
    multi-language numbers, tell Claude the speaker may code-switch and to
    keep each part in whichever language it was actually said in, never
    translating between the selected languages; (2) for any selected
    language with a known confusable near-language on file
    (storage.get_voice_transcription_confusable_spelling), a narrowly-
    scoped licence to fix Whisper's spelling artefact for that language
    only - not licence to change a word's language or meaning. Part (2) is
    skipped entirely when the active transcription_provider isn't "groq":
    it was written to patch a Whisper-specific habit (e.g. transcribing
    Afrikaans using Dutch orthography), and a side-by-side comparison of
    the same voice note through both providers showed ElevenLabs doesn't
    reproduce that habit in its raw output - the words this hint tells
    Claude to "correct" mostly aren't wrong to begin with on that provider,
    so leaving it active there just invites the model to over-correct
    already-right words instead of doing nothing.

    Both fragments are superadmin-editable templates
    (voice_transcription_settings.multi_language_hint/
    confusable_spelling_hint) rather than fixed strings, falling back to
    the _DEFAULT_* constants above when blank - same pattern as `prompt`
    itself."""
    languages = storage.parse_voice_transcription_languages(number.get("voice_transcription_language"))
    if not languages:
        return ""

    settings_row = settings_row or {}
    provider = settings_row.get("transcription_provider") or "groq"
    multi_language_template = settings_row.get("multi_language_hint") or _DEFAULT_MULTI_LANGUAGE_HINT
    confusable_spelling_template = settings_row.get("confusable_spelling_hint") or _DEFAULT_CONFUSABLE_SPELLING_HINT

    def _safe_format(template: str, default: str, **kwargs) -> str:
        # These templates are free-text superadmin input (not developer-
        # controlled), so a typo'd/missing placeholder must not crash the
        # send path - fall back to the built-in default and log it instead.
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            logger.warning("Voice transcription hint template is malformed, using built-in default: %r", template)
            return default.format(**kwargs)

    parts = []
    if len(languages) > 1:
        described = storage.describe_languages(languages)
        parts.append(_safe_format(multi_language_template, _DEFAULT_MULTI_LANGUAGE_HINT, languages=described))

    # Framed as "not guessing" on purpose: some superadmin-edited base
    # prompts (reasonably) tell Claude to leave unclear wording alone
    # rather than guess at it. A word that could be Dutch or Afrikaans is
    # exactly the kind of case that instruction would otherwise apply to,
    # which would quietly disable this fix. Spelling that out here keeps
    # this hint effective regardless of what the base prompt says.
    #
    # Only relevant for Whisper (see docstring) - skip the lookup entirely
    # for any other provider rather than appending a hint whose examples
    # don't apply to that provider's transcription habits.
    if provider == "groq":
        for code in languages:
            info = storage.get_voice_transcription_confusable_spelling(code)
            if not info:
                continue
            name = storage.describe_languages([code])
            parts.append(_safe_format(
                confusable_spelling_template, _DEFAULT_CONFUSABLE_SPELLING_HINT,
                name=name, confusable=info["confusable"], patterns=info["patterns"],
            ))

    return "".join(parts)


async def _clean_up_transcript(transcript: str, number: dict) -> tuple[str | None, dict]:
    """Returns (cleaned_text, usage) - cleaned_text is None (rather than
    raising) on any failure, which the caller treats as "module handled
    this message, but couldn't produce a reply", same as a WhatsApp send
    failure, not a reason to fall back to maybe_generate_ai_reply. usage is
    {"prompt_tokens", "completion_tokens", "model"} straight off
    response.usage.input_tokens/output_tokens - all None when the Claude
    call was never attempted (e.g. no model configured), passed through to
    storage.record_voice_transcription() so the /usage page's Voice
    Transcription Clean-up card reflects real Anthropic token spend, not
    just call counts."""
    empty_usage = {"prompt_tokens": None, "completion_tokens": None, "model": None}
    settings_row = storage.get_voice_transcription_settings()
    model = settings_row.get("model") if settings_row else None
    if not model:
        logger.error(
            "Cannot clean up voice transcription - no model configured (a superadmin "
            "needs to set this under Voice Transcription Settings)"
        )
        return None, empty_usage

    if settings.dry_run:
        logger.info(
            "[SIMULATION MODE / DRY RUN] Intercepted voice transcription clean-up. Raw: %r",
            transcript,
        )
        return f"[SIMULATED] {transcript}", empty_usage

    try:
        client = clients.get_anthropic_client()
    except ValueError:
        logger.exception("Cannot clean up voice transcription - AI credentials aren't configured")
        return None, empty_usage

    call_kwargs = {}
    effort = settings_row.get("effort")
    if effort and _model_supports_effort(model):
        call_kwargs["output_config"] = {"effort": effort}

    system_prompt = (settings_row.get("prompt") or _DEFAULT_CLEANUP_PROMPT) + _language_hint(number, settings_row)
    logger.info("Cleaning up voice transcript for number %s with system prompt=%r", number["id"], system_prompt)

    try:
        response = await client.messages.create(
            model=model,
            # 16000 (was 4096, was 1024 before that) - the paragraph-break
            # instruction in the default/typical custom prompt splits long
            # transcripts across more lines without shortening them, and a
            # several-minute WhatsApp voice note easily clears 1024 tokens
            # once cleaned up. 4096 turned out to still be too low for a
            # different reason: Claude Sonnet/Opus models run adaptive
            # thinking by default even though this call never sets a
            # `thinking` param, and thinking tokens count against the same
            # max_tokens ceiling as the visible reply - so a call can spend
            # the whole budget thinking and come back with zero text. That
            # happened in production on a real transcript (effort=medium on
            # claude-sonnet-5): the response had no text blocks at all, and
            # `cleaned or transcript` below silently shipped the raw,
            # unclean transcript instead of erroring or retrying. 16000
            # gives thinking and the final output both room regardless of
            # which model/effort is configured.
            max_tokens=16000,
            system=system_prompt,
            messages=[{"role": "user", "content": transcript}],
            **call_kwargs,
        )
    except Exception:
        logger.exception("Claude clean-up call failed for voice transcription")
        return None, {"prompt_tokens": None, "completion_tokens": None, "model": model}

    usage = {
        "prompt_tokens": response.usage.input_tokens,
        "completion_tokens": response.usage.output_tokens,
        "model": model,
    }

    cleaned = "".join(block.text for block in response.content if block.type == "text").strip()
    if not cleaned:
        logger.warning(
            "Claude clean-up call for voice transcription (number %s, model %s) returned no "
            "text - stop_reason=%r. Falling back to the raw transcript instead of failing "
            "outright, but this reply will be unclean/un-paragraphed.",
            number["id"], model, getattr(response, "stop_reason", None),
        )
    return cleaned or transcript, usage


async def _clean_up_and_deliver(conversation: dict, number: dict, transcript: str) -> tuple[bool, dict]:
    """Returns (delivered, usage) - delivered is whether the cleaned-up
    transcript actually reached the contact; the caller logs delivered as
    voice_transcription_log.sent regardless of outcome, since every path
    here still counts as a processed request. usage is the Claude clean-up
    call's own token usage from _clean_up_transcript, passed straight
    through unchanged after this point."""
    cleaned, usage = await _clean_up_transcript(transcript, number)
    if cleaned is None:
        return False, usage

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
        return False, usage

    client = clients.get_whatsapp_client_for_number(number)
    try:
        result = await client.send_text(conversation["contact_wa_id"], cleaned)
    except (MessagingLimitExceeded, WhatsAppSendError) as exc:
        storage.record_outbound_message(
            conversation["id"], sender_type="ai", message_type="text", body=cleaned,
            status="failed", error_message=str(exc),
        )
        return False, usage

    wamid = (result.get("messages") or [{}])[0].get("id")
    storage.record_outbound_message(
        conversation["id"], sender_type="ai", message_type="text", body=cleaned,
        wamid=wamid, status="sent",
    )
    return True, usage


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

    sent, usage = await _clean_up_and_deliver(conversation, number, transcript)
    storage.record_voice_transcription(
        whatsapp_number_id=number["id"], conversation_id=conversation["id"],
        inbound_message_id=inbound_message_id, sent=sent,
        prompt_tokens=usage["prompt_tokens"], completion_tokens=usage["completion_tokens"], model=usage["model"],
    )
    return True
