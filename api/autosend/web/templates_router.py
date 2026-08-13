"""API endpoints backing the WhatsApp Templates builder page.

Thin proxy to Meta's Graph API - deliberately keeps no local record of
templates (unlike Automations' whatsapp_templates table). Meta is the
single source of truth for template content and approval status; this
just gives staff a UI for create/list/delete instead of Meta's own
Business Manager UI.

Scoped by WhatsApp number only (not unit) since templates live on
the WABA, not on a unit - a number's `waba_id` is what Meta scopes
templates to. Access control still goes through the same
_accessible_numbers()/_get_number_if_authorized() used by campaigns_router
and automations_router, so a staff user can only manage templates for
numbers under their assigned unit(s).
"""
import logging
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from autosend.web.auth import get_current_web_user
from autosend.web.campaigns_router import _get_number_if_authorized

router = APIRouter()
logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"
ALLOWED_CATEGORIES = {"MARKETING", "UTILITY", "AUTHENTICATION"}
NAME_RE = re.compile(r"^[a-z0-9_]+$")
FOOTER_MAX_LEN = 60  # Meta's hard limit for the FOOTER component's text field

# Meta rejects (error_subcode 2388299, "Variables can't be at the start or
# end of the template") any BODY or HEADER text where a {{n}} placeholder is
# the first or last thing in the text - it must be wrapped in static text.
# Checked here so this surfaces as a clean 400 instead of round-tripping to
# Meta and coming back as a 502.
_LEADING_VAR_RE = re.compile(r"^\s*\{\{\s*\d+\s*\}\}")
_TRAILING_VAR_RE = re.compile(r"\{\{\s*\d+\s*\}\}\s*$")


def _check_no_edge_variables(text: str, field_label: str) -> None:
    if _LEADING_VAR_RE.search(text) or _TRAILING_VAR_RE.search(text):
        raise HTTPException(
            status_code=400,
            detail=f"{field_label} can't start or end with a variable like {{{{1}}}} - "
                   f"Meta requires static text around it, e.g. \"Hi {{{{1}}}}, ...\" "
                   f"rather than \"{{{{1}}}}, ...\" or \"... {{{{1}}}}\".",
        )


def _require_waba(number: dict) -> str:
    waba_id = number.get("waba_id")
    if not waba_id:
        raise HTTPException(
            status_code=400,
            detail="This WhatsApp number has no WABA ID on file. Add it in "
                   "SQLAdmin under WhatsApp Numbers first.",
        )
    return waba_id


# NOTE: template *listing* for a number intentionally lives only in
# numbers_router.py's GET /api/templates (used by dashboard.html,
# automations.html, and this page) - this module used to define its own
# duplicate of that same route, which FastAPI silently never reached and
# which nothing actually called. Removed rather than kept as a second
# source of truth. (This comment previously pointed at campaigns_router.py;
# the route moved to numbers_router.py in a later refactor.)


class ButtonIn(BaseModel):
    type: str  # "URL" | "PHONE_NUMBER" | "QUICK_REPLY"
    text: str
    url: str | None = None          # URL buttons only; may contain one {{1}}
    phone_number: str | None = None  # PHONE_NUMBER buttons only, E.164


class TemplateIn(BaseModel):
    number_id: int
    name: str
    category: str
    language: str = "en_US"
    header_type: str = "none"       # "none" | "text" | "image"
    header_text: str | None = None
    header_handle: str | None = None  # from /api/whatsapp-templates/header-upload
    body_text: str
    body_example_values: list[str] = []  # sample values, one per {{n}} in body_text
    footer_text: str | None = None
    buttons: list[ButtonIn] = []


class TemplateEditIn(BaseModel):
    """Same content fields as TemplateIn, minus name/language - Meta's edit
    endpoint (POST /{template_id}) can't change either on an existing
    template, only category and components."""
    number_id: int
    category: str
    header_type: str = "none"
    header_text: str | None = None
    header_handle: str | None = None  # a fresh upload is required if the
                                       # template has (or will have) an IMAGE
                                       # header - Meta doesn't hand back a
                                       # reusable handle for existing ones.
    body_text: str
    body_example_values: list[str] = []
    footer_text: str | None = None
    buttons: list[ButtonIn] = []


