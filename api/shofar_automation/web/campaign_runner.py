"""Campaign execution engine: the send loop and its throttle/resume
handling, called from a background thread (immediate sends, see
create_campaign in campaigns_router.py) or from the scheduler (scheduled
sends and throttled-campaign resumes, see scheduler.py).

Split out of campaigns_router.py so this logic - and its comments, which
document some genuinely non-obvious behavior around resuming throttled
campaigns - can be read and changed independently of the HTTP layer.
"""
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from shofar_automation import storage, whatsapp_limits
from shofar_automation.web import whatsapp_bulk

logger = logging.getLogger(__name__)

DEFAULT_SEND_CONCURRENCY = 20


def _finalize_campaign_status(campaign_id: int, status: str):
    """Best-effort update of the campaign's terminal status. Swallows
    errors since this is often called from an exception handler and must
    not raise."""
    try:
        storage.finalize_campaign_status(campaign_id, status)
    except sqlite3.Error:
        # Ordinary storage hiccup (locked DB, etc.) - same "must not raise"
        # contract as before, just logged as a routine DB failure.
        logger.exception("DB error finalizing status for campaign %s", campaign_id)
    except Exception:
        # Not a DB error - almost certainly a bug in this function or a
        # caller passing a bad campaign_id/status. Still can't raise (see
        # docstring: this is frequently called from inside another
        # except block, and raising here would replace/mask the original
        # exception), but log at CRITICAL so it doesn't blend in with
        # ordinary, expected DB errors and get missed.
        logger.critical(
            "Unexpected (non-DB) error finalizing status for campaign %s - this looks like a bug",
            campaign_id, exc_info=True,
        )


def _send_one(token: str, phone_number_id: str, phone: str, template_name: str, language: str,
              body_var_columns: list[str], button_var_columns: list[str], image_media_id, row: dict):
    """Runs on a worker thread inside the batch's ThreadPoolExecutor.
    Deliberately does nothing but build the payload and make the HTTP call
    - no DB access here, so sqlite3 writes never happen off the main
    thread. Returns (ok, response) same shape as whatsapp_bulk.send_message,
    with network/build exceptions folded into a failed response so the
    caller has one uniform result type to record regardless of what went
    wrong."""
    try:
        body_values = [row.get(col, "") for col in body_var_columns]
        # button_var_columns keeps a slot per button position (even if
        # blank), unlike body_var_columns - a blank entry means that
        # button has no dynamic variable, so look up nothing for it rather
        # than treating "" as a real column name.
        button_values = [row.get(col, "") if col else None for col in button_var_columns]
        payload = whatsapp_bulk.build_payload(
            phone, template_name, language, body_values, image_media_id, button_values,
        )
        return whatsapp_bulk.send_message(token, phone_number_id, payload)
    except (requests.exceptions.RequestException, ValueError) as e:
        # A network/HTTP failure calling Meta, or a validation error while
        # building the payload (e.g. bad template config for this row) -
        # both are expected, per-recipient failure modes. Fold into the
        # same (ok, response) shape as a real send rejection so the caller
        # has one uniform result type, and only this row is recorded as
        # failed - the rest of the batch is unaffected.
        return False, {"error": f"error: {e}"}
    # Deliberately no bare `except Exception` here. Anything else
    # (TypeError, AttributeError, unexpected KeyError, ...) means this
    # code itself is broken, not that this particular row failed - and
    # since build_payload/send_message would raise it identically for
    # every row in the batch, swallowing it here would bury a systemic
    # bug inside hundreds of near-identical "failed" recipients instead of
    # surfacing it. Let it propagate: as_completed().result() re-raises it
    # on the main thread in _run_campaign, where the outer except already
    # treats that as a catastrophic failure - the campaign is marked
    # "failed" and stops immediately instead of grinding through the rest
    # of the list one silently-broken row at a time.


