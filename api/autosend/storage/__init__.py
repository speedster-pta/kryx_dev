"""
Lightweight persistence for the registration poller and campaign manager.

This package replaces the old single-file storage.py. It's split by table
ownership (mirroring how init_db() itself is organized): schema/migrations,
dedup tracking, units+numbers+templates, staff users, campaigns,
login lockout, and WABA messaging limits.

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
    get_organisation,
    get_organisation_by_slug,
    list_organisations,
    deactivate_organisation,
)

from .modules import (
    MODULE_PCO,
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
)

from .units import (
    REGISTRATION_TEMPLATE_TYPES,
    get_unit_by_phone_id,
    get_unit_by_slug,
    get_active_units,
    get_unit_ids_for_org,
    get_whatsapp_numbers,
    get_whatsapp_number_by_id,
    update_whatsapp_number_quality,
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
)

from .users import (
    get_user,
    get_user_by_id,
    update_staff_password,
    update_staff_username,
    create_user,
    assign_staff_unit,
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
    get_send_status_summary
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

__all__ = [
    "DB_PATH",
    "init_db",
    "get_signup_watermark", "set_signup_watermark",
    "get_recent_failures", "get_recent_form_failures",
    "is_processed", "mark_processed",
    "is_form_submission_processed", "mark_form_submission_processed",
    "Organisation", "create_organisation", "get_organisation", "get_organisation_by_slug",
    "list_organisations", "deactivate_organisation",
    "MODULE_PCO", "AVAILABLE_MODULES",
    "is_enabled", "enable", "disable", "orgs_with_module_enabled", "enabled_modules_for_org",
    "is_granted", "grant", "revoke", "granted_modules_for_org",
    "REGISTRATION_TEMPLATE_TYPES",
    "get_unit_by_phone_id", "get_unit_by_slug", "get_active_units", "get_unit_ids_for_org",
    "get_whatsapp_numbers", "get_whatsapp_number_by_id", "update_whatsapp_number_quality",
    "get_template", "get_form_whatsapp_template_id", "get_template_by_id",
    "list_registration_templates", "upsert_registration_template",
    "list_form_mappings", "upsert_form_mapping", "delete_form_mapping",
    "get_user", "get_user_by_id", "update_staff_password", "update_staff_username",
    "create_user", "assign_staff_unit",
    "create_campaign", "add_campaign_recipient", "update_campaign_recipient",
    "update_campaign_progress", "finalize_campaign_status", "get_campaign_status",
    "get_campaign_payload", "set_campaign_payload", "clear_campaign_payload",
    "request_campaign_cancel", "list_pending_scheduled_campaigns",
    "list_campaigns", "get_campaign", "list_throttled_campaigns",
    "get_lockout", "record_login_attempt", "get_login_attempt_row", "clear_login_attempts",
    "log_sent_message", "count_recent_unique_recipients", "oldest_message_in_window",
    "get_waba_limit", "upsert_waba_limit_tier", "set_waba_restricted",
    "daily_message_counts", "waba_label_map",
    "record_send", "get_recent_sends",
    "SERVING_STATUS_FILTERS", "SERVING_PLAN_SELECTION_MODES", "list_serving_rules", "get_serving_rule_by_id",
    "list_active_serving_rules", "upsert_serving_rule", "delete_serving_rule",
    "is_serving_reminder_sent", "mark_serving_reminder", "list_deferred_serving_reminders",
    "get_serving_reminder_counts",
    "get_cached_service_types", "set_cached_service_types", "create_whatsapp_number", "create_onboarding_intent", "consume_latest_onboarding_intent", "get_meta_platform_settings"
]
