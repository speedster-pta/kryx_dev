"""Local storage for WhatsApp Inbox media (images/audio/video/documents
downloaded from Meta after an inbound message).

Deliberately NOT mounted as public StaticFiles like storage/
header_images.py's header images - conversation media is private, and is
only ever served through web/conversations_router.py's authenticated
media endpoint, which re-checks the requesting user's unit access before
streaming bytes.

INBOX_MEDIA_DIR is the single source of truth for where these files live,
same pattern as HEADER_IMAGES_DIR in header_images.py.
"""
from pathlib import Path

from ._db import DB_PATH

INBOX_MEDIA_DIR = DB_PATH.parent / "inbox_media"


def media_path_for(message_id: int, extension: str) -> Path:
    """One file per message, named by the message's own local id (not
    Meta's media_id, which isn't guaranteed filesystem-safe) so serving a
    download is a simple id-based lookup rather than needing the filename
    stored anywhere beyond media_local_path."""
    INBOX_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    return INBOX_MEDIA_DIR / f"{message_id}.{extension.lstrip('.')}"
