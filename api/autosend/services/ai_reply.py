"""AI Assistant RAG auto-reply for the WhatsApp Inbox.

maybe_generate_ai_reply() is the single entry point, called after every
inbound Inbox message is recorded (integrations/webhooks.py for text/
media, services/audio_transcription.py once a voice note's transcript is
ready). Gating order matters:

  1. Keyword auto-replies (storage/ai_auto_reply_rules.py) - fully
     independent of the AI Assistant module, checked first, gated only on
     the number's own keyword_auto_reply_enabled toggle.
  2. storage.MODULE_AI_ASSISTANT enabled for the org.
  3. The number's own ai_auto_reply_enabled toggle.
  4. conversations.ai_status == 'active' (not 'paused' by a staff member
     or already 'escalated').
  5. A daily reply cap per number (cost/runaway-loop safety net).

No fallback model name is ever used anywhere below - generate_ai_response
raises a clear error if ai_credentials/its model isn't configured yet,
and maybe_generate_ai_reply just logs and gives up on that message rather
than guessing a model.
"""
from __future__ import annotations

from pydantic import BaseModel

from autosend import clients, storage
from autosend.config import settings
from autosend.integrations.whatsapp import MessagingLimitExceeded, WhatsAppSendError
from autosend.utils.logging import get_logger

logger = get_logger(__name__)

# Fixed, platform-wide fallback texts - used when a number hasn't set its
# own whatsapp_number_ai_settings.handoff_message, or (for the opt-out
# acknowledgement) always, since that one isn't per-number customisable.
HANDOFF_MESSAGE = "Thanks for reaching out! I'll get one of our team members to help you with that shortly."
UNSUBSCRIBE_MESSAGE = (
    "We've noted your request to stop receiving automated replies. A team member will "
    "action this - feel free to keep messaging this number in the meantime."
)

_DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant answering WhatsApp messages on behalf of the "
    "organisation below. Use the provided knowledge base context to answer "
    "accurately and warmly. If the context doesn't cover the question, say so "
    "honestly rather than guessing, and set escalate to true."
)

# Recent conversation history included as context for the AI call -
# messages older than this are dropped even if the conversation itself is
# still open, so a long-dormant thread that suddenly gets a new message
# doesn't drag months of stale context into every reply.
HISTORY_MAX_AGE_DAYS = 7
HISTORY_MAX_MESSAGES = 20


class AIReplyOutput(BaseModel):
    reply: str
    escalate: bool = False
    confidence: float = 1.0
    opt_out: bool = False


def _model_supports_effort(model: str) -> bool:
    # Haiku models 400 if output_config.effort is sent at all.
    return "haiku" not in model.lower()


def _build_system_prompt(credentials: dict, number_settings: dict) -> str:
    """Layering order: bot_description (this number's persona) -> base
    system prompt -> platform-wide custom_instructions -> this number's
    own custom_instructions, each appended after the last."""
    parts = []
    if number_settings.get("bot_description"):
        parts.append(number_settings["bot_description"])
    parts.append(credentials.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT)
    if credentials.get("custom_instructions"):
        parts.append(credentials["custom_instructions"])
    if number_settings.get("custom_instructions"):
        parts.append(number_settings["custom_instructions"])
    return "\n\n".join(parts)


def _recent_history(conversation_id: int) -> list[dict]:
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_MAX_AGE_DAYS)).isoformat()
    messages = [
        m for m in storage.list_messages(conversation_id, limit=200)
        if m["created_at"] >= cutoff and (m.get("body") or "").strip()
    ][-HISTORY_MAX_MESSAGES:]

    history: list[dict] = []
    for m in messages:
        role = "user" if m["direction"] == "in" else "assistant"
        if history and history[-1]["role"] == role:
            # The Messages API expects strictly alternating turns -
            # merge consecutive same-role messages rather than send an
            # invalid sequence.
            history[-1]["content"] += f"\n{m['body']}"
        else:
            history.append({"role": role, "content": m["body"]})
    return history


