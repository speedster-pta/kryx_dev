"""AI Playground JSON API: lets staff test the AI RAG auto-reply's prompt/
retrieval/model behaviour against a unit's knowledge base without touching
a real conversation - calls the same services/ai_reply.py::
generate_ai_response() pure function the live Inbox webhook path uses.
Unit scoping (for access control) reuses web/numbers_router.py's
_accessible_units/_check_unit_access directly rather than a local copy
(per CLAUDE.md's "cross-importing a scoping helper from another router
module" precedent - see web/ai_settings_router.py reusing
_get_number_if_authorized). An optional whatsapp_number_id on top of the
chosen unit lets staff additionally preview one specific number's own AI
settings (bot description/custom instructions/handoff message, see
WhatsAppNumberAISettingsCrudView) - the knowledge base itself stays
unit(+org-wide) scoped either way, per generate_ai_response's own scoping.
"""
import anthropic
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from autosend import storage
from autosend.services.ai_reply import generate_ai_response
from autosend.web.auth import get_current_web_user
from autosend.web.numbers_router import _accessible_units, _check_unit_access

router = APIRouter()


@router.get("/api/ai/units")
def api_ai_units(user: dict = Depends(get_current_web_user)):
    return [{"id": u["id"], "name": u["name"]} for u in _accessible_units(user)]


class PlaygroundHistoryTurn(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class PlaygroundBody(BaseModel):
    unit_id: int
    # Optional: the knowledge base retrieved for the reply always stays
    # scoped to unit_id above (plus that unit's org-wide entries), but bot
    # description/custom instructions/handoff message are per-number (see
    # services/ai_reply.py::generate_ai_response) - omitting this just
    # previews the platform-wide settings with none of a specific
    # number's own layered on top.
    whatsapp_number_id: int | None = None
    message: str
    # Client-side-only history the browser accumulates across playground
    # turns - never read from or written to conversations/
    # conversation_messages, per this feature's whole point (a safe
    # sandbox to try prompts/knowledge-base changes against, with nothing
    # persisted as if it were a real contact thread).
    history: list[PlaygroundHistoryTurn] = []
    # Playground-only overrides of the live path's configured AI
    # Credentials model/effort, so staff can compare models/effort without
    # touching prod config - None means "use the AI Credentials setting"
    # (see generate_ai_response).
    model: str | None = None
    effort: str | None = None


@router.post("/api/ai/playground")
async def api_ai_playground(payload: PlaygroundBody, user: dict = Depends(get_current_web_user)):
    _check_unit_access(user, payload.unit_id)
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="A message is required")

    unit = next((u for u in _accessible_units(user) if u["id"] == payload.unit_id), None)
    if unit is None:
        raise HTTPException(status_code=403, detail="You do not have access to this unit")

    if payload.whatsapp_number_id is not None:
        number = storage.get_whatsapp_number_by_id(payload.whatsapp_number_id)
        if not number or number["unit_id"] != payload.unit_id:
            raise HTTPException(status_code=400, detail="That WhatsApp number does not belong to this unit")

    # Translate the playground's role/content shape into the
    # direction/body shape generate_ai_response's own history-building
    # expects internally - one internal message shape, so prompt-assembly
    # logic never has to know which caller supplied the history.
    history = [
        {"role": "assistant" if turn.role == "assistant" else "user", "content": turn.content}
        for turn in payload.history
    ]

    # Model/effort overrides are a superadmin-only control (the playground
    # UI hides the pickers for non-superadmins, but that's client-side
    # only - re-check here so a direct POST can't override what a
    # unit-scoped staff member is allowed to compare against).
    model = payload.model if user.get("is_superadmin") else None
    effort = payload.effort if user.get("is_superadmin") else None

    try:
        result = await generate_ai_response(
            org_id=unit["org_id"], unit_id=payload.unit_id, whatsapp_number_id=payload.whatsapp_number_id,
            message_text=payload.message, history=history, model=model, effort=effort,
        )
    except ValueError as exc:
        # generate_ai_response raises ValueError if no API key or no model
        # is configured - a config problem, not a bad playground request,
        # but still worth a clear 400 rather than a raw 500.
        raise HTTPException(status_code=400, detail=str(exc))
    except anthropic.RateLimitError:
        raise HTTPException(status_code=429, detail="Claude is rate-limiting this app right now - try again shortly.")
    except anthropic.APIStatusError as exc:
        raise HTTPException(status_code=502, detail=f"Claude API error: {exc.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(status_code=502, detail="Could not reach the Claude API. Check network connectivity.")

    output = result["output"]
    retrieved_entries = [
        e for e in (storage.get_knowledge_base_entry(eid) for eid in result["entry_ids"]) if e
    ]

    # Mirrors maybe_generate_ai_reply's forced escalate-on-opt_out - kept
    # here too so the playground's logged escalation state matches what
    # the live path would actually do for the same reply.
    escalate = output.escalate or output.opt_out

    storage.record_ai_reply(
        whatsapp_number_id=payload.whatsapp_number_id, conversation_id=None, inbound_message_id=None,
        source="playground", sent=False, escalated=escalate,
        retrieved_entry_ids=result["entry_ids"], prompt_tokens=result["prompt_tokens"],
        completion_tokens=result["completion_tokens"], model=result["model"],
    )

    return {
        "reply": output.reply,
        "escalate": escalate,
        "opt_out": output.opt_out,
        "confidence": output.confidence,
        # The model actually used - resolved by generate_ai_response
        # itself (explicit override > the platform-wide AI Credentials
        # setting), not guessed here, so this stays accurate regardless
        # of which one supplied it.
        "model": result["model"],
        "prompt_tokens": result["prompt_tokens"],
        "completion_tokens": result["completion_tokens"],
        "retrieved_entries": [
            {
                "id": e["id"], "title": e["title"], "content": e["content"],
                "source_type": e["source_type"], "source_ref": e["source_ref"],
            }
            for e in retrieved_entries
        ],
    }
