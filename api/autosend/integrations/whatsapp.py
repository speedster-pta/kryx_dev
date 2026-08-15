import logging

import anyio
import httpx

from autosend.config import settings
from autosend.integrations.whatsapp_payload import build_button_components

BASE_URL = "https://graph.facebook.com/v21.0"

logger = logging.getLogger(__name__)


def _build_template_payload(
    to_phone_e164: str,
    template_name: str,
    body_values: list[str],
    *,
    header_image_url: str | None = None,
    button_values: list[str | None] | None = None,
) -> dict:
    """Shared payload shape for every send_*_template method below - body
    text is the only part that varies per template; button/header
    component construction is identical across all three."""
    components = [
        {
            "type": "body",
            "parameters": [{"type": "text", "text": value} for value in body_values],
        },
    ]
    components.extend(build_button_components(button_values))
    if header_image_url:
        components.insert(0, {
            "type": "header",
            "parameters": [{"type": "image", "image": {"link": header_image_url}}],
        })
    return {
        "messaging_product": "whatsapp",
        "to": to_phone_e164,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": "en"},
            "components": components,
        },
    }


class MessagingLimitExceeded(Exception):
    """Raised by the send_*_template methods when the 24h messaging limit
    gate blocks the send - callers (e.g. registration_poller.py) can catch
    this to defer/retry rather than treating it as a hard delivery failure."""


class WhatsAppSendError(Exception):
    """Raised when Meta rejects a send for a reason OTHER than the 24h
    messaging limit (bad template, invalid phone number, param mismatch,
    etc). Carries Meta's own error code/message so callers (send_log) can
    record a structured error instead of the generic httpx.HTTPStatusError
    that raise_for_status() would otherwise produce, which only says
    "400 Bad Request" with no indication of *why*."""

    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}" if code else message)


