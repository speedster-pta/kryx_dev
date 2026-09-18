"""Knowledge Base JSON API for the AI Assistant module.

Org-scoped (knowledge_base_entries.org_id), with an optional unit_id
narrowing (NULL = "org-wide", shared across every one of that org's
units) - see storage/knowledge_base.py's own docstring for why a NULL
unit_id must never become a cross-org "global" concept the way the
single-tenant parent project's did.

Managing an org-wide (unit_id=None) entry requires org-admin/superadmin -
the multi-tenant equivalent of the parent's "only a superadmin can touch
global entries" rule (plain staff there is roughly this app's "staff
scoped to one unit", and org-admin is roughly its "admin of everything
this org owns").
"""
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel

from autosend import storage
from autosend.services.knowledge_ingest import IngestError, ingest_pdf, save_manual_qa, scrape_url
from autosend.web.auth import ai_assistant_module_visible, get_current_web_user

router = APIRouter(prefix="/api/knowledge", tags=["knowledge-base"])


def _resolve_org_id(request_org_id: int | None, user: dict, query_org_id: str | None) -> int:
    """A superadmin can target any org via ?org_id= (they have no owning
    org of their own); everyone else is pinned to their own session
    org_id regardless of any query param - never trust a client-supplied
    org_id past this point for a non-superadmin."""
    if user["is_superadmin"]:
        if not query_org_id:
            raise HTTPException(status_code=400, detail="org_id query parameter is required for superadmin")
        return int(query_org_id)
    if request_org_id is None:
        raise HTTPException(status_code=403, detail="No organisation on this session")
    return request_org_id


def _require_module(org_id: int, user: dict) -> None:
    if user["is_superadmin"]:
        return
    if not storage.is_enabled(org_id, storage.MODULE_AI_ASSISTANT):
        raise HTTPException(status_code=403, detail="The AI Assistant module isn't enabled for this organisation")


def _check_unit_scope(org_id: int, unit_id: int | None, user: dict, *, require_admin_for_org_wide: bool) -> None:
    if user["is_superadmin"]:
        return
    # Checked before anything else: this org_id usually comes straight from
    # _resolve_org_id (already pinned to the caller's own org for a
    # non-superadmin) but the entry_id-keyed endpoints (update/delete) look
    # it up from the entry row itself, which a guessed/attacker-controlled
    # entry_id could point at another org entirely - is_org_admin alone is
    # NOT enough to prove that here, since it says nothing about *which*
    # org the caller administers.
    if user["org_id"] != org_id:
        raise HTTPException(status_code=404, detail="Knowledge base entry not found")
    if unit_id is None:
        if require_admin_for_org_wide and not user["is_org_admin"]:
            raise HTTPException(
                status_code=403,
                detail="Only an org admin can manage org-wide (all-units) knowledge base entries",
            )
        return
    accessible_unit_ids = storage.get_unit_ids_for_org(org_id) if user["is_org_admin"] else user["unit_ids"]
    if unit_id not in accessible_unit_ids:
        raise HTTPException(status_code=403, detail="You do not have access to this unit")


def _entry_org_id_or_404(entry_id: int) -> dict:
    entry = storage.get_knowledge_base_entry(entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Knowledge base entry not found")
    return entry


@router.get("/entries")
def api_list_entries(unit_id: int | None = None, org_id: str | None = None, user: dict = Depends(get_current_web_user)):
    resolved_org_id = _resolve_org_id(user["org_id"], user, org_id)
    _require_module(resolved_org_id, user)
    if unit_id is not None:
        _check_unit_scope(resolved_org_id, unit_id, user, require_admin_for_org_wide=False)
    return storage.list_knowledge_base_entries(resolved_org_id, unit_id=unit_id)


class ManualEntryIn(BaseModel):
    unit_id: int | None = None
    title: str
    content: str


@router.post("/entries/manual")
def api_create_manual_entry(payload: ManualEntryIn, org_id: str | None = None, user: dict = Depends(get_current_web_user)):
    resolved_org_id = _resolve_org_id(user["org_id"], user, org_id)
    _require_module(resolved_org_id, user)
    _check_unit_scope(resolved_org_id, payload.unit_id, user, require_admin_for_org_wide=True)
    if not payload.title.strip() or not payload.content.strip():
        raise HTTPException(status_code=400, detail="Title and content are required")
    entry_id = save_manual_qa(resolved_org_id, payload.unit_id, payload.title.strip(), payload.content.strip())
    return {"id": entry_id}


class UrlEntryIn(BaseModel):
    unit_id: int | None = None
    url: str


@router.post("/entries/url")
async def api_ingest_url(payload: UrlEntryIn, org_id: str | None = None, user: dict = Depends(get_current_web_user)):
    resolved_org_id = _resolve_org_id(user["org_id"], user, org_id)
    _require_module(resolved_org_id, user)
    _check_unit_scope(resolved_org_id, payload.unit_id, user, require_admin_for_org_wide=True)
    if not payload.url.strip().lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="A valid http(s) URL is required")
    try:
        entry_ids = await scrape_url(resolved_org_id, payload.unit_id, payload.url.strip())
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"entry_ids": entry_ids}


@router.post("/entries/pdf")
async def api_ingest_pdf(
    file: UploadFile, unit_id: int | None = None, org_id: str | None = None,
    user: dict = Depends(get_current_web_user),
):
    resolved_org_id = _resolve_org_id(user["org_id"], user, org_id)
    _require_module(resolved_org_id, user)
    _check_unit_scope(resolved_org_id, unit_id, user, require_admin_for_org_wide=True)
    file_bytes = await file.read()
    try:
        entry_ids = await ingest_pdf(resolved_org_id, unit_id, file.filename or "upload.pdf", file_bytes)
    except IngestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"entry_ids": entry_ids}


class EntryUpdateIn(BaseModel):
    title: str
    content: str
    is_active: bool = True


@router.patch("/entries/{entry_id}")
def api_update_entry(entry_id: int, payload: EntryUpdateIn, user: dict = Depends(get_current_web_user)):
    entry = _entry_org_id_or_404(entry_id)
    _require_module(entry["org_id"], user)
    _check_unit_scope(entry["org_id"], entry["unit_id"], user, require_admin_for_org_wide=True)
    if not payload.title.strip() or not payload.content.strip():
        raise HTTPException(status_code=400, detail="Title and content are required")
    storage.update_knowledge_base_entry(entry_id, title=payload.title.strip(), content=payload.content.strip(), is_active=payload.is_active)
    return {"id": entry_id}


@router.delete("/entries/{entry_id}")
def api_delete_entry(entry_id: int, user: dict = Depends(get_current_web_user)):
    entry = _entry_org_id_or_404(entry_id)
    _require_module(entry["org_id"], user)
    _check_unit_scope(entry["org_id"], entry["unit_id"], user, require_admin_for_org_wide=True)
    storage.delete_knowledge_base_entry(entry_id)
    return {"deleted": entry_id}