def _run_campaign(campaign_id: int, number: dict,
                   template_name: str, language: str, image_media_id, rows, body_var_columns,
                   phone_column: str, delay_seconds: float, button_var_columns: list[str],
                   initial_sent: int = 0, initial_failed: int = 0, initial_skipped: int = 0):
    """initial_sent/failed/skipped: counts already recorded for this
    campaign from an earlier run segment - non-zero when this call is
    resuming a throttled campaign (see launch_scheduled_campaign) rather
    than starting a fresh one. Without these, a resumed campaign's dashboard
    counters (sent, success %, "x of y processed") would appear to reset to
    zero and restart from scratch even though `rows` here is correctly only
    the not-yet-sent remainder - the counters just weren't told about the
    work already done in the segment before the throttle."""
    token = number["access_token"]
    phone_number_id = number["phone_number_id"]
    sent, failed, skipped = initial_sent, initial_failed, initial_skipped
    cancelled = False
    throttled = False

    # How many sends this number holds in flight at once. Configurable per
    # WhatsAppNumber (numbers page) since real-world throughput varies by
    # number/tier; falls back to DEFAULT_SEND_CONCURRENCY for rows that
    # predate the column or leave it unset.
    batch_size = number.get("send_concurrency") or DEFAULT_SEND_CONCURRENCY
    reserve_fraction = whatsapp_limits.reserve_fraction_for(number)

    def _persist_remaining(remaining_rows):
        storage.set_campaign_payload(campaign_id, {
            "rows": remaining_rows,
            "body_var_columns": body_var_columns,
            "button_var_columns": button_var_columns,
            "phone_column": phone_column,
            "image_media_id": image_media_id,
        })

    idx = 0
    n = len(rows)
    try:
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            while idx < n:
                # Re-read from the DB once per batch rather than an
                # in-memory flag, so a cancel request from any
                # process/thread is picked up promptly and survives if
                # this worker itself restarts mid-send. Coarser than the
                # old per-message check (now per-batch, i.e. up to
                # batch_size sends may still be in flight when a cancel
                # comes in), which is the accepted trade-off of batching.
                if storage.get_campaign_status(campaign_id) == "cancelling":
                    cancelled = True
                    break

                # Confirm 24h capacity before building this batch, not
                # just once at the start - a long campaign can cross the
                # cap partway through, and another campaign or the
                # transactional flow may be drawing from the same WABA
                # pool concurrently. Size the batch down to whatever
                # headroom is actually available (None = unlimited tier)
                # instead of firing a full batch_size batch and hoping.
                remaining_capacity, reason = whatsapp_limits.available_capacity(number, reserve_fraction)
                if remaining_capacity == 0:
                    logger.info("Campaign %s throttled: %s", campaign_id, reason)
                    throttled = True
                    _persist_remaining(rows[idx:])
                    break
                effective_batch_size = (
                    batch_size if remaining_capacity is None else min(batch_size, remaining_capacity)
                )

                # Walk forward collecting up to effective_batch_size real
                # sends, recording+skipping blank-phone rows inline as we
                # go (they never touch the network or a send slot).
                batch = []  # list of (row_idx, phone, row)
                batch_end = idx
                while batch_end < n and len(batch) < effective_batch_size:
                    row = rows[batch_end]
                    phone = row.get(phone_column, "").strip()
                    if not phone:
                        rec_id = storage.add_campaign_recipient(campaign_id, phone)
                        skipped += 1
                        storage.update_campaign_recipient(rec_id, "skipped", "missing phone")
                        storage.update_campaign_progress(campaign_id, sent, failed)
                    else:
                        batch.append((batch_end, phone, row))
                    batch_end += 1

                if not batch:
                    # Batch was entirely blank-phone rows - move on
                    # without touching the executor or the 24h counter.
                    idx = batch_end
                    continue

                # Create every recipient record on the main thread up
                # front, before dispatching sends - keeps all sqlite3
                # writes off worker threads, and matches the old
                # behavior of a DB row existing before its send is
                # attempted.
                rec_ids = {row_idx: storage.add_campaign_recipient(campaign_id, phone)
                           for row_idx, phone, _ in batch}

                futures = {
                    executor.submit(
                        _send_one, token, phone_number_id, phone, template_name, language,
                        body_var_columns, button_var_columns, image_media_id, row,
                    ): (row_idx, phone)
                    for row_idx, phone, row in batch
                }

                # Only the HTTP call itself ran concurrently; everything
                # below - counters, recipient records, 24h logging - is
                # sequential on the main thread as results land, same as
                # the old per-message path.
                batch_throttled = False
                for future in as_completed(futures):
                    row_idx, phone = futures[future]
                    rec_id = rec_ids[row_idx]
                    ok, response = future.result()

                    if ok:
                        sent += 1
                        msg_id = response.get("messages", [{}])[0].get("id", "")
                        status_val, detail = "sent", msg_id
                        # Only log successful sends - a failed API call
                        # never reached Meta's messaging-limit counter.
                        whatsapp_limits.record_send(number, phone, campaign_id)
                    else:
                        failed += 1
                        status_val, detail = "failed", str(response.get("error", response))
                        # A real rejection from Meta is authoritative and
                        # should stop further batches immediately, not
                        # wait for the local counter to (never) reflect
                        # it - see whatsapp_limits.record_rejection's
                        # docstring. Everything else already in flight in
                        # this batch still finishes and gets recorded;
                        # only rows in *later* batches get held back.
                        if whatsapp_limits.record_rejection(number, response):
                            batch_throttled = True

                    storage.update_campaign_recipient(rec_id, status_val, detail)
                    storage.update_campaign_progress(campaign_id, sent, failed)

                idx = batch_end

                if batch_throttled:
                    throttled = True
                    _persist_remaining(rows[idx:])
                    break

                if delay_seconds and delay_seconds > 0:
                    # Now paces between batches rather than between
                    # individual messages, since sends within a batch are
                    # concurrent.
                    time.sleep(delay_seconds)

        # "complete" only when every recipient was actually sent - a
        # campaign where everything was skipped or failed should not be
        # reported as complete.
        if cancelled:
            campaign_status = "cancelled"
        elif throttled:
            campaign_status = "throttled"
        elif sent == 0 and len(rows) > 0:
            campaign_status = "failed"
        elif failed > 0 or skipped > 0:
            campaign_status = "partial"
        else:
            campaign_status = "complete"

    except Exception:
        # Catastrophic failure outside the per-recipient try/except (e.g. a
        # DB error). Make sure the campaign doesn't stay stuck on "running".
        logger.exception("Campaign %s failed unexpectedly", campaign_id)
        _finalize_campaign_status(campaign_id, "failed")
        return

    _finalize_campaign_status(campaign_id, campaign_status)