class WhatsAppClient:
    def __init__(self, access_token: str, phone_number_id: str, number: dict | None = None):
        self.phone_number_id = phone_number_id
        self.number = number
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    async def _post_messages(self, payload: dict) -> dict:
        to_phone = payload.get("to", "unknown")
        if settings.dry_run:
            logger.info(
                "[SIMULATION MODE / DRY RUN] Intercepted WhatsApp send to %s. Payload: %s",
                to_phone, payload,
            )
            self._record(to_phone)
            return {
                "messaging_product": "whatsapp",
                "contacts": [{"input": to_phone, "wa_id": to_phone}],
                "messages": [{"id": f"wamid.simulated_{to_phone}"}],
            }

        response = await self.client.post(
            f"/{self.phone_number_id}/messages", json=payload
        )
        if response.status_code >= 400:
            logger.error(
                f"WhatsApp API error {response.status_code}: {response.text}"
            )
            self._handle_error_response(response)
        response.raise_for_status()
        self._record(to_phone)
        return response.json()


    async def _gate(self) -> None:
        if self.number is None:
            logger.warning(
                "WhatsAppClient for %s sending without a `number` dict - "
                "this send won't be gated or logged against the 24h "
                "messaging limit", self.phone_number_id,
            )
            return
        # Local import: avoids a circular import at module load time, since
        # whatsapp_limits.py imports BASE_URL from this module.
        from autosend import whatsapp_limits
        # gate_send() is synchronous and can hit Meta's Graph API
        # (_ensure_fresh_tier -> sync_tier_from_meta -> requests.get) on a
        # cache miss. Offload to a worker thread rather than calling it
        # inline - this method runs on the asyncio event loop (called from
        # send_*_template below), and a blocking call here would stall
        # webhook ingestion and every other coroutine on the loop for the
        # duration of that request, not just this send.
        allowed, reason = await anyio.to_thread.run_sync(whatsapp_limits.gate_send, self.number)
        if not allowed:
            raise MessagingLimitExceeded(reason)

    def _record(self, to_phone_e164: str) -> None:
        if self.number is None:
            return
        from autosend import whatsapp_limits
        whatsapp_limits.record_send(self.number, to_phone_e164)

    def _handle_error_response(self, response: httpx.Response) -> None:
        """Called after logging a >=400 response and before
        raise_for_status(). If this was Meta rejecting the send for
        exceeding the messaging limit, records that authoritatively (see
        whatsapp_limits.record_rejection) and raises MessagingLimitExceeded
        instead of letting raise_for_status() surface a generic HTTPStatusError -
        callers that specifically want to defer/retry on this condition can
        catch MessagingLimitExceeded rather than parsing the exception
        message themselves.

        For every other >=400 response, raises WhatsAppSendError carrying
        Meta's own error code/message, instead of falling through to
        raise_for_status()'s generic "400 Bad Request" (which send_log
        would otherwise have no useful error_code to record). NOTE: this
        used to return early with no error at all when self.number was
        None (record_rejection needs a number to check against) - that
        early return has moved to just around the limit-specific check, so
        callers without a `number` dict still get a real WhatsAppSendError
        instead of losing error detail entirely."""
        try:
            body = response.json()
        except Exception:
            return
        error = body.get("error", {}) if isinstance(body, dict) else {}

        if self.number is not None:
            from autosend import whatsapp_limits
            if whatsapp_limits.record_rejection(self.number, body):
                raise MessagingLimitExceeded(
                    error.get("message", "24h messaging limit reached")
                )

        if error:
            raise WhatsAppSendError(
                code=error.get("code"), message=error.get("message", "WhatsApp API error")
            )

    async def send_free_acknowledgment_template(
        self,
        to_phone_e164: str,
        template_name: str,
        registrant_first_name: str,
        event_name: str,
        header_image_url: str | None = None,
        button_values: list[str | None] | None = None,
        body_values: list[str] | None = None,
    ) -> dict:
        """Body: {{1}}=name, {{2}}=event_name by default. body_values
        (optional): explicit ordered list of body parameter text, sent
        instead of [registrant_first_name, event_name] - used by
        registration_poller.py once a template's body_variable_order
        includes a reorder or a Custom Text entry. registrant_first_name/
        event_name are kept as-is (rather than removed) both as the
        default body when body_values isn't given and to avoid a breaking
        signature change for any other caller.

        button_values (optional): one entry per button on the template, in
        order - only entries for dynamic URL buttons need a value, everything
        else should be None or omitted entirely."""
        await self._gate()
        payload = _build_template_payload(
            to_phone_e164, template_name,
            body_values if body_values is not None else [registrant_first_name, event_name],
            header_image_url=header_image_url,
            button_values=button_values,
        )
        return await self._post_messages(payload)

    async def send_payment_template(
        self,
        to_phone_e164: str,
        template_name: str,
        registrant_first_name: str,
        event_name: str,
        amount_due: str,
        reference: str,
        link_suffix: str,
        header_image_url: str | None = None,
        button_values: list[str | None] | None = None,
        body_values: list[str] | None = None,
    ) -> dict:
        """
        body: {{1}}=name, {{2}}=event_name, {{3}}=amount_due, {{4}}=reference
        by default. body_values (optional): explicit ordered list of body
        parameter text, sent instead of the four positional args above -
        used by registration_poller.py once a template's body_variable_order
        includes a reorder or a Custom Text entry.

        button_values (optional): one entry per button on the template, in
        order - only entries for dynamic URL buttons need a value, everything
        else should be None. If not provided at all (None), falls back to a
        single button at index 0 filled with link_suffix (the Stitch payment
        link suffix) - this preserves existing behaviour for any
        payment_reminder template that hasn't been reconfigured with an
        explicit button variable yet.
        """
        await self._gate()
        if button_values is None:
            button_values = [link_suffix]

        payload = _build_template_payload(
            to_phone_e164, template_name,
            body_values if body_values is not None
            else [registrant_first_name, event_name, amount_due, reference],
            header_image_url=header_image_url,
            button_values=button_values,
        )
        return await self._post_messages(payload)

    async def send_template(
        self,
        to_phone_e164: str,
        template_name: str,
        *parameters: str,
        header_image_url: str | None = None,
        button_values: list[str | None] | None = None,
    ) -> dict:
        await self._gate()
        payload = _build_template_payload(
            to_phone_e164, template_name, list(parameters),
            header_image_url=header_image_url,
            button_values=button_values,
        )
        return await self._post_messages(payload)

    async def send_text(
        self,
        to_phone_e164: str,
        text: str,
    ) -> dict:
        payload = {
            "messaging_product": "whatsapp",
            "to": to_phone_e164,
            "type": "text",
            "text": {
                "body": text,
            },
        }

        return await self._post_messages(payload)

