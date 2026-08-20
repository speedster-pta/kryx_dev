# Fixed Stitch Money base URL that build_payment_link_suffix()'s output is
# always appended to. Named here (rather than left as a docstring-only
# comment) so admin pages that need to show the full link to a staff member
# building a template - e.g. the "Calendar invite"-style presets in the
# WhatsApp template builder - have one canonical constant to import instead
# of re-typing the literal.
STITCH_BASE_URL = "https://express.stitch.money/"


def build_reference(event_name: str, first_name: str, last_name: str) -> str:
    """
    Human-typeable reference for manual EFT/Stitch entry, e.g.
    "Men's Camp" + "John" + "Doe" -> "MEN-JDoe"

    First 3 alphabetic characters of the event name (uppercased) +
    underscore + first initial + surname (spaces/punctuation stripped
    from surname so double-barrelled names stay reasonably short).
    """
    alpha_only = "".join(ch for ch in event_name if ch.isalpha())
    event_code = alpha_only[:3].upper() or "EVT"
    initial = (first_name[:1] or "").upper()
    surname = "".join(ch for ch in last_name if ch.isalpha())
    return f"{event_code}-{initial}{surname}"

def format_amount_due(total_due_cents: int) -> str:
    """Cents -> display string for {{3}}, e.g. 50000 -> "R500"."""
    rands = total_due_cents // 100
    return f"R{rands:,}"


def build_payment_link_suffix(total_due_cents: int, reference: str) -> str:
    """
    Single dynamic URL parameter for the Stitch button, appended after
    https://express.stitch.money/.

    ASSUMPTION - VERIFY with a live test message: amount is passed as
    whole Rand (no cents, no symbol), e.g. total_due_cents=50000 ->
    "500/MEN_JDoe". Adjust if Stitch's link actually expects cents.
    """
    rands = total_due_cents // 100
    return f"{rands}/{reference}"
