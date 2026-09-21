"""
Lightweight persistence for the registration poller and campaign manager.

This package replaces the old single-file storage.py. It's split by table
ownership (mirroring how init_db() itself is organized): schema (no
migration tool - see schema.py), dedup tracking, units+numbers+templates,
users, campaigns, login lockout, and WABA messaging limits.

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
    MODULE_KRYX_BOOKINGS,
    MODULE_AI_ASSISTANT,
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
    ensure_webhook_slug,
    get_unit_by_phone_id,
    get_unit_by_slug,
    get_unit_by_webhook_slug,
    get_active_units,
    get_unit_ids_for_org,
    count_units_for_org,
    count_whatsapp_numbers_for_org,
    get_whatsapp_numbers,
    get_whatsapp_number_by_id,
    get_whatsapp_number_by_phone_id,
    set_whatsapp_number_ai_toggles,
    update_whatsapp_number_quality,
    update_whatsapp_number_display_number,
    mark_whatsapp_number_disconnected,
    clear_whatsapp_number_disconnected,
    get_disconnected_whatsapp_numbers,
    get_template,
    get_form_whatsapp_template_id,
    get_template_by_id,
    list_registration_templates,
    upsert_registration_template,
    list_form_mappings,
    upsert_form_mapping,
    delete_form_mapping,
    list_registration_event_templates,
    upsert_registration_event_template,
    delete_registration_event_template,
    get_registration_event_template,
    get_cached_signups,
    set_cached_signups,
    create_whatsapp_number,
    create_onboarding_intent,
    consume_latest_onboarding_intent,
    get_meta_platform_settings,
    get_pco_platform_settings,
    create_pco_oauth_state,
    consume_pco_oauth_state,
    get_pco_org_settings,
    save_pco_oauth_tokens,
    disconnect_pco_oauth,
    sync_pco_subdomain,
    create_unit_webhook_secret,
    list_unit_webhook_secrets,
    get_unit_webhook_secrets_decrypted,
    delete_unit_webhook_secret,
    get_stitch_credentials,
    is_stitch_active,
)

from .meta_apps import (
    get_meta_app_secrets_decrypted,
)

from .users import (
    get_user,
    get_user_by_id,
    update_user_password,
    update_user_username,
    update_user_email,
    create_user,
    assign_user_unit,
    count_active_org_admins,
    count_active_org_users,
)

from .campaigns import (
    create_campaign,
    add_campaign_recipient,
    update_campaign_recipient,
    update_campaign_recipient_delivery_status_by_wamid,
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
)

from .send_log import (
    record_send,
    already_sent,
    update_send_log_delivery_status_by_wamid,
    get_recent_sends,
    get_send_count,
    get_distinct_number_ids,
    get_send_status_summary,
    count_sent_messages_for_org_since,
)

from .usage import (
    send_totals_by_number,
    daily_send_group_count,
    daily_send_counts,
    unit_label_map,
    number_label_map,
)

from .serving import (
    STATUS_FILTERS as SERVING_STATUS_FILTERS,
    PLAN_SELECTION_MODES as SERVING_PLAN_SELECTION_MODES,
    SCHEDULE_TYPES as SERVING_SCHEDULE_TYPES,
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
    get_cached_teams,
    set_cached_teams,
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

from .kryx_bookings import (
    BOOKING_STATUSES,
    STATUS_LABELS as KRYX_BOOKINGS_STATUS_LABELS,
    BOOKING_VARIABLES,
    RECIPIENT_MODES as KRYX_BOOKINGS_RECIPIENT_MODES,
    generate_api_key,
    set_connection_active,
    get_connection as get_kryx_bookings_connection,
    get_connection_by_api_key as get_kryx_bookings_connection_by_api_key,
    touch_last_used as touch_kryx_bookings_last_used,
    upsert_booking_automation,
    get_booking_automation,
    delete_booking_automation,
    list_booking_automations,
    list_active_booking_automations,
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
    increment_addon_messages_consumed,
    credit_addon_messages_purchased,
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

from .terms import (
    record_terms_acceptance,
    get_terms_acceptances_for_org,
)

from .conversations import (
    list_conversations,
    get_conversation,
    get_or_create_conversation,
    set_conversation_ai_status,
    mark_conversation_read,
    is_session_window_open,
    list_messages,
    get_message_with_unit,
    get_message_with_conversation,
    record_inbound_message,
    record_outbound_message,
    record_outbound_echo,
    mirror_outbound_to_inbox,
    set_message_body,
    update_delivery_status,
    update_message_media_download,
    mark_draft_sent,
    delete_draft_message,
)

from .ai_credentials import get_ai_credentials

from .ai_ingestion_settings import get_ai_ingestion_settings

from .groq_credentials import get_groq_credentials

from .knowledge_base import (
    list_entries as list_knowledge_base_entries,
    get_entry as get_knowledge_base_entry,
    create_entry as create_knowledge_base_entry,
    update_entry as update_knowledge_base_entry,
    delete_entry as delete_knowledge_base_entry,
    replace_source_entries as replace_knowledge_base_source_entries,
    search_active_entries as search_knowledge_base_entries,
    get_source_document_title as get_knowledge_base_source_document_title,
    list_source_chunks as list_knowledge_base_source_chunks,
    delete_source as delete_knowledge_base_source,
    set_source_active as set_knowledge_base_source_active,
)

from .ai_reply_log import (
    record_reply as record_ai_reply,
    count_replies_today as count_ai_replies_today,
    reply_token_usage_by_org,
    keyword_reply_counts_by_org,
    reply_counts_by_category,
)

from .ai_ingestion_log import (
    record_ingestion as record_ai_ingestion,
    ingestion_token_usage_by_org,
)

from .whatsapp_number_ai_settings import (
    get_ai_settings as get_whatsapp_number_ai_settings,
    upsert_ai_settings as upsert_whatsapp_number_ai_settings,
)

from .ai_auto_reply_rules import (
    list_rules as list_ai_auto_reply_rules,
    get_rule as get_ai_auto_reply_rule,
    create_rule as create_ai_auto_reply_rule,
    update_rule as update_ai_auto_reply_rule,
    delete_rule as delete_ai_auto_reply_rule,
    find_matching_rule as find_matching_ai_auto_reply_rule,
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
    "MODULE_PCO", "MODULE_SME_METRICS", "MODULE_EMAIL_WA", "MODULE_ICAL", "MODULE_STITCH", "MODULE_KRYX_BOOKINGS",
    "MODULE_AI_ASSISTANT", "AVAILABLE_MODULES",
    "is_enabled", "enable", "disable", "orgs_with_module_enabled", "enabled_modules_for_org",
    "is_granted", "grant", "revoke", "granted_modules_for_org", "migrate_legacy_email_wa_module_key",
    "REGISTRATION_TEMPLATE_TYPES",
    "get_unit_by_phone_id", "get_unit_by_slug", "get_unit_by_webhook_slug", "generate_webhook_slug",
    "ensure_webhook_slug",
    "get_active_units", "get_unit_ids_for_org", "count_units_for_org", "count_whatsapp_numbers_for_org",
    "get_whatsapp_numbers", "get_whatsapp_number_by_id", "get_whatsapp_number_by_phone_id",
    "set_whatsapp_number_ai_toggles",
    "update_whatsapp_number_quality",
    "update_whatsapp_number_display_number",
    "mark_whatsapp_number_disconnected",
    "clear_whatsapp_number_disconnected",
    "get_disconnected_whatsapp_numbers",
    "get_template", "get_form_whatsapp_template_id", "get_template_by_id",
    "list_registration_templates", "upsert_registration_template",
    "list_form_mappings", "upsert_form_mapping", "delete_form_mapping",
    "list_registration_event_templates", "upsert_registration_event_template",
    "delete_registration_event_template", "get_registration_event_template",
    "get_cached_signups", "set_cached_signups",
    "get_user", "get_user_by_id", "update_user_password", "update_user_username",
    "update_user_email",
    "create_user", "assign_user_unit", "count_active_org_admins", "count_active_org_users",
    "create_campaign", "add_campaign_recipient", "update_campaign_recipient",
    "update_campaign_recipient_delivery_status_by_wamid",
    "update_campaign_progress", "finalize_campaign_status", "get_campaign_status",
    "get_campaign_payload", "set_campaign_payload", "clear_campaign_payload",
    "request_campaign_cancel", "list_pending_scheduled_campaigns",
    "list_campaigns", "get_campaign", "list_throttled_campaigns",
    "get_lockout", "record_login_attempt", "get_login_attempt_row", "clear_login_attempts",
    "log_sent_message", "count_recent_unique_recipients", "oldest_message_in_window",
    "get_waba_limit", "upsert_waba_limit_tier", "set_waba_restricted",
    "record_send", "already_sent", "update_send_log_delivery_status_by_wamid",
    "get_recent_sends", "get_send_count", "get_distinct_number_ids",
    "get_send_status_summary", "count_sent_messages_for_org_since",
    "send_totals_by_number", "daily_send_group_count", "daily_send_counts",
    "unit_label_map", "number_label_map",
    "SERVING_STATUS_FILTERS", "SERVING_PLAN_SELECTION_MODES", "SERVING_SCHEDULE_TYPES",
    "list_serving_rules", "get_serving_rule_by_id",
    "list_active_serving_rules", "upsert_serving_rule", "delete_serving_rule",
    "is_serving_reminder_sent", "mark_serving_reminder", "list_deferred_serving_reminders",
    "get_serving_reminder_counts", "get_cached_teams", "set_cached_teams",
    "get_cached_service_types", "set_cached_service_types", "create_whatsapp_number", "create_onboarding_intent", "consume_latest_onboarding_intent", "get_meta_platform_settings",
    "get_pco_platform_settings", "create_pco_oauth_state", "consume_pco_oauth_state", "get_pco_org_settings", "save_pco_oauth_tokens", "disconnect_pco_oauth", "sync_pco_subdomain",
    "create_unit_webhook_secret", "list_unit_webhook_secrets", "get_unit_webhook_secrets_decrypted", "delete_unit_webhook_secret",
    "get_stitch_credentials", "is_stitch_active", "get_meta_app_secrets_decrypted",
    "generate_local_part", "create_email_integration", "upsert_email_integration",
    "delete_email_integration", "get_email_integration_by_id", "get_email_integration_by_local_part",
    "list_email_integrations", "is_inbound_email_processed", "mark_inbound_email_processed",
    "upsert_email_wa_integration", "delete_email_wa_integration", "get_email_wa_integration_by_id",
    "get_email_wa_integration_by_local_part", "list_email_wa_integrations",
    "BOOKING_STATUSES", "KRYX_BOOKINGS_STATUS_LABELS", "BOOKING_VARIABLES", "KRYX_BOOKINGS_RECIPIENT_MODES",
    "generate_api_key", "set_connection_active",
    "get_kryx_bookings_connection", "get_kryx_bookings_connection_by_api_key",
    "touch_kryx_bookings_last_used", "upsert_booking_automation", "get_booking_automation",
    "delete_booking_automation", "list_booking_automations", "list_active_booking_automations",
    "is_email_wa_inbound_processed", "mark_email_wa_inbound_processed",
    "get_ical_event_by_source", "upsert_ical_event", "cancel_ical_event",
    "get_or_create_ical_link", "attach_event_to_link", "get_ical_link_with_events",
    "mark_ical_link_accessed",
    "Subscription", "get_plan_by_key", "list_plans", "get_addon_by_key", "list_addons",
    "get_coupon_by_code", "increment_coupon_redemption", "list_coupons", "create_subscription",
    "get_subscription", "get_subscription_by_id", "update_subscription",
    "add_subscription_item", "remove_subscription_item", "remove_one_subscription_item", "clear_subscription_items",
    "list_active_addons_for_subscription", "increment_addon_messages_consumed",
    "credit_addon_messages_purchased",
    "log_transaction", "claim_pending_initial_transaction", "finalize_initial_transaction",
    "get_transaction_by_reference",
    "list_subscriptions_with_pending_downgrade", "list_active_subscriptions_due_for_billing",
    "list_subscriptions_with_pending_cancellation",
    "is_org_current",
    "record_terms_acceptance", "get_terms_acceptances_for_org",
    "list_conversations", "get_conversation", "get_or_create_conversation",
    "set_conversation_ai_status",
    "mark_conversation_read", "is_session_window_open", "list_messages",
    "get_message_with_unit", "get_message_with_conversation", "record_inbound_message",
    "record_outbound_message",
    "record_outbound_echo",
    "mirror_outbound_to_inbox",
    "set_message_body",
    "update_delivery_status", "update_message_media_download",
    "mark_draft_sent", "delete_draft_message",
    "get_ai_credentials", "get_ai_ingestion_settings", "get_groq_credentials",
    "list_knowledge_base_entries", "get_knowledge_base_entry", "create_knowledge_base_entry",
    "update_knowledge_base_entry", "delete_knowledge_base_entry",
    "replace_knowledge_base_source_entries", "search_knowledge_base_entries",
    "get_knowledge_base_source_document_title",
    "list_knowledge_base_source_chunks", "delete_knowledge_base_source", "set_knowledge_base_source_active",
    "record_ai_reply", "count_ai_replies_today", "record_ai_ingestion",
    "reply_token_usage_by_org", "keyword_reply_counts_by_org", "reply_counts_by_category",
    "ingestion_token_usage_by_org",
    "get_whatsapp_number_ai_settings", "upsert_whatsapp_number_ai_settings",
    "list_ai_auto_reply_rules", "get_ai_auto_reply_rule", "create_ai_auto_reply_rule",
    "update_ai_auto_reply_rule", "delete_ai_auto_reply_rule", "find_matching_ai_auto_reply_rule",
]
