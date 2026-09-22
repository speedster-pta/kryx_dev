"""storage/voice_transcription.py

Voice Transcription module (storage.MODULE_VOICE_TRANSCRIPTION): which
WhatsApp number is assigned to receive forwarded voice notes
(whatsapp_numbers.voice_transcription_enabled), that number's whitelist
of activated sending numbers (voice_transcription_allowed_senders) -
only a whitelisted sender's voice note gets a transcription reply, see
services/voice_transcription_reply.py - and an append-only log of every
voice note the module claimed (voice_transcription_log), which powers
the /usage page's per-number counter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ._db import _connect

_SENDER_COLUMNS = ["id", "whatsapp_number_id", "wa_id", "label", "created_at"]

# A curated (but not free-text) field: a bad/unsupported code would
# otherwise silently degrade to auto-detect instead of erroring. Lives
# here (not in web/voice_transcription_router.py) so
# services/audio_transcription.py can reuse the same code->name mapping
# when building a multi-language prompt hint, without a services->web
# import. This is the full set of ISO-639-1 codes Groq's Whisper models
# support (https://console.groq.com/docs/speech-to-text, "Supported
# Languages"), not just an SA-relevant subset - extend it only if Groq
# adds a language, and keep it sorted by display name so the dropdown in
# voice_transcription_settings.html stays easy to scan.
LANGUAGE_CHOICES = {
    "af": "Afrikaans",
    "sq": "Albanian",
    "am": "Amharic",
    "ar": "Arabic",
    "hy": "Armenian",
    "as": "Assamese",
    "az": "Azerbaijani",
    "ba": "Bashkir",
    "eu": "Basque",
    "be": "Belarusian",
    "bn": "Bengali",
    "bs": "Bosnian",
    "br": "Breton",
    "bg": "Bulgarian",
    "yue": "Cantonese",
    "ca": "Catalan",
    "zh": "Chinese",
    "hr": "Croatian",
    "cs": "Czech",
    "da": "Danish",
    "nl": "Dutch",
    "en": "English",
    "et": "Estonian",
    "fo": "Faroese",
    "fi": "Finnish",
    "fr": "French",
    "gl": "Galician",
    "ka": "Georgian",
    "de": "German",
    "el": "Greek",
    "gu": "Gujarati",
    "ht": "Haitian Creole",
    "ha": "Hausa",
    "haw": "Hawaiian",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "is": "Icelandic",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "jw": "Javanese",
    "kn": "Kannada",
    "kk": "Kazakh",
    "km": "Khmer",
    "ko": "Korean",
    "lo": "Lao",
    "la": "Latin",
    "lv": "Latvian",
    "lb": "Luxembourgish",
    "ln": "Lingala",
    "lt": "Lithuanian",
    "mk": "Macedonian",
    "mg": "Malagasy",
    "ms": "Malay",
    "ml": "Malayalam",
    "mt": "Maltese",
    "mi": "Maori",
    "mr": "Marathi",
    "mn": "Mongolian",
    "my": "Myanmar (Burmese)",
    "ne": "Nepali",
    "no": "Norwegian",
    "nn": "Norwegian Nynorsk",
    "oc": "Occitan",
    "ps": "Pashto",
    "fa": "Persian",
    "pl": "Polish",
    "pt": "Portuguese",
    "pa": "Punjabi",
    "ro": "Romanian",
    "ru": "Russian",
    "sa": "Sanskrit",
    "sr": "Serbian",
    "sn": "Shona",
    "sd": "Sindhi",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "so": "Somali",
    "es": "Spanish",
    "su": "Sundanese",
    "sw": "Swahili",
    "sv": "Swedish",
    "tl": "Tagalog",
    "tg": "Tajik",
    "ta": "Tamil",
    "tt": "Tatar",
    "te": "Telugu",
    "th": "Thai",
    "bo": "Tibetan",
    "tr": "Turkish",
    "tk": "Turkmen",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "uz": "Uzbek",
    "vi": "Vietnamese",
    "cy": "Welsh",
    "yi": "Yiddish",
    "yo": "Yoruba",
}


def parse_voice_transcription_languages(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [code for code in raw.split(",") if code]


def describe_languages(codes: list[str]) -> str:
    """Natural-language join of language codes' display names, e.g.
    ["en", "af"] -> "English and Afrikaans". Shared by
    services/audio_transcription.py (Groq prompt hint) and
    services/voice_transcription_reply.py (Claude clean-up prompt's
    language hint) so both are built from the same per-number selection
    the same way."""
    names = [LANGUAGE_CHOICES.get(code, code) for code in codes]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def get_voice_transcription_settings() -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT model, effort, prompt, multi_language_hint, confusable_spelling_hint, "
            "transcription_provider FROM voice_transcription_settings LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            "model": row[0], "effort": row[1], "prompt": row[2],
            "multi_language_hint": row[3], "confusable_spelling_hint": row[4],
            "transcription_provider": row[5] or "groq",
        }


def get_transcription_provider() -> str:
    """"groq" (default, Whisper) or "elevenlabs" (Scribe) - which provider
    services/audio_transcription.py sends raw audio to. Falls back to
    "groq" if the settings row doesn't exist yet (e.g. no superadmin has
    ever opened Voice Transcription Settings), same default as an unset
    column on an existing row."""
    settings = get_voice_transcription_settings()
    return (settings or {}).get("transcription_provider") or "groq"


def get_voice_transcription_confusable_spelling(language_code: str) -> dict | None:
    """Looked up per selected language when building the Claude clean-up
    prompt's confusable-spelling hint (services/voice_transcription_reply.py
    ::_language_hint) - the superadmin-editable replacement for what used
    to be a hardcoded _CONFUSABLE_SPELLING dict in that module. None means
    that language has no known confusable near-language on file, same as
    a missing dict entry before this existed."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT confusable_name, patterns FROM voice_transcription_confusable_spellings WHERE language_code = ?",
            (language_code,),
        ).fetchone()
        return {"confusable": row[0], "patterns": row[1]} if row else None


