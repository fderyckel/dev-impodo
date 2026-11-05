"""Submit the real Match data correction form through the owning HTTP commands."""

import re
from html import unescape
from html.parser import HTMLParser

from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from tests.support.browser_scenarios import POST_HEADERS
from tests.support.database_probe import count_duckdb_statements


class MappingForm(HTMLParser):
    """Read successful form controls, preserving repeated names and selected options."""

    def __init__(self, markup, *, form_attribute="data-mapping-form"):
        super().__init__()
        self.active = False
        self.entries = []
        self.action = ""
        self.select = None
        self.options = []
        self.textarea = None
        self.form_attribute = form_attribute
        self.feed(markup)
        assert self.action, f"The form with {form_attribute} must be rendered"

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form":
            self.active = self.form_attribute in values
            if self.active:
                self.action = values["action"]
        if not self.active or "disabled" in values:
            return
        name = values.get("name")
        if tag == "input" and name:
            kind = values.get("type", "text")
            if kind in {"button", "submit", "file"}:
                return
            if (
                kind in {"checkbox", "radio"}
                and "checked" not in values
                and name != "warning_acknowledgement"
            ):
                return
            self.entries.append(
                [name, values.get("value", "on" if kind == "checkbox" else "")]
            )
        elif tag == "select" and name:
            self.select = (name, "multiple" in values)
            self.options = []
        elif tag == "option" and self.select:
            self.options.append((values.get("value", ""), "selected" in values))
        elif tag == "textarea" and name:
            self.textarea = [name, ""]

    def handle_data(self, data):
        if self.active and self.textarea is not None:
            self.textarea[1] += data

    def handle_endtag(self, tag):
        if tag == "select" and self.select:
            name, multiple = self.select
            selected = [value for value, checked in self.options if checked]
            if not selected and not multiple and self.options:
                selected = [self.options[0][0]]
            self.entries.extend([name, value] for value in selected)
            self.select = None
        elif tag == "textarea" and self.textarea is not None:
            self.entries.append(self.textarea)
            self.textarea = None
        elif tag == "form":
            self.active = False

    def replace_source(self, prefix, original, reviewed):
        matches = [
            item
            for item in self.entries
            if item[0].startswith(prefix) and item[1] == original
        ]
        assert len(matches) == 1, matches
        matches[0][1] = reviewed


def correct_bom_through_browser(fixture, workspace_id, columns, *, two_problems=False):
    """Follow the saved root-cause links, save new providers, and confirm matches."""

    with count_duckdb_statements() as statements, collect_duckdb_request_timings() as timings:
        summary = fixture.client.get(f"/workspaces/{workspace_id}/summary")
    fixture.assertEqual(summary.status_code, 200, summary.text)
    # The measured real summary includes authorization and foundation owners
    # before its retained workspace projection. Resolving a rule adds no owner.
    fixture.assertLessEqual(timings.connection_count, 6)
    fixture.assertLessEqual(timings.schema_check_count, 4)
    fixture.assertEqual(
        sum("duckdb_columns()" in statement for statement in statements),
        timings.schema_check_count,
    )
    fixture.assertEqual(timings.lock_retry_count, 0)
    links = [unescape(value) for value in re.findall(r'href="([^"]+)"', summary.text)]
    corrections = [
        (
            "relationship-",
            "product_id",
            "relation_source_",
            "Component",
            "ReviewedComponent",
        )
    ]
    if two_problems:
        corrections.append(
            ("scalar-", "product_qty", "scalar_source_", "Quantity", "ReviewedQuantity")
        )
    for anchor, field, prefix, original, reviewed in corrections:
        candidates = [
            link
            for link in links
            if "/mapping?" in link and anchor in link and link.endswith("-" + field)
        ]
        fixture.assertEqual(len(candidates), 1, links)
        url = candidates[0]
        page = fixture.client.get(url)
        fixture.assertEqual(page.status_code, 200, page.text)
        form = MappingForm(page.text)
        form.replace_source(
            prefix, columns["lines"][original], columns["lines"][reviewed]
        )
        checked = fixture.client.post(
            form.action,
            json={"entries": [*form.entries, ["action", "draft"]]},
            headers={**POST_HEADERS, "X-CSRF-Token": fixture.csrf},
        )
        fixture.assertEqual(checked.status_code, 200, checked.text)
    form = MappingForm(fixture.client.get(url).text)
    confirmed = fixture.client.post(
        form.action,
        json={"entries": [*form.entries, ["action", "submit"]]},
        headers={**POST_HEADERS, "X-CSRF-Token": fixture.csrf},
    )
    fixture.assertEqual(confirmed.status_code, 200, confirmed.text)
