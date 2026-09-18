"""Port of a fix from the sibling single-tenant project (shofar-cloud's
dev-whatsapp/api/shofar_automation/integrations/whatsapp.py): send_text
(freeform replies inside an open 24h customer-service session, used by
the Inbox reply router and the AI reply pipeline) must never count
against the WABA's business-initiated messaging cap. _post_messages now
takes a record_usage flag, defaulting to True for every template send,
with send_text passing record_usage=False so it skips _record() (and
therefore whatsapp_limits.record_send) entirely.

No async test plugin (pytest-asyncio/anyio) is installed in this repo -
driven via asyncio.run() in plain sync test functions, same as
test_registration_poller_ical.py.
"""
import asyncio

import autosend.config as config_module
from autosend.integrations.whatsapp import WhatsAppClient


async def _noop_gate():
    return None


def _make_client(monkeypatch, recorded):
    monkeypatch.setattr(config_module.settings, "dry_run", True)
    client = WhatsAppClient("fake-token", "fake-phone-number-id", number={"id": 1})
    monkeypatch.setattr(client, "_record", lambda to_phone: recorded.append(to_phone))
    monkeypatch.setattr(client, "_gate", _noop_gate)
    return client


def test_send_text_does_not_record_usage(monkeypatch):
    recorded = []
    client = _make_client(monkeypatch, recorded)

    asyncio.run(client.send_text("27821234567", "hello there"))

    assert recorded == []


def test_send_template_still_records_usage(monkeypatch):
    recorded = []
    client = _make_client(monkeypatch, recorded)

    asyncio.run(client.send_template("27821234567", "some_template", "Alex"))

    assert recorded == ["27821234567"]