async def generate_ai_response(
    *, org_id: int, unit_id: int, whatsapp_number_id: int, conversation_id: int, message_text: str,
) -> dict:
    """Retrieval + Claude call. Raises ValueError if AI credentials/model
    aren't configured - callers must not catch this to silently fall back
    to a default model, only to skip this message and log clearly."""
    credentials = storage.get_ai_credentials()
    model = credentials.get("model") if credentials else None
    if not credentials or not credentials.get("api_key") or not model:
        raise ValueError("AI credentials aren't configured yet (Anthropic API key/model)")

    entries = storage.search_knowledge_base_entries(org_id, unit_id, message_text, limit=5)
    if entries:
        context = "\n\n".join(f"- {e['title']}: {e['content']}" for e in entries)
    else:
        context = "No knowledge base entries matched this question."

    number_settings = storage.get_whatsapp_number_ai_settings(whatsapp_number_id) or {}
    system_prompt = _build_system_prompt(credentials, number_settings)
    system_prompt += f"\n\nRelevant knowledge base context:\n{context}"

    messages = _recent_history(conversation_id) + [{"role": "user", "content": message_text}]

    if settings.dry_run:
        # Same "simulation mode" intent as WhatsAppClient's dry_run handling
        # (integrations/whatsapp.py) - a local/dev run shouldn't spend real
        # Anthropic credits on every test webhook trigger.
        logger.info(
            "[SIMULATION MODE / DRY RUN] Intercepted AI reply generation for conversation %s. Message: %r",
            conversation_id, message_text,
        )
        return {
            "output": AIReplyOutput(reply="[SIMULATED] This is a dry-run AI reply."),
            "entry_ids": [e["id"] for e in entries],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "model": model,
        }

    client = clients.get_anthropic_client()
    call_kwargs = {}
    effort = credentials.get("effort")
    if effort and _model_supports_effort(model):
        call_kwargs["output_config"] = {"effort": effort}

    response = await client.messages.parse(
        model=model,
        max_tokens=1024,
        system=system_prompt,
        messages=messages,
        output_format=AIReplyOutput,
        **call_kwargs,
    )
    output = response.parsed_output
    if output is None:
        raise ValueError("AI did not return a structured reply")

    return {
        "output": output,
        "entry_ids": [e["id"] for e in entries],
        "prompt_tokens": response.usage.input_tokens,
        "completion_tokens": response.usage.output_tokens,
        "model": model,
    }


async def _deliver_reply(conversation: dict, number: dict, text: str, *, sender_type: str) -> bool:
    """Sends `text` as a free-text WhatsApp message. Automated replies
    can only use free text (never a template), so this fails cleanly - no
    send attempted - if the 24h session window is already closed."""
    if not storage.is_session_window_open(conversation):
        logger.warning(
            "Skipping automated reply for conversation %s - WhatsApp session window is closed",
            conversation["id"],
        )
        storage.record_outbound_message(
            conversation["id"], sender_type=sender_type, message_type="text", body=text,
            status="failed", error_message="WhatsApp 24h session window is closed",
        )
        return False

    client = clients.get_whatsapp_client_for_number(number)
    try:
        result = await client.send_text(conversation["contact_wa_id"], text)
    except (MessagingLimitExceeded, WhatsAppSendError) as exc:
        storage.record_outbound_message(
            conversation["id"], sender_type=sender_type, message_type="text", body=text,
            status="failed", error_message=str(exc),
        )
        return False

    wamid = (result.get("messages") or [{}])[0].get("id")
    storage.record_outbound_message(
        conversation["id"], sender_type=sender_type, message_type="text", body=text,
        wamid=wamid, status="sent",
    )
    return True


async def maybe_generate_ai_reply(conversation_id: int, inbound_message_id: int) -> None:
    conversation = storage.get_conversation(conversation_id)
    if not conversation:
        return
    number = storage.get_whatsapp_number_by_id(conversation["whatsapp_number_id"])
    if not number or not number["active"]:
        return
    message = storage.get_message_with_unit(inbound_message_id)
    if not message:
        return
    text = (message.get("body") or "").strip()
    if not text:
        return

    if number.get("keyword_auto_reply_enabled"):
        rule = storage.find_matching_ai_auto_reply_rule(number["id"], text)
        if rule:
            sent = await _deliver_reply(conversation, number, rule["response_text"], sender_type="ai")
            storage.record_ai_reply(
                whatsapp_number_id=number["id"], conversation_id=conversation_id,
                inbound_message_id=inbound_message_id, source="keyword", sent=sent,
            )
            return

    if not storage.is_enabled(number["org_id"], storage.MODULE_AI_ASSISTANT):
        return
    if not number.get("ai_auto_reply_enabled"):
        return
    if conversation.get("ai_status") != "active":
        return
    if storage.count_ai_replies_today(number["id"]) >= settings.ai_daily_reply_cap_per_number:
        logger.warning("AI Assistant daily reply cap reached for number %s - skipping", number["id"])
        return

    try:
        result = await generate_ai_response(
            org_id=number["org_id"], unit_id=number["unit_id"], whatsapp_number_id=number["id"],
            conversation_id=conversation_id, message_text=text,
        )
    except ValueError as exc:
        logger.error("Cannot generate AI reply for conversation %s: %s", conversation_id, exc)
        return
    except Exception:
        logger.exception("AI reply generation failed for conversation %s", conversation_id)
        return

    output: AIReplyOutput = result["output"]
    escalated = bool(output.escalate or output.opt_out)

    if output.opt_out:
        reply_text = UNSUBSCRIBE_MESSAGE
    elif output.escalate:
        number_settings = storage.get_whatsapp_number_ai_settings(number["id"]) or {}
        reply_text = number_settings.get("handoff_message") or HANDOFF_MESSAGE
    else:
        reply_text = output.reply

    sent = await _deliver_reply(conversation, number, reply_text, sender_type="ai")

    if escalated:
        storage.set_conversation_ai_status(conversation_id, "escalated")

    storage.record_ai_reply(
        whatsapp_number_id=number["id"], conversation_id=conversation_id,
        inbound_message_id=inbound_message_id, source="live", sent=sent, escalated=escalated,
        retrieved_entry_ids=result["entry_ids"], prompt_tokens=result["prompt_tokens"],
        completion_tokens=result["completion_tokens"], model=result["model"],
    )
