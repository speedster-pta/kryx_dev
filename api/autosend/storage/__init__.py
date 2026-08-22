"""
Lightweight persistence for the registration poller and campaign manager.

This package replaces the old single-file storage.py. It's split by table
ownership (mirroring how init_db() itself is organized): schema (no
migration tool - see schema.py), dedup tracking, units+numbers+templates,
staff users, campaigns, login lockout, and WABA messaging limits.

Every name below was previously a top-level function in storage.py -
re-exported here so existing call sites (`from autosend import
storage` then `storage.get_template(...)`, etc.) don't need to change.
"""

from ._db import DB_PATH, _connect

from .schema import init_core_schema


def init_db() -> None:
    with _connect() as conn:
        init_core_schema(conn)

from .dedup import (
    get_signup_watermark,
    set_signup_watermark,
    get_recent_failures,
    get_recent_form_failures,
    is_processed,
    mark_processed,
    is_form_submission_processed,
    mark_form_submission_processed,
)

from .organisations import (
    Organisation,
    create_organisation,
    generate_unique_slug,
    get_organisation,
    get_organisation_by_slug,
    list_organisations,
    deactivate_organisation,
    activate_organisation,
    is_org_active,
    is_org_email_verified,
    update_organisation_name,
)

from .email_verification import (
    create_email_verification_token,
    consume_email_verification_token,
    mark_email_verified,
)

from .platform_email import (
    get_platform_email_settings,
)

from .modules import (
    MODULE_PCO,
    MODULE_SME_METRICS,
    MODULE_EMAIL_WA,
    MODULE_ICAL,
    MODULE_STITCH,
    AVAILABLE_MODULES,
    is_enabled,
    enable,
    disable,
    orgs_with_module_enabled,
    enabled_modules_for_org,
    is_granted,
    grant,
    revoke,
    granted_modules_for_org,
    migrate_legacy_email_wa_module_key,
)

from .units import (
    REGISTRATION_TEMPLATE_TYPES,
    generate_webhook_slug,
    get_unit_by_phone_id,
    get_unit_by_slug,
    get_unit_by_webhook_slug,
    get_active_units,
    get_unit_ids_for_org,
    count_units_for_org,
    count_whatsapp_numbers_for_org,
    get_whatsapp_numbers,
    get_whatsapp_number_by_id,
    update_whatsapp_number_quality,
    update_whatsapp_number_display_number,
    get_template,
    get_form_whatsapp_template_id,
    get_template_by_id,
    list_registration_templates,
    upsert_registration_template,
    list_form_mappings,
    upsert_form_mapping,
    delete_form_mapping,
    create_whatsapp_number,
    create_onboarding_intent,
    consume_latest_onboarding_intent,
    get_meta_platform_settings,
    get_pco_platform_settings,
    create_pco_oauth_state,
    consume_pco_oauth_state,
    get_pco_org_settings,
    save_pco_oauth_tokens,
    create_unit_webhook_secret,
    list_unit_webhook_secrets,
    get_unit_webhook_secrets_decrypted,
    delete_unit_webhook_secret,
    get_stitch_credentials,
    is_stitch_active,
)

from .users import (
    get_user,
    get_user_by_id,
    update_staff_password,
    update_staff_username,
    update_staff_email,
    create_user,
    assign_staff_unit,
    count_active_org_admins,
    count_active_org_users,
)

from .campaigns import (
    create_campaign,
    add_campaign_recipient,
    update_campaign_recipient,
    update_campaign_progress,
    finalize_campaign_status,
    get_campaign_status,
    get_campaign_payload,
    set_campaign_payload,
    clear_campaign_payload,
    request_campaign_cancel,
    list_pending_scheduled_campaigns,
    list_campaigns,
    get_campaign,
    list_throttled_campaigns,
)

from .auth_lockout import (
    get_lockout,
    record_login_attempt,
    get_login_attempt_row,
    clear_login_attempts,
)

