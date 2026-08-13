"""Shared home for the local header-image files uploaded from the
Automations page (see web/automations_router.py's
POST /api/automations/header-image).

Lives in the storage package - rather than web/, where the upload endpoint
itself lives - so the upsert/delete functions in units.py and
serving.py can clean up orphaned files on edit/delete without a storage ->
web import (which would run backwards against the rest of the app and
risks a circular import back into this same package).

HEADER_IMAGES_DIR here is the single source of truth for where these files
live; main.py (the StaticFiles mount) and automations_router.py (the
upload endpoint) both import it from here rather than each computing
DB_PATH.parent / "header_images" independently, so there's no risk of the
two definitions drifting apart.
"""
import logging
from pathlib import Path
from urllib.parse import urlparse

from ._db import DB_PATH

logger = logging.getLogger(__name__)

HEADER_IMAGES_DIR = DB_PATH.parent / "header_images"

# Matches the mount path in main.py: app.mount("/media/header-images", ...).
# Used to recognize "this is a file we stored" vs. e.g. some other https
# URL that happened to end up in header_image_url - only ever the former
# in practice (the Automations UI only offers file upload, no manual URL
# paste), but checked defensively since delete_header_image_file() is
# reachable from several call sites and a bad delete is not recoverable.
_HEADER_IMAGE_URL_MARKER = "/media/header-images/"


def delete_header_image_file(url: str | None) -> None:
    """Best-effort delete of a previously-uploaded header image, given the
    URL that was stored in whatsapp_templates.header_image_url. Silently
    no-ops for None/blank, for any URL that isn't one of ours, or for a
    file that's already gone. Never raises - a failed filesystem cleanup
    should never block or roll back the DB write that's replacing or
    removing the reference to it."""
    if not url:
        return
    parsed_path = urlparse(url).path
    if _HEADER_IMAGE_URL_MARKER not in parsed_path:
        return
    filename = Path(parsed_path).name
    if not filename:
        return
    try:
        (HEADER_IMAGES_DIR / filename).unlink(missing_ok=True)
    except OSError:
        logger.warning("Failed to delete header image file %r", filename, exc_info=True)
