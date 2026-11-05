"""Read hidden source-authoring form values independently of HTML attribute order."""

from html.parser import HTMLParser


class _SourceForms(HTMLParser):
    def __init__(self, markup: str) -> None:
        super().__init__()
        self.forms: dict[str, dict[str, str]] = {}
        self.current: dict[str, str] | None = None
        self.feed(markup)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self.current = {}
            self.forms[attributes.get("action") or ""] = self.current
        elif tag == "input" and self.current is not None:
            name = attributes.get("name")
            if name and attributes.get("type") == "hidden":
                self.current[name] = attributes.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None


def hidden_source_form_value(markup: str, action: str, name: str) -> str:
    """Return the named hidden value from the requested source command form."""
    forms = _SourceForms(markup).forms
    if action not in forms:
        raise AssertionError(f"Form {action} was not rendered")
    if name not in forms[action]:
        raise AssertionError(f"Field {name} was not rendered")
    return forms[action][name]