# The one confusable-spelling pair this module originally shipped with as a
# hardcoded dict (Afrikaans mistranscribed using Dutch spelling
# conventions) - see services/voice_transcription_reply.py's module
# docstring for the production history behind these exact patterns.
# Seeded once into voice_transcription_confusable_spellings so existing
# installs keep this correction after the table replaces the old hardcoded
# dict, without a superadmin having to re-type production-tuned wording.
_DEFAULT_SEEDED_CONFUSABLE_SPELLINGS = [
    (
        "af", "Dutch",
        "Dutch 'ij' -> Afrikaans 'y' (tijd -> tyd, blij -> bly, vrij -> vry); "
        "Dutch '-elijk' -> Afrikaans '-lik' (gemakkelijk -> gemaklik, "
        "verskrikkelijk -> verskriklik, moeilijk -> moeilik); Dutch 'ch'/'cht' -> "
        "Afrikaans 'g' (licht -> lig, gedachte -> gedagte); and standalone words "
        "such as Dutch 'misschien' -> Afrikaans 'miskien' and Dutch 'ontvangst' -> "
        "Afrikaans 'ontvangs'.",
    ),
]


def seed_default_confusable_spelling(conn) -> None:
    """One-time data seed, not a recurring migration (see storage/schema.py
    ::_create_voice_transcription_confusable_spellings and its own
    docstring) - guarded by a marker table, same pattern as
    storage.modules.migrate_legacy_email_wa_module_key, so this can never
    re-fire after its first successful run. That matters because a
    superadmin might deliberately delete the seeded Afrikaans/Dutch row
    (decide they don't want that correction); an unconditional
    INSERT-if-missing on every startup would silently bring it back. Call
    once, from core/db_init.py, after storage.schema.init_core_schema (the
    owning table must already exist)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _voice_transcription_confusable_spelling_seed (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            applied_at TEXT NOT NULL
        )
        """
    )
    if conn.execute("SELECT 1 FROM _voice_transcription_confusable_spelling_seed WHERE id = 1").fetchone():
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        "INSERT INTO voice_transcription_confusable_spellings "
        "(language_code, confusable_name, patterns, created_at) VALUES (?, ?, ?, ?)",
        [(code, name, patterns, now) for code, name, patterns in _DEFAULT_SEEDED_CONFUSABLE_SPELLINGS],
    )
    conn.execute("INSERT INTO _voice_transcription_confusable_spelling_seed (id, applied_at) VALUES (1, ?)", (now,))


