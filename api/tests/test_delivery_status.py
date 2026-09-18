"""Delivery-status tracking for send_log (transactional automations) and
campaign_recipients (bulk campaigns) - see storage/message_status.py for the
shared should_apply_delivery_status ordering rule both tables use.

The key regression under test: Meta's WhatsApp status webhook can redeliver
an event, or deliver events out of order (e.g. a retried 'sent' webhook
arriving after a 'read' event already landed) - applying it naively would
downgrade an already-delivered/-read row back to an earlier state. Mirrors
the parent single-tenant project's equivalent coverage for
storage/message_status.py.
"""
from autosend import storage


def _make_send_log_row(unit_id: int, wamid: str) -> None:
    storage.record_send(
        unit_id=unit_id, source="registration_poller", status="sent",
        recipient_phone="+27821234567", template_name="free_acknowledgment",
        reference_id="reg-1", wamid=wamid,
    )


def _get_send_log_delivery(unit_id: int, wamid: str) -> dict:
    rows = storage.get_recent_sends(unit_ids=[unit_id], limit=10)
    return next(r for r in rows if r["wamid"] == wamid)


def _make_campaign_recipient(unit_id: int, number_id: int, wamid: str) -> tuple[int, int]:
    campaign_id = storage.create_campaign(
        user_id=1, unit_id=unit_id, whatsapp_number_id=number_id,
        template_name="blast_template", language="en", total=1,
    )
    recipient_id = storage.add_campaign_recipient(campaign_id, "+27821234567")
    storage.update_campaign_recipient(recipient_id, "sent", "wamid.abc", wamid=wamid)
    return campaign_id, recipient_id


def _get_campaign_recipient_delivery(campaign_id: int) -> dict:
    campaign = storage.get_campaign(campaign_id)
    return campaign["recipients"][0]


class TestSendLogDeliveryStatus:
    def test_wamid_and_status_captured_at_send_time(self, tenants):
        tenant_a, _tenant_b = tenants
        wamid = f"wamid.{tenant_a.unit_id}.1"
        _make_send_log_row(tenant_a.unit_id, wamid)

        row = _get_send_log_delivery(tenant_a.unit_id, wamid)
        assert row["wamid"] == wamid
        assert row["delivery_status"] is None

    def test_delivery_status_progresses_forward(self, tenants):
        tenant_a, _tenant_b = tenants
        wamid = f"wamid.{tenant_a.unit_id}.2"
        _make_send_log_row(tenant_a.unit_id, wamid)

        assert storage.update_send_log_delivery_status_by_wamid(wamid, "delivered", "2024-01-01T00:00:00+00:00")
        assert _get_send_log_delivery(tenant_a.unit_id, wamid)["delivery_status"] == "delivered"

        assert storage.update_send_log_delivery_status_by_wamid(wamid, "read", "2024-01-01T00:01:00+00:00")
        assert _get_send_log_delivery(tenant_a.unit_id, wamid)["delivery_status"] == "read"

    def test_out_of_order_sent_does_not_downgrade_read(self, tenants):
        """A retried/redelivered 'sent' webhook arriving after 'read' has
        already been recorded must not clobber the more advanced state -
        this is the exact scenario should_apply_delivery_status guards
        against."""
        tenant_a, _tenant_b = tenants
        wamid = f"wamid.{tenant_a.unit_id}.3"
        _make_send_log_row(tenant_a.unit_id, wamid)

        storage.update_send_log_delivery_status_by_wamid(wamid, "read", "2024-01-01T00:01:00+00:00")
        assert _get_send_log_delivery(tenant_a.unit_id, wamid)["delivery_status"] == "read"

        # Out-of-order redelivery of an earlier event.
        applied = storage.update_send_log_delivery_status_by_wamid(wamid, "sent", "2024-01-01T00:00:00+00:00")
        assert applied is True  # row was found - just not downgraded
        row = _get_send_log_delivery(tenant_a.unit_id, wamid)
        assert row["delivery_status"] == "read"
        assert row["delivery_updated_at"] == "2024-01-01T00:01:00+00:00"

    def test_failed_cannot_override_delivered(self, tenants):
        tenant_a, _tenant_b = tenants
        wamid = f"wamid.{tenant_a.unit_id}.4"
        _make_send_log_row(tenant_a.unit_id, wamid)

        storage.update_send_log_delivery_status_by_wamid(wamid, "delivered", "2024-01-01T00:00:00+00:00")
        storage.update_send_log_delivery_status_by_wamid(
            wamid, "failed", "2024-01-01T00:05:00+00:00", error_message="some later glitch",
        )
        row = _get_send_log_delivery(tenant_a.unit_id, wamid)
        assert row["delivery_status"] == "delivered"
        assert row["delivery_error_message"] is None

    def test_unmatched_wamid_returns_false(self, tenants):
        assert storage.update_send_log_delivery_status_by_wamid(
            "wamid.never-recorded", "delivered", "2024-01-01T00:00:00+00:00",
        ) is False