def _count_placeholders(text: str) -> int:
    nums = [int(n) for n in re.findall(r"\{\{\s*(\d+)\s*\}\}", text)]
    return max(nums) if nums else 0


def _validate_and_build_components(payload: TemplateIn | TemplateEditIn) -> list[dict]:
    """Shared by create and edit - both submit the same component shapes to
    Meta, just via different endpoints (create: POST .../message_templates,
    edit: POST /{template_id})."""
    if payload.category not in ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Category must be one of {sorted(ALLOWED_CATEGORIES)}")
    if payload.footer_text and len(payload.footer_text) > FOOTER_MAX_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"Footer must be {FOOTER_MAX_LEN} characters or fewer "
                   f"(got {len(payload.footer_text)}) - this is a WhatsApp/Meta limit",
        )

    components = []

    if payload.header_type == "text":
        if not payload.header_text:
            raise HTTPException(status_code=400, detail="Header text is required")
        _check_no_edge_variables(payload.header_text, "Header text")
        components.append({"type": "HEADER", "format": "TEXT", "text": payload.header_text})
    elif payload.header_type == "image":
        if not payload.header_handle:
            raise HTTPException(status_code=400, detail="Upload a header image first")
        components.append({
            "type": "HEADER", "format": "IMAGE",
            "example": {"header_handle": [payload.header_handle]},
        })

    body_component = {"type": "BODY", "text": payload.body_text}
    _check_no_edge_variables(payload.body_text, "Body text")
    placeholder_count = _count_placeholders(payload.body_text)
    if placeholder_count > 0:
        if len(payload.body_example_values) < placeholder_count:
            raise HTTPException(
                status_code=400,
                detail=f"Body uses {{{{1}}}}..{{{{{placeholder_count}}}}}; "
                       f"provide {placeholder_count} example value(s)",
            )
        body_component["example"] = {"body_text": [payload.body_example_values[:placeholder_count]]}
    components.append(body_component)

    if payload.footer_text:
        components.append({"type": "FOOTER", "text": payload.footer_text})

    if payload.buttons:
        button_objs = []
        for b in payload.buttons:
            if b.type == "URL":
                if not b.url:
                    raise HTTPException(status_code=400, detail="URL button requires a url")
                obj = {"type": "URL", "text": b.text, "url": b.url}
                if _count_placeholders(b.url) > 0:
                    obj["example"] = [b.url.replace("{{1}}", "example-value")]
                button_objs.append(obj)
            elif b.type == "PHONE_NUMBER":
                if not b.phone_number:
                    raise HTTPException(status_code=400, detail="Phone button requires a phone_number")
                button_objs.append({"type": "PHONE_NUMBER", "text": b.text, "phone_number": b.phone_number})
            elif b.type == "QUICK_REPLY":
                button_objs.append({"type": "QUICK_REPLY", "text": b.text})
            else:
                raise HTTPException(status_code=400, detail=f"Unknown button type {b.type}")
        components.append({"type": "BUTTONS", "buttons": button_objs})

    return components


