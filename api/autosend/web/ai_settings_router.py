"""Per-WhatsApp-number AI Assistant settings JSON API - bot persona/
handoff message/custom instructions, the AI/keyword activation toggles,
and the number's own exact-match auto-reply keyword rules.

Reuses numbers_router.py's _get_number_if_authorized for unit-scoped
access checks on a number, same precedent as campaigns_router.py per that
function's own docstring.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from autosend import storage
from autosend.web.auth import get_current_web_user
from autosend.web.numbers_router import _get_number_if_authorized

router = APIRouter(prefix="/api/ai-settings", tags=["ai-settings"])


def _require_module(number: dict, user: dict) -> None:
    if user["is_superadmin"]:
        return
    if not storage.is_enabled(number["org_id"], storage.MODULE_AI_ASSISTANT):
        raise HTTPException(status_code=403, detail="The AI Assistant module isn't enabled for this organisation")


@router.get("/{number_id}")
def api_get_ai_settings(number_id: int, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    _require_module(number, user)
    settings = storage.get_whatsapp_number_ai_settings(number_id) or {}
    return {
        "whatsapp_number_id": number_id,
        "label": number["label"],
        "ai_auto_reply_enabled": bool(number.get("ai_auto_reply_enabled")),
        "keyword_auto_reply_enabled": bool(number.get("keyword_auto_reply_enabled")),
        "bot_description": settings.get("bot_description"),
        "handoff_message": settings.get("handoff_message"),
        "custom_instructions": settings.get("custom_instructions"),
        "draft_review_enabled": bool(settings.get("draft_review_enabled")),
    }


class AISettingsIn(BaseModel):
    ai_auto_reply_enabled: bool
    keyword_auto_reply_enabled: bool
    bot_description: str | None = None
    handoff_message: str | None = None
    custom_instructions: str | None = None
    draft_review_enabled: bool = False


@router.post("/{number_id}")
def api_save_ai_settings(number_id: int, payload: AISettingsIn, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    _require_module(number, user)
    storage.set_whatsapp_number_ai_toggles(
        number_id, ai_auto_reply_enabled=payload.ai_auto_reply_enabled,
        keyword_auto_reply_enabled=payload.keyword_auto_reply_enabled,
    )
    storage.upsert_whatsapp_number_ai_settings(
        number_id, custom_instructions=payload.custom_instructions,
        bot_description=payload.bot_description, handoff_message=payload.handoff_message,
        draft_review_enabled=payload.draft_review_enabled,
    )
    return {"ok": True}


@router.get("/{number_id}/rules")
def api_list_rules(number_id: int, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    _require_module(number, user)
    return storage.list_ai_auto_reply_rules(number_id)


class RuleIn(BaseModel):
    keyword: str
    response_text: str
    active: bool = True


def _rule_or_404(number_id: int, rule_id: int) -> dict:
    rule = storage.get_ai_auto_reply_rule(rule_id)
    if not rule or rule["whatsapp_number_id"] != number_id:
        raise HTTPException(status_code=404, detail="Rule not found")
    return rule


@router.post("/{number_id}/rules")
def api_create_rule(number_id: int, payload: RuleIn, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    _require_module(number, user)
    if not payload.keyword.strip() or not payload.response_text.strip():
        raise HTTPException(status_code=400, detail="Keyword and response text are required")
    rule_id = storage.create_ai_auto_reply_rule(
        number_id, payload.keyword.strip(), payload.response_text.strip(), payload.active,
    )
    return {"id": rule_id}


@router.patch("/{number_id}/rules/{rule_id}")
def api_update_rule(number_id: int, rule_id: int, payload: RuleIn, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    _require_module(number, user)
    _rule_or_404(number_id, rule_id)
    if not payload.keyword.strip() or not payload.response_text.strip():
        raise HTTPException(status_code=400, detail="Keyword and response text are required")
    storage.update_ai_auto_reply_rule(
        rule_id, keyword=payload.keyword.strip(), response_text=payload.response_text.strip(), active=payload.active,
    )
    return {"id": rule_id}


@router.delete("/{number_id}/rules/{rule_id}")
def api_delete_rule(number_id: int, rule_id: int, user: dict = Depends(get_current_web_user)):
    number = _get_number_if_authorized(user, number_id)
    _require_module(number, user)
    _rule_or_404(number_id, rule_id)
    storage.delete_ai_auto_reply_rule(rule_id)
    return {"deleted": rule_id}