from .limits import (
    log_sent_message,
    count_recent_unique_recipients,
    oldest_message_in_window,
    get_waba_limit,
    upsert_waba_limit_tier,
    set_waba_restricted,
    daily_message_counts,
    waba_label_map,
)

from .send_log import (
    record_send,
    get_recent_sends,
    get_send_count,
    get_distinct_number_ids,
    get_send_status_summary,
    count_sent_messages_for_org_since,
)

from .serving import (
    STATUS_FILTERS as SERVING_STATUS_FILTERS,
    PLAN_SELECTION_MODES as SERVING_PLAN_SELECTION_MODES,
    list_serving_rules,
    get_serving_rule_by_id,
    list_active_serving_rules,
    upsert_serving_rule,
    delete_serving_rule,
    is_serving_reminder_sent,
    mark_serving_reminder,
    list_deferred_serving_reminders,
    get_serving_reminder_counts,
    get_cached_service_types,
    set_cached_service_types,
)

from .sme_metrics import (
    generate_local_part,
    create_email_integration,
    upsert_email_integration,
    delete_email_integration,
    get_email_integration_by_id,
    get_email_integration_by_local_part,
    list_email_integrations,
    is_inbound_email_processed,
    mark_inbound_email_processed,
)

from .email_wa import (
    upsert_email_wa_integration,
    delete_email_wa_integration,
    get_email_wa_integration_by_id,
    get_email_wa_integration_by_local_part,
    list_email_wa_integrations,
    is_email_wa_inbound_processed,
    mark_email_wa_inbound_processed,
)

from .billing import (
    Subscription,
    get_plan_by_key,
    get_plan_by_id,
    list_plans,
    get_addon_by_key,
    list_addons,
    get_coupon_by_code,
    increment_coupon_redemption,
    list_coupons,
    create_subscription,
    get_subscription,
    get_subscription_by_id,
    update_subscription,
    add_subscription_item,
    remove_subscription_item,
    remove_one_subscription_item,
    clear_subscription_items,
    list_active_addons_for_subscription,
    log_transaction,
    claim_pending_initial_transaction,
    finalize_initial_transaction,
    get_transaction_by_reference,
    list_subscriptions_with_pending_downgrade,
    list_active_subscriptions_due_for_billing,
    list_subscriptions_with_pending_cancellation,
    is_org_current,
)

from .ical import (
    get_ical_event_by_source,
    upsert_ical_event,
    cancel_ical_event,
    get_or_create_ical_link,
    attach_event_to_link,
    get_ical_link_with_events,
    mark_ical_link_accessed,
)

