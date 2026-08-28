"""Regression tests for the shared WhatsApp template-parameter sanitizer.

Meta's WhatsApp Cloud API rejects any template *parameter* value containing
a literal newline, tab, or 5+ consecutive spaces with a generic error
(132018) that gives no indication of which parameter caused it. These tests
pin the sanitizer's behaviour so a future change can't silently reintroduce
that failure mode.
"""
from autosend.integrations.whatsapp import _build_template_payload
from autosend.integrations.whatsapp_payload import (
    build_button_components,
    sanitize_param_text,
)
from autosend.web.whatsapp_bulk import build_payload


def test_sanitize_param_text_strips_newlines_and_tabs():
    assert sanitize_param_text("Sound Engineer\nUsher") == "Sound Engineer Usher"
    assert sanitize_param_text("Sound Engineer\tUsher") == "Sound Engineer Usher"


def test_sanitize_param_text_collapses_long_runs_of_spaces():
    assert sanitize_param_text("Sound     Engineer") == "Sound Engineer"


def test_sanitize_param_text_preserves_single_spaces():
    assert sanitize_param_text("Sound Engineer") == "Sound Engineer"


def test_sanitize_param_text_strips_leading_trailing_whitespace():
    assert sanitize_param_text("  Sound Engineer  \n") == "Sound Engineer"


def test_sanitize_param_text_coerces_non_strings():
    assert sanitize_param_text(42) == "42"


def test_build_button_components_sanitizes_values():
    components = build_button_components(["ref\n123"])
    assert components[0]["parameters"][0]["text"] == "ref 123"


def test_transactional_body_payload_sanitizes_values():
    payload = _build_template_payload(
        "27821234567", "serving_reminder", ["Sound Engineer: 9 Aug\nUsher: 16 Aug"],
    )
    body = next(c for c in payload["template"]["components"] if c["type"] == "body")
    assert body["parameters"][0]["text"] == "Sound Engineer: 9 Aug Usher: 16 Aug"


def test_bulk_body_payload_sanitizes_values():
    payload = build_payload(
        "27821234567", "campaign_blast", "en", ["Line one\nLine two"],
    )
    body = next(c for c in payload["template"]["components"] if c["type"] == "body")
    assert body["parameters"][0]["text"] == "Line one Line two"
