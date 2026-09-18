"""Tests for the two safety nets in services/ai_reply.py ported over from
the sibling single-tenant project (shofar-cloud's dev-whatsapp/api/
shofar_automation/services/ai_reply.py): generate_ai_response's dry_run
short-circuit (mirrors WhatsAppClient's own dry_run handling in
integrations/whatsapp.py - a local/dev run shouldn't spend real Anthropic
credits on every test webhook trigger) and maybe_generate_ai_reply's daily
reply cap per WhatsApp number (settings.ai_daily_reply_cap_per_number - a
runaway/prompt-injection-loop safety valve).

No async test plugin (pytest-asyncio/anyio) is installed in this repo -
driven via asyncio.run() in plain sync test functions, same as
test_whatsapp_client_usage_recording.py. Storage/clients calls are
monkeypatched directly on the ai_reply module's own references (it does
`from autosend import clients, storage`, so patching autosend.storage.X
and ai_reply.storage.X are the same object) rather than spinning up the DB
fixtures in conftest.py - nothing under test here reads or writes a real
row.
"""
import asyncio

import autosend.config as config_module
from autosend.services import ai_reply


class _RecordingLogger:
    """Stands in for ai_reply.logger so the cap test can assert a warning
    was logged without depending on caplog + logger propagation (get_logger
    sets propagate=False, so pytest's caplog wouldn't see it anyway)."""

    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args):
        self.warnings.append(msg % args if args else msg)

    def info(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


def test_generate_ai_response_dry_run_skips_anthropic_call(monkeypatch):
    monkeypatch.setattr(config_module.settings, "dry_run", True)
    monkeypatch.setattr(
        ai_reply.storage, "get_ai_credentials", lambda: {"api_key": "fake-key", "model": "claude-x"}
    )
    monkeypatch.setattr(ai_reply.storage, "search_knowledge_base_entries", lambda *a, **k: [])
    monkeypatch.setattr(ai_reply.storage, "get_whatsapp_number_ai_settings", lambda *a, **k: {})
    monkeypatch.setattr(ai_reply.storage, "list_messages", lambda *a, **k: [])

    def _fail_if_called():
        raise AssertionError("Anthropic client should never be constructed while settings.dry_run is True")

    monkeypatch.setattr(ai_reply.clients, "get_anthropic_client", _fail_if_called)

    result = asyncio.run(
        ai_reply.generate_ai_response(
            org_id=1, unit_id=1, whatsapp_number_id=1, conversation_id=1, message_text="hello",
        )
    )

    assert result["output"].reply == "[SIMULATED] This is a dry-run AI reply."
    assert result["prompt_tokens"] == 0
    assert result["completion_tokens"] == 0
    assert result["model"] == "claude-x"


def test_maybe_generate_ai_reply_suppresses_once_daily_cap_reached(monkeypatch):
    number = {
        "id": 1, "org_id": 1, "unit_id": 1, "active": True,
        "keyword_auto_reply_enabled": False, "ai_auto_reply_enabled": True,
    }
    conversation = {"id": 1, "whatsapp_number_id": 1, "ai_status": "active"}
    message = {"id": 1, "body": "hello there"}

    monkeypatch.setattr(ai_reply.storage, "get_conversation", lambda cid: conversation)
    monkeypatch.setattr(ai_reply.storage, "get_whatsapp_number_by_id", lambda nid: number)
    monkeypatch.setattr(ai_reply.storage, "get_message_with_unit", lambda mid: message)
    monkeypatch.setattr(ai_reply.storage, "is_enabled", lambda org_id, module_key: True)
    monkeypatch.setattr(config_module.settings, "ai_daily_reply_cap_per_number", 5)
    monkeypatch.setattr(ai_reply.storage, "count_ai_replies_today", lambda number_id: 5)

    def _fail_if_called(**kwargs):
        raise AssertionError("generate_ai_response should not be called once the daily cap is reached")

    monkeypatch.setattr(ai_reply, "generate_ai_response", _fail_if_called)
    recording_logger = _RecordingLogger()
    monkeypatch.setattr(ai_reply, "logger", recording_logger)

    asyncio.run(ai_reply.maybe_generate_ai_reply(conversation_id=1, inbound_message_id=1))

    assert any("daily reply cap reached" in w for w in recording_logger.warnings)