# Kept in sync by hand with the explicit imports above - not derived from
# them - so an import typo here would only hide a name from `import *`,
# never break the `storage.get_x(...)` call sites those imports exist for.
__all__ = [
    "DB_PATH",
    "init_db",
    "get_signup_watermark", "set_signup_watermark",
    "get_recent_failures", "get_recent_form_failures",
    "is_processed", "mark_processed",
    "is_form_submission_processed", "mark_form_submission_processed",
    "Organisation", "create_organisation", "generate_unique_slug", "get_organisation", "get_organisation_by_slug",
    "list_organisations", "deactivate_organisation", "activate_organisation", "is_org_active",
    "is_org_email_verified", "update_organisation_name",
    "create_email_verification_token", "consume_email_verification_token", "mark_email_verified",
    "get_platform_email_settings",
    "MODULE_PCO", "MODULE_SME_METRICS", "MODULE_EMAIL_WA", "MODULE_ICAL", "MODULE_STITCH", "AVAILABLE_MODULES",
    "is_enabled", "enable", "disable", "orgs_with_module_enabled", "enabled_modules_for_org",
    "is_granted", "grant", "revoke", "granted_modules_for_org", "migrate_legacy_email_wa_module_key",
    "REGISTRATION_TEMPLATE_TYPES",
    "get_unit_by_phone_id", "get_unit_by_slug", "get_unit_by_webhook_slug", "generate_webhook_slug",
    "get_active_units", "get_unit_ids_for_org", "count_units_for_org", "count_whatsapp_numbers_for_org",
    "get_whatsapp_numbers", "get_whatsapp_number_by_id", "update_whatsapp_number_quality",
    "update_whatsapp_number_display_number",
    "get_template", "get_form_whatsapp_template_id", "get_template_by_id",
    "list_registration_templates", "upsert_registration_template",
    "list_form_mappings", "upsert_form_mapping", "delete_form_mapping",
    "get_user", "get_user_by_id", "update_staff_password", "update_staff_username",
    "update_staff_email",
    "create_user", "assign_staff_unit", "count_active_org_admins", "count_active_org_users",
    "create_campaign", "add_campaign_recipient", "update_campaign_recipient",
    "update_campaign_progress", "finalize_campaign_status", "get_campaign_status",
    "get_campaign_payload", "set_campaign_payload", "clear_campaign_payload",
    "request_campaign_cancel", "list_pending_scheduled_campaigns",
    "list_campaigns", "get_campaign", "list_throttled_campaigns",
    "get_lockout", "record_login_attempt", "get_login_attempt_row", "clear_login_attempts",
    "log_sent_message", "count_recent_unique_recipients", "oldest_message_in_window",
    "get_waba_limit", "upsert_waba_limit_tier", "set_waba_restricted",
    "daily_message_counts", "waba_label_map",
    "record_send", "get_recent_sends", "get_send_count", "get_distinct_number_ids",
    "get_send_status_summary", "count_sent_messages_for_org_since",
    "SERVING_STATUS_FILTERS", "SERVING_PLAN_SELECTION_MODES", "list_serving_rules", "get_serving_rule_by_id",
    "list_active_serving_rules", "upsert_serving_rule", "delete_serving_rule",
    "is_serving_reminder_sent", "mark_serving_reminder", "list_deferred_serving_reminders",
    "get_serving_reminder_counts",
    "get_cached_service_types", "set_cached_service_types", "create_whatsapp_number", "create_onboarding_intent", "consume_latest_onboarding_intent", "get_meta_platform_settings",
    "get_pco_platform_settings", "create_pco_oauth_state", "consume_pco_oauth_state", "get_pco_org_settings", "save_pco_oauth_tokens",
    "create_unit_webhook_secret", "list_unit_webhook_secrets", "get_unit_webhook_secrets_decrypted", "delete_unit_webhook_secret",
    "get_stitch_credentials", "is_stitch_active",
    "generate_local_part", "create_email_integration", "upsert_email_integration",
    "delete_email_integration", "get_email_integration_by_id", "get_email_integration_by_local_part",
    "list_email_integrations", "is_inbound_email_processed", "mark_inbound_email_processed",
    "upsert_email_wa_integration", "delete_email_wa_integration", "get_email_wa_integration_by_id",
    "get_email_wa_integration_by_local_part", "list_email_wa_integrations",
    "is_email_wa_inbound_processed", "mark_email_wa_inbound_processed",
    "get_ical_event_by_source", "upsert_ical_event", "cancel_ical_event",
    "get_or_create_ical_link", "attach_event_to_link", "get_ical_link_with_events",
    "mark_ical_link_accessed",
    "Subscription", "get_plan_by_key", "list_plans", "get_addon_by_key", "list_addons",
    "get_coupon_by_code", "increment_coupon_redemption", "list_coupons", "create_subscription",
    "get_subscription", "get_subscription_by_id", "update_subscription",
    "add_subscription_item", "remove_subscription_item", "remove_one_subscription_item", "clear_subscription_items",
    "list_active_addons_for_subscription",
    "log_transaction", "claim_pending_initial_transaction", "finalize_initial_transaction",
    "get_transaction_by_reference",
    "list_subscriptions_with_pending_downgrade", "list_active_subscriptions_due_for_billing",
    "list_subscriptions_with_pending_cancellation",
    "is_org_current",
]