def set_voice_transcription_enabled(whatsapp_number_id: int, enabled: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE whatsapp_numbers SET voice_transcription_enabled = ? WHERE id = ?",
            (int(enabled), whatsapp_number_id),
        )
        conn.commit()


def set_voice_transcription_language(whatsapp_number_id: int, languages: list[str] | None) -> None:
    """languages is a list of ISO-639-1 codes (e.g. ["en", "af"]), stored
    comma-joined, or None/empty for auto-detect. See
    services/audio_transcription.py for how zero/one/many selected
    languages are each used differently at transcription time."""
    with _connect() as conn:
        conn.execute(
            "UPDATE whatsapp_numbers SET voice_transcription_language = ? WHERE id = ?",
            (",".join(languages) if languages else None, whatsapp_number_id),
        )
        conn.commit()


def _row_to_sender(row) -> dict:
    return dict(zip(_SENDER_COLUMNS, row))


def list_voice_transcription_allowed_senders(whatsapp_number_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(_SENDER_COLUMNS)} FROM voice_transcription_allowed_senders "
            "WHERE whatsapp_number_id = ? ORDER BY created_at",
            (whatsapp_number_id,),
        ).fetchall()
        return [_row_to_sender(r) for r in rows]


def get_voice_transcription_allowed_sender(sender_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {', '.join(_SENDER_COLUMNS)} FROM voice_transcription_allowed_senders WHERE id = ?",
            (sender_id,),
        ).fetchone()
        return _row_to_sender(row) if row else None


def add_voice_transcription_allowed_sender(whatsapp_number_id: int, wa_id: str, label: str | None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_transcription_allowed_senders (whatsapp_number_id, wa_id, label, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (whatsapp_number_id, wa_id, label, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return cur.lastrowid


def delete_voice_transcription_allowed_sender(sender_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM voice_transcription_allowed_senders WHERE id = ?", (sender_id,))
        conn.commit()


def is_voice_transcription_sender_allowed(whatsapp_number_id: int, wa_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM voice_transcription_allowed_senders WHERE whatsapp_number_id = ? AND wa_id = ?",
            (whatsapp_number_id, wa_id),
        ).fetchone()
        return row is not None


def record_voice_transcription(
    *, whatsapp_number_id: int | None, conversation_id: int | None, inbound_message_id: int | None, sent: bool,
) -> int:
    """One row per voice note the module claimed (see
    services/voice_transcription_reply.py::maybe_reply_with_transcription),
    regardless of whether the reply actually reached the contact -
    sent=False still counts as "processed" (a real Claude clean-up call
    was attempted, or would have been if credentials were configured),
    same "claimed but not delivered" distinction as ai_reply_log.sent."""
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO voice_transcription_log
                (created_at, whatsapp_number_id, conversation_id, inbound_message_id, sent)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(), whatsapp_number_id, conversation_id,
                inbound_message_id, int(sent),
            ),
        )
        conn.commit()
        return cur.lastrowid


def voice_transcription_counts_by_number(days: int = 30) -> list[dict]:
    """Count of voice notes claimed (request_count) and how many actually
    got a reply delivered (sent_count) per WhatsApp number over the
    trailing `days` - feeds the /usage page's Voice Transcriptions card,
    same per-number shape as storage.usage.send_totals_by_number. Numbers
    with zero requests in the window simply don't appear."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT whatsapp_number_id, COUNT(*) AS request_count, COALESCE(SUM(sent), 0) AS sent_count
            FROM voice_transcription_log
            WHERE created_at >= ? AND whatsapp_number_id IS NOT NULL
            GROUP BY whatsapp_number_id
            ORDER BY request_count DESC
            """,
            (since,),
        ).fetchall()
    return [{"whatsapp_number_id": r[0], "request_count": r[1], "sent_count": r[2]} for r in rows]
