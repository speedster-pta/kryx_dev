"""
Live model lists for the AI Credentials page / AI Playground model pickers,
fetched from each provider's own "list models" endpoint (Anthropic
/v1/models, Groq /openai/v1/models, ElevenLabs /v1/models) rather than
hard-coded, so a newly released model shows up without a code change.
Ported from the parent single-tenant project's model_catalog.py.

Each list is cached in-process for _CACHE_TTL_SECONDS and invalidated by
invalidate() whenever AICredentialsView saves that provider's key. Any
failure (no key on file yet, invalid key, network error, timeout) falls
back to the static FALLBACK_* lists below rather than breaking the page -
those are never cached, so the live list is retried on the next load.

Unlike the pipelines themselves, this deliberately doesn't use the cached
clients in clients.py: those keep their key until an app restart (no cache
invalidation there), whereas each fetch here builds a short-lived client
from the key currently in the DB, so saving a new key shows that account's
models straight away.

The pickers also always include whatever model is currently saved, even if
the provider no longer lists it (e.g. a saved alias like "claude-haiku-4-5"
where the API only lists the dated snapshot), so opening and re-saving a
settings form never silently swaps the configured model.
"""
import asyncio
import logging
import time
from dataclasses import dataclass

from autosend import storage

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 6 * 60 * 60
# Caps how long a settings page render waits on a provider before giving up
# and using the fallback list instead.
_FETCH_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class ModelChoice:
    value: str
    label: str
    # Only meaningful for Claude models - whether output_config.effort is
    # accepted (see model_supports_effort below).
    supports_effort: bool = True


FALLBACK_CLAUDE_MODELS = [
    ModelChoice("claude-opus-5", "Claude Opus 5"),
    ModelChoice("claude-sonnet-5", "Claude Sonnet 5"),
    ModelChoice("claude-haiku-4-5", "Claude Haiku 4.5", supports_effort=False),
]
FALLBACK_WHISPER_MODELS = [
    ModelChoice("whisper-large-v3", "whisper-large-v3"),
    ModelChoice("whisper-large-v3-turbo", "whisper-large-v3-turbo"),
]
FALLBACK_SCRIBE_MODELS = [
    ModelChoice("scribe_v2", "scribe_v2"),
    ModelChoice("scribe_v2_medical", "scribe_v2_medical"),
]

_cache: dict[str, tuple[float, list[ModelChoice]]] = {}


def invalidate(provider: str) -> None:
    """Drops one provider's cached list ("anthropic", "groq" or
    "elevenlabs") - call after saving that provider's API key."""
    _cache.pop(provider, None)


def _api_key(creds: dict | None, provider: str) -> str:
    if not creds or not creds.get("api_key"):
        # Caught by _get() as "no key yet", same as clients.py raising.
        raise ValueError(f"No {provider} API key configured")
    return creds["api_key"]


def _claude_supports_effort_heuristic(model_id: str) -> bool:
    # Haiku 400s outright on output_config.effort - only used when the
    # Models API didn't report capabilities for a model.
    return "haiku" not in model_id.lower()


async def _fetch_anthropic() -> list[ModelChoice]:
    from anthropic import AsyncAnthropic

    choices = []
    async with AsyncAnthropic(api_key=_api_key(storage.get_ai_credentials(), "Anthropic")) as client:
        async for model in client.models.list(limit=100):
            caps = model.capabilities
            # AI replies and Knowledge Base ingestion both call
            # messages.parse(output_format=...), so a model without
            # structured outputs would just error.
            if caps is not None and not caps.structured_outputs.supported:
                continue
            supports_effort = caps.effort.supported if caps is not None else _claude_supports_effort_heuristic(model.id)
            choices.append(ModelChoice(model.id, model.display_name or model.id, supports_effort))
    return choices


async def _fetch_groq() -> list[ModelChoice]:
    from groq import AsyncGroq

    async with AsyncGroq(api_key=_api_key(storage.get_groq_credentials(), "Groq")) as client:
        response = await client.models.list()
    choices = [
        ModelChoice(model.id, model.id)
        for model in response.data
        if "whisper" in model.id.lower() and getattr(model, "active", True) is not False
    ]
    return sorted(choices, key=lambda c: c.value)


async def _fetch_elevenlabs() -> list[ModelChoice]:
    import httpx

    api_key = _api_key(storage.get_elevenlabs_credentials(), "ElevenLabs")
    async with httpx.AsyncClient(base_url="https://api.elevenlabs.io", headers={"xi-api-key": api_key}) as client:
        response = await client.get("/v1/models")
    response.raise_for_status()
    return [
        ModelChoice(m["model_id"], m.get("name") or m["model_id"])
        for m in response.json()
        if str(m.get("model_id", "")).startswith("scribe")
    ]


_FETCHERS = {
    "anthropic": (_fetch_anthropic, FALLBACK_CLAUDE_MODELS),
    "groq": (_fetch_groq, FALLBACK_WHISPER_MODELS),
    "elevenlabs": (_fetch_elevenlabs, FALLBACK_SCRIBE_MODELS),
}


async def _get(provider: str) -> list[ModelChoice]:
    cached = _cache.get(provider)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]
    fetch, fallback = _FETCHERS[provider]
    try:
        choices = await asyncio.wait_for(fetch(), timeout=_FETCH_TIMEOUT_SECONDS)
    except ValueError:
        # No key on file for this provider yet - expected, not worth a warning.
        return fallback
    except Exception as exc:
        logger.warning("Couldn't list %s models, using fallback list: %r", provider, exc)
        return fallback
    if not choices:
        # e.g. ElevenLabs not (yet) listing its Scribe models on /v1/models.
        return fallback
    _cache[provider] = (time.monotonic(), choices)
    return choices


def _with_saved(choices: list[ModelChoice], *saved: str | None, claude: bool = False) -> list[ModelChoice]:
    known = {c.value for c in choices}
    extra = []
    for value in saved:
        if value and value not in known:
            known.add(value)
            effort = _claude_supports_effort_heuristic(value) if claude else True
            extra.append(ModelChoice(value, f"{value} (currently saved)", effort))
    return choices + extra


async def claude_models(*saved: str | None) -> list[ModelChoice]:
    return _with_saved(await _get("anthropic"), *saved, claude=True)


async def whisper_models(*saved: str | None) -> list[ModelChoice]:
    return _with_saved(await _get("groq"), *saved)


async def scribe_models(*saved: str | None) -> list[ModelChoice]:
    return _with_saved(await _get("elevenlabs"), *saved)


def model_supports_effort(model: str | None) -> bool:
    """Whether output_config.effort may be sent for a Claude model - some
    (e.g. Haiku) 400 outright if it's sent at all. Prefers the capability
    the Anthropic Models API reported on the last successful listing,
    falling back to a "not Haiku" substring match for models the catalogue
    hasn't seen (not fetched yet since the last restart, or a saved alias
    the API lists under a dated id). Shared by services/ai_reply.py and
    services/voice_transcription_reply.py."""
    if not model:
        return False
    cached = _cache.get("anthropic")
    if cached:
        for choice in cached[1]:
            if choice.value == model:
                return choice.supports_effort
    return _claude_supports_effort_heuristic(model)