def launch_scheduled_campaign(campaign_id: int) -> None:
    """Called by the scheduler (shofar_automation.scheduler) when a
    scheduled campaign's time arrives - or when a throttled campaign is
    resumed after its 24h messaging limit frees up (see
    scheduler.recheck_throttled_campaigns). Both cases are "run the stored
    payload_json from where it left off", so they share this one path.
    Runs synchronously on the scheduler's own worker thread (see
    scheduler.py's note on AsyncIOScheduler's default executor), same as
    _run_campaign does when kicked off from a plain threading.Thread for an
    immediate send."""
    campaign = storage.get_campaign(campaign_id)
    if not campaign or campaign["status"] not in ("scheduled", "throttled"):
        # Cancelled before it fired, or something else already handled it -
        # don't run it.
        logger.info(
            "Skipping campaign %s: status is %s",
            campaign_id, campaign["status"] if campaign else "missing",
        )
        return

    payload = storage.get_campaign_payload(campaign_id)
    if not payload:
        logger.error("Campaign %s has no stored payload, cannot run", campaign_id)
        _finalize_campaign_status(campaign_id, "failed")
        return

    number = storage.get_whatsapp_number_by_id(campaign["whatsapp_number_id"])
    if not number:
        logger.error("Campaign %s: WhatsApp number no longer exists", campaign_id)
        _finalize_campaign_status(campaign_id, "failed")
        return

    # Flip to running before clearing the payload, not after - if the
    # process dies between these two lines, list_pending_scheduled_campaigns
    # (and list_throttled_campaigns) won't pick this campaign up again
    # (it's no longer 'scheduled'/'throttled'), which is the right outcome:
    # better to leave one send needing a manual check than to silently
    # double-send everyone on restart.
    storage.finalize_campaign_status(campaign_id, "running")
    storage.clear_campaign_payload(campaign_id)

    # For a fresh 'scheduled' launch there are no recipients yet, so these
    # are all 0 - harmless. For a 'throttled' resume, campaign_recipients
    # already has one row per person contacted in the earlier segment(s)
    # before the throttle hit; counting those directly (rather than trusting
    # campaign.sent/failed) is what makes the dashboard's "sent" count,
    # success %, and "x of y processed" continue from where they left off
    # instead of appearing to restart at zero.
    initial_sent = sum(1 for r in campaign["recipients"] if r["status"] == "sent")
    initial_failed = sum(1 for r in campaign["recipients"] if r["status"] == "failed")
    initial_skipped = sum(1 for r in campaign["recipients"] if r["status"] == "skipped")

    _run_campaign(
        campaign_id, number,
        campaign["template_name"], campaign["language"], payload["image_media_id"],
        payload["rows"], payload["body_var_columns"], payload["phone_column"],
        number.get("send_delay_seconds", 0.0), payload["button_var_columns"],
        initial_sent, initial_failed, initial_skipped,
    )
