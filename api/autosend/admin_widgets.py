"""Generic sqladmin form/widget helpers - no dependency on our models, so
these can be reused by any ModelView in admin_views.py."""
from markupsafe import Markup
from sqladmin.fields import QuerySelectMultipleField

# A tick-box row is much easier to scan than a True/False dropdown - used
# below via form_overrides for every nullable Boolean column (SQLAdmin's
# default ModelConverter renders those as a True/False SelectField rather
# than a native checkbox; only NOT NULL Boolean columns get a checkbox
# automatically - see sqladmin/forms.py:conv_boolean).
# IMPORTANT: this must return a *new* dict on every call, not a shared
# constant - sqladmin's get_model_form() does
# `field_args = form_args.get(name, {})` and then mutates that same dict
# in place (`field_args["name"] = name`) for whichever field it belongs
# to. A single shared dict reused across several fields (or several
# ModelViews) would have its contents overwritten by whichever field
# processed last, corrupting the others.
def _checkbox_render_kw() -> dict:
    return {"render_kw": {"class": "form-check-input"}}


class CheckboxListWidget:
    """Renders a QuerySelectMultipleField as a stack of checkboxes instead
    of a <select multiple> box - one tick box per related row, all visible
    at once instead of hidden behind a multi-select control."""

    def __call__(self, field, **kwargs):
        html = [f'<div class="d-flex flex-column gap-2" id="{field.id}">']
        for value, label, checked, _render_kw in field.iter_choices():
            choice_id = f"{field.id}-{value}"
            checked_attr = "checked" if checked else ""
            html.append(
                f'<label class="form-check d-flex align-items-center gap-2 mb-0" for="{choice_id}">'
                f'<input type="checkbox" class="form-check-input" name="{field.name}" '
                f'id="{choice_id}" value="{value}" {checked_attr}>'
                f'<span class="form-check-label mb-0">{label}</span>'
                f"</label>"
            )
        html.append("</div>")
        return Markup("\n".join(html))


class CheckboxQuerySelectMultipleField(QuerySelectMultipleField):
    widget = CheckboxListWidget()
