"""Background download of inbound WhatsApp Inbox media (images/audio/
video/documents) referenced by a webhook event's media id.

Runs as a FastAPI BackgroundTask after the webhook has already ack'd Meta
with a fast 2xx - Meta's media URLs are short-lived and require the same
Bearer token as every other Graph API call, so this can't be deferred to
a lazy fetch at Inbox page-view time.
"""
import mimetypes

import httpx

from autosend import storage
from autosend.storage.inbox_media import media_path_for
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"

# Meta doesn't always give a useful extension via mimetypes.guess_extension
# for every WhatsApp-native format (e.g. voice notes are audio/ogg, which
# guess_extension often can't resolve in a minimal container image) -
# explicit map for the types actually reachable via an inbound WhatsApp
# message, falling back to mimetypes/"bin" for anything else.
_EXTENSION_OVERRIDES = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/amr": "amr",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "application/pdf": "pdf",
}


def _extension_for(mime_type: str | None) -> str:
    if not mime_type:
        return "bin"
    base_type = mime_type.split(";")[0].strip()
    if base_type in _EXTENSION_OVERRIDES:
        return _EXTENSION_OVERRIDES[base_type]
    guessed = mimetypes.guess_extension(base_type)
    return guessed.lstrip(".") if guessed else "bin"


async def download_and_store_media(message_id: int, media_id: str, access_token: str) -> None:
    """Meta's two-step media fetch: GET /{media-id} for a short-lived
    signed URL + metadata, then GET that URL with the same bearer token
    for the actual bytes."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            meta_response = await client.get(f"{GRAPH_BASE}/{media_id}", headers=headers)
            meta_response.raise_for_status()
            meta = meta_response.json()

            file_response = await client.get(meta["url"], headers=headers)
            file_response.raise_for_status()
    except Exception as exc:
        logger.warning(
            "Failed to download Inbox media %s for message %s: %s", media_id, message_id, exc,
        )
        storage.update_message_media_download(message_id, status="failed", error=str(exc))
        return

    mime_type = meta.get("mime_type")
    path = media_path_for(message_id, _extension_for(mime_type))
    path.write_bytes(file_response.content)

    storage.update_message_media_download(
        message_id,
        status="downloaded",
        local_path=str(path),
        sha256=meta.get("sha256"),
        file_size=meta.get("file_size") or len(file_response.content),
    )
