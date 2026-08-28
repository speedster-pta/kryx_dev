"""Pure, side-effect-free WhatsApp template-payload component builders
shared between the two independent WhatsApp clients
(integrations/whatsapp.py - async, transactional; web/whatsapp_bulk.py -
sync, bulk campaigns). The clients themselves are deliberately NOT
merged (different event-loop/threading models - see whatsapp_bulk.py's
own docstring) - this module only factors out the one piece of
component-construction logic that was genuinely identical between them.
Header-component construction is NOT shared here: the two paths address
images differently (a live URL link vs. a pre-uploaded Meta media
handle), which is real divergence, not duplication.
"""
import re

# Meta's WhatsApp Cloud API rejects any template *parameter* value (not the
# template's own static text) containing a literal newline, tab, or 2+
# consecutive spaces, with the unhelpful generic error
# "132018 - There's an issue with the parameters in your template" that
# doesn't say which parameter or rule caused it. There's no supported way to
# get a real line break inside one parameter, so every parameter value is
# sanitized here rather than relying on each caller to avoid the trigger -
# this is the single choke point both build_button_components below and the
# body-parameter builders in integrations/whatsapp.py and
# web/whatsapp_bulk.py funnel through.
_PARAM_WHITESPACE_VIOLATION_RE = re.compile(r"[\n\t]+|[ ]{2,}")


def sanitize_param_text(value) -> str:
    return _PARAM_WHITESPACE_VIOLATION_RE.sub(" ", str(value)).strip()


def build_button_components(button_values: list[str | None] | None) -> list[dict]:
    """Builds one WhatsApp `button`/`url` component per non-empty entry in
    button_values, addressed by its position in the list (index 0, 1, 2...) -
    this matches how Meta identifies each dynamic URL button on a template
    by its button index. Buttons without a variable (None/empty/falsy) are
    skipped: only URL buttons with a {{1}} in their URL need a component
    at all."""
    if not button_values:
        return []
    return [
        {
            "type": "button",
            "sub_type": "url",
            "index": str(i),
            "parameters": [{"type": "text", "text": sanitize_param_text(value)}],
        }
        for i, value in enumerate(button_values)
        if value
    ]
