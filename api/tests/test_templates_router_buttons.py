"""Unit tests for templates_router.py's button validation.

These exercise _validate_buttons() directly rather than going through
TestClient/HTTP: unlike the tenant-boundary tests, this is pure request-shape
validation with no DB or tenant scoping involved (the router is a thin proxy
to Meta's Graph API - see its module docstring), so a full app/HTTP round
trip would only add Graph API mocking without covering anything more.
"""
import pytest
from fastapi import HTTPException

from autosend.web.templates_router import ButtonIn, _validate_buttons


def test_two_voice_call_buttons_rejected():
    buttons = [
        ButtonIn(type="VOICE_CALL", text="Call us"),
        ButtonIn(type="VOICE_CALL", text="Call again"),
    ]
    with pytest.raises(HTTPException) as exc_info:
        _validate_buttons(buttons)
    assert exc_info.value.status_code == 400
    assert "Call on WhatsApp" in exc_info.value.detail


def test_one_voice_call_button_accepted():
    buttons = [ButtonIn(type="VOICE_CALL", text="Call us")]
    _validate_buttons(buttons)  # should not raise
