"""Tests for services/knowledge_ingest.py's AI ingestion pass, ported over
from a bug found on the sibling single-tenant project (shofar-cloud's
dev-whatsapp/api/shofar_automation/services/knowledge_ingest.py): a
truncated JSON response from the ingestion model (most commonly caused by
a source document producing more FAQ content than max_tokens allows)
raised a raw, unhandled pydantic.ValidationError that propagated past
web/knowledge_router.py's `except IngestError` handling and 500'd the
request instead of showing the staff member a clean message.

No async test plugin (pytest-asyncio/anyio) is installed in this repo -
driven via asyncio.run() in plain sync test functions, same as
test_ai_reply_safety_nets.py. clients/storage calls are monkeypatched
directly on knowledge_ingest's own module references (it does
`from autosend import clients, storage`), so nothing here touches a real
DB or makes a real Anthropic API call."""
import asyncio

import pytest

from autosend.services import knowledge_ingest


class _TruncatingMessages:
    """Stands in for AsyncAnthropic().messages - .parse() raises a real
    pydantic.ValidationError, the same way the SDK's own JSON-mode parsing
    fails when the model's output got cut off mid-string before it formed
    valid JSON matching the requested output_format."""

    async def parse(self, **kwargs):
        knowledge_ingest._FAQPairs.model_validate({"pairs": "not-a-list"})


class _TruncatingAnthropicClient:
    def __init__(self):
        self.messages = _TruncatingMessages()


def test_generate_faq_pairs_converts_truncated_json_to_ingest_error(monkeypatch):
    monkeypatch.setattr(
        knowledge_ingest.storage, "get_ai_ingestion_settings",
        lambda: {"api_key": "fake-key", "model": "claude-x"},
    )
    monkeypatch.setattr(
        knowledge_ingest.clients, "get_ai_ingestion_client", lambda: _TruncatingAnthropicClient(),
    )

    def _fail_if_called(**kwargs):
        raise AssertionError("record_ai_ingestion should not run once parsing has failed")

    monkeypatch.setattr(knowledge_ingest.storage, "record_ai_ingestion", _fail_if_called)

    with pytest.raises(knowledge_ingest.IngestError) as exc_info:
        asyncio.run(
            knowledge_ingest._generate_faq_pairs(
                "some long source text", "Some Document",
                org_id=1, unit_id=1, source_type="pdf", source_ref="doc.pdf",
            )
        )

    assert "more FAQ content than a single pass can handle" in str(exc_info.value)