@router.post("/api/whatsapp-templates")
async def api_create_template(payload: TemplateIn, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, payload.number_id)
    waba_id = _require_waba(number)

    if not NAME_RE.match(payload.name):
        raise HTTPException(status_code=400, detail="Name must be lowercase letters, numbers, and underscores only")

    components = _validate_and_build_components(payload)

    graph_payload = {
        "name": payload.name,
        "language": payload.language,
        "category": payload.category,
        "components": components,
    }

    async with httpx.AsyncClient(base_url=GRAPH_BASE, timeout=30) as client:
        response = await client.post(
            f"/{waba_id}/message_templates",
            headers={"Authorization": f"Bearer {number['access_token']}"},
            json=graph_payload,
        )
    if response.status_code >= 400:
        logger.error("Meta template create error %s: %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail=response.json().get("error", {}).get("message", response.text))
    return response.json()


@router.patch("/api/whatsapp-templates/{template_id}")
async def api_edit_template(template_id: str, payload: TemplateEditIn, user: dict = Depends(get_current_web_user)):
    """Edits an existing template's content. Uses Meta's numeric template
    `id` (surfaced via GET /api/templates), not the name - name and
    language can't be changed here, only category and components.

    Meta re-reviews the template after any content edit, so status drops
    back to PENDING. Rate-limited by Meta: an APPROVED template can be
    edited up to 10 times per rolling 30 days or once per 24 hours;
    REJECTED/PAUSED templates have no such limit. A 24h/30d rate-limit hit
    surfaces as a 502 with Meta's own error message below.
    """
    number = _get_number_if_authorized(user, payload.number_id)
    _require_waba(number)  # sanity check - numbers without a WABA never see templates to edit in the first place

    components = _validate_and_build_components(payload)
    graph_payload = {"category": payload.category, "components": components}

    async with httpx.AsyncClient(base_url=GRAPH_BASE, timeout=30) as client:
        response = await client.post(
            f"/{template_id}",
            headers={"Authorization": f"Bearer {number['access_token']}"},
            json=graph_payload,
        )
    if response.status_code >= 400:
        logger.error("Meta template edit error %s: %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail=response.json().get("error", {}).get("message", response.text))
    return response.json()


@router.delete("/api/whatsapp-templates/{template_name}")
async def api_delete_template(template_name: str, number_id: int, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    waba_id = _require_waba(number)
    async with httpx.AsyncClient(base_url=GRAPH_BASE, timeout=30) as client:
        response = await client.delete(
            f"/{waba_id}/message_templates",
            headers={"Authorization": f"Bearer {number['access_token']}"},
            params={"name": template_name},
        )
    if response.status_code >= 400:
        logger.error("Meta template delete error %s: %s", response.status_code, response.text)
        raise HTTPException(status_code=502, detail=response.json().get("error", {}).get("message", response.text))
    return {"deleted": template_name}


@router.post("/api/whatsapp-templates/header-upload")
async def api_upload_header_media(number_id: int, file: UploadFile, user: dict = Depends(get_current_web_user)):
    """Resumable Upload API flow, required for IMAGE header `example`
    handles (a template-creation-time concept, distinct from the
    send-time header_image_url used by Automations/campaigns). Needs
    meta_app_id, which most existing WhatsAppNumber rows won't have yet -
    add it in SQLAdmin under WhatsApp Numbers if this 400s."""
    number = _get_number_if_authorized(user, number_id)
    app_id = number.get("meta_app_id")
    if not app_id:
        raise HTTPException(
            status_code=400,
            detail="This WhatsApp number has no Meta App ID on file, which "
                   "image headers require. Add it in SQLAdmin under WhatsApp "
                   "Numbers, or use a text header instead.",
        )
    data = await file.read()
    content_type = file.content_type or "image/jpeg"

    async with httpx.AsyncClient(base_url=GRAPH_BASE, timeout=60) as client:
        session_resp = await client.post(
            f"/{app_id}/uploads",
            headers={"Authorization": f"OAuth {number['access_token']}"},
            params={
                "file_name": file.filename or "header.jpg",
                "file_length": len(data),
                "file_type": content_type,
            },
        )
        if session_resp.status_code >= 400:
            logger.error("Meta upload session error %s: %s", session_resp.status_code, session_resp.text)
            raise HTTPException(status_code=502, detail="Failed to start upload session: " + session_resp.text)
        upload_id = session_resp.json()["id"]  # e.g. "upload:XYZ"

        append_resp = await client.post(
            f"/{upload_id}",
            headers={
                "Authorization": f"OAuth {number['access_token']}",
                "file_offset": "0",
            },
            content=data,
        )
        if append_resp.status_code >= 400:
            logger.error("Meta upload append error %s: %s", append_resp.status_code, append_resp.text)
            raise HTTPException(status_code=502, detail="Failed to upload file: " + append_resp.text)

    return {"header_handle": append_resp.json()["h"]}
