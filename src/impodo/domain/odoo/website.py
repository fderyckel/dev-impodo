"""Compare the default HTTP scheme applied to Contact websites by Odoo."""

from urllib.parse import urlsplit


def matches_default_contact_website(
    model: str, field: str, prepared: object, observed: object
) -> bool:
    """Accept only Odoo's exact default scheme addition on its Website field."""

    if (
        (model, field) != ("res.partner", "website")
        or not isinstance(prepared, str)
        or not isinstance(observed, str)
        or not prepared
    ):
        return False
    prefix = "http:" if prepared.startswith("//") else "http://"
    if observed != prefix + prepared or any(char.isspace() for char in prepared):
        return False
    try:
        return not urlsplit(prepared).scheme and bool(urlsplit(observed).hostname)
    except ValueError:
        return False