class TestCampaignRecipientDeliveryStatus:
    def test_wamid_captured_on_sent_recipient(self, tenants):
        tenant_a, _tenant_b = tenants
        wamid = f"wamid.campaign.{tenant_a.unit_id}.1"
        campaign_id, _recipient_id = _make_campaign_recipient(tenant_a.unit_id, tenant_a.number_id, wamid)

        recipient = _get_campaign_recipient_delivery(campaign_id)
        assert recipient["status"] == "sent"
        assert recipient["delivery_status"] is None

    def test_out_of_order_sent_does_not_downgrade_read(self, tenants):
        tenant_a, _tenant_b = tenants
        wamid = f"wamid.campaign.{tenant_a.unit_id}.2"
        campaign_id, _recipient_id = _make_campaign_recipient(tenant_a.unit_id, tenant_a.number_id, wamid)

        storage.update_campaign_recipient_delivery_status_by_wamid(wamid, "read", "2024-01-01T00:01:00+00:00")
        assert _get_campaign_recipient_delivery(campaign_id)["delivery_status"] == "read"

        applied = storage.update_campaign_recipient_delivery_status_by_wamid(
            wamid, "sent", "2024-01-01T00:00:00+00:00",
        )
        assert applied is True
        recipient = _get_campaign_recipient_delivery(campaign_id)
        assert recipient["delivery_status"] == "read"
        assert recipient["delivery_updated_at"] == "2024-01-01T00:01:00+00:00"

    def test_failed_cannot_override_delivered(self, tenants):
        tenant_a, _tenant_b = tenants
        wamid = f"wamid.campaign.{tenant_a.unit_id}.3"
        campaign_id, _recipient_id = _make_campaign_recipient(tenant_a.unit_id, tenant_a.number_id, wamid)

        storage.update_campaign_recipient_delivery_status_by_wamid(wamid, "delivered", "2024-01-01T00:00:00+00:00")
        storage.update_campaign_recipient_delivery_status_by_wamid(
            wamid, "failed", "2024-01-01T00:05:00+00:00", error_message="some later glitch",
        )
        recipient = _get_campaign_recipient_delivery(campaign_id)
        assert recipient["delivery_status"] == "delivered"
        assert recipient["delivery_error_message"] is None

    def test_unmatched_wamid_returns_false(self, tenants):
        assert storage.update_campaign_recipient_delivery_status_by_wamid(
            "wamid.campaign.never-recorded", "delivered", "2024-01-01T00:00:00+00:00",
        ) is False

    def test_wamid_scoped_to_recipient_not_shared_across_campaigns(self, tenants):
        """Two different campaign_recipients rows must never resolve to the
        same wamid - each row's wamid comes from its own, distinct Meta
        send response."""
        tenant_a, _tenant_b = tenants
        wamid_1 = f"wamid.campaign.{tenant_a.unit_id}.4a"
        wamid_2 = f"wamid.campaign.{tenant_a.unit_id}.4b"
        campaign_id_1, _ = _make_campaign_recipient(tenant_a.unit_id, tenant_a.number_id, wamid_1)
        campaign_id_2, _ = _make_campaign_recipient(tenant_a.unit_id, tenant_a.number_id, wamid_2)

        storage.update_campaign_recipient_delivery_status_by_wamid(wamid_1, "read", "2024-01-01T00:00:00+00:00")

        assert _get_campaign_recipient_delivery(campaign_id_1)["delivery_status"] == "read"
        assert _get_campaign_recipient_delivery(campaign_id_2)["delivery_status"] is None
