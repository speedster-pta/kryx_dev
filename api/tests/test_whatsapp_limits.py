"""Coverage for two whatsapp_limits.py gaps found by comparing against the
sibling single-tenant project (dev-whatsapp/api/shofar_automation):

1. Tier-lookup fallback: a WABA reporting a tier string not in TIER_LIMITS
   used to silently fall back to DEFAULT_TIER's 250 cap. TIER_LIMITS now
   carries both the older (TIER_50/TIER_1K/TIER_10K/TIER_100K) and newer
   (TIER_2K/TIER_2000/TIER_10000/TIER_100000) naming, and cap_for_tier()
   also resolves the raw numeric cap the business_capability_update
   webhook delivers instead of a TIER_* label.
2. Disconnected-number flagging: Meta's Graph API code 100/subcode 33
   ("phone_number_id doesn't exist / no permission") now gets recognized
   via is_number_disconnected_error() and flagged via
   storage.mark_whatsapp_number_disconnected(), surfaced in
   storage.get_disconnected_whatsapp_numbers() and GET /ops/failures.
"""
from autosend import storage, whatsapp_limits


class TestCapForTier:
    def test_legacy_tier_names_resolve(self):
        assert whatsapp_limits.cap_for_tier("TIER_50") == 50
        assert whatsapp_limits.cap_for_tier("TIER_1K") == 1000
        assert whatsapp_limits.cap_for_tier("TIER_10K") == 10000
        assert whatsapp_limits.cap_for_tier("TIER_100K") == 100000

    def test_current_tier_names_resolve(self):
        """TIER_2K is the name a live WABA has been observed reporting
        instead of the documented TIER_2000 - both must map to the same
        cap, otherwise this WABA's true 2,000 cap gets silently treated
        as the 250 default (see the TIER_LIMITS fallback below)."""
        assert whatsapp_limits.cap_for_tier("TIER_2K") == 2000
        assert whatsapp_limits.cap_for_tier("TIER_2000") == 2000
        assert whatsapp_limits.cap_for_tier("TIER_10000") == 10000
        assert whatsapp_limits.cap_for_tier("TIER_100000") == 100000

    def test_unlimited_tier_has_no_cap(self):
        assert whatsapp_limits.cap_for_tier("TIER_UNLIMITED") is None

    def test_unknown_tier_falls_back_to_default_tier_cap(self):
        assert whatsapp_limits.cap_for_tier("TIER_NOT_A_REAL_ONE") == whatsapp_limits.TIER_LIMITS[
            whatsapp_limits.DEFAULT_TIER
        ]

    def test_none_tier_falls_back_to_default_tier_cap(self):
        assert whatsapp_limits.cap_for_tier(None) == whatsapp_limits.TIER_LIMITS[whatsapp_limits.DEFAULT_TIER]

    def test_raw_numeric_cap_from_capability_update_webhook_resolves_directly(self):
        """business_capability_update never sends a TIER_* label, only a
        raw number (see record_capability_update) - cap_for_tier() must
        resolve that straight through rather than treating it as an
        unrecognized tier string and falling back to the default."""
        assert whatsapp_limits.cap_for_tier("2000") == 2000
        assert whatsapp_limits.cap_for_tier("123456") == 123456


class TestRecordCapabilityUpdate:
    def test_stores_raw_numeric_cap_resolvable_by_cap_for_tier(self):
        waba_id = "waba-capability-update-test"
        whatsapp_limits.record_capability_update(waba_id, 2000)

        row = storage.get_waba_limit(waba_id)
        assert row["messaging_limit_tier"] == "2000"
        assert whatsapp_limits.cap_for_tier(row["messaging_limit_tier"]) == 2000


class TestIsNumberDisconnectedError:
    def test_matches_code_100_subcode_33(self):
        body = {"error": {"code": 100, "error_subcode": 33, "message": "Unsupported get request."}}
        assert whatsapp_limits.is_number_disconnected_error(body) is True

    def test_does_not_match_other_subcodes(self):
        body = {"error": {"code": 100, "error_subcode": 1, "message": "Something else."}}
        assert whatsapp_limits.is_number_disconnected_error(body) is False

    def test_does_not_match_messaging_limit_rejection(self):
        body = {"error": {"code": 131056, "message": "There are restrictions on how many messages..."}}
        assert whatsapp_limits.is_number_disconnected_error(body) is False

    def test_handles_missing_error_key(self):
        assert whatsapp_limits.is_number_disconnected_error({}) is False
        assert whatsapp_limits.is_number_disconnected_error(None) is False


class TestDisconnectedNumberFlagging:
    def test_mark_flags_number_and_appears_in_failures_list(self, tenants):
        tenant_a, tenant_b = tenants
        number = storage.get_whatsapp_number_by_id(tenant_a.number_id)

        storage.mark_whatsapp_number_disconnected(number["phone_number_id"], "2026-09-18T00:00:00+00:00")

        disconnected = storage.get_disconnected_whatsapp_numbers()
        matching = [n for n in disconnected if n["phone_number_id"] == number["phone_number_id"]]
        assert len(matching) == 1
        assert matching[0]["meta_disconnected_at"] == "2026-09-18T00:00:00+00:00"
        assert matching[0]["unit_name"] == tenant_a.unit_name

        # A number from another tenant that was never flagged must not show up.
        other_number = storage.get_whatsapp_number_by_id(tenant_b.number_id)
        assert not any(n["phone_number_id"] == other_number["phone_number_id"] for n in disconnected)

    def test_clear_removes_number_from_failures_list(self, tenants):
        tenant_a, _tenant_b = tenants
        number = storage.get_whatsapp_number_by_id(tenant_a.number_id)

        storage.mark_whatsapp_number_disconnected(number["phone_number_id"], "2026-09-18T00:00:00+00:00")
        storage.clear_whatsapp_number_disconnected(number["phone_number_id"])

        disconnected = storage.get_disconnected_whatsapp_numbers()
        assert not any(n["phone_number_id"] == number["phone_number_id"] for n in disconnected)

    def test_ops_failures_endpoint_includes_disconnected_numbers(self, client, tenants):
        import os

        tenant_a, _tenant_b = tenants
        number = storage.get_whatsapp_number_by_id(tenant_a.number_id)
        storage.mark_whatsapp_number_disconnected(number["phone_number_id"], "2026-09-18T00:00:00+00:00")

        response = client.get(
            "/ops/failures", headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]},
        )
        assert response.status_code == 200
        body = response.json()
        assert "disconnected_numbers" in body
        assert any(n["phone_number_id"] == number["phone_number_id"] for n in body["disconnected_numbers"])
