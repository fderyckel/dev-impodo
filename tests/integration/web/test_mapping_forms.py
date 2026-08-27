from __future__ import annotations

from tests.support.paths import REPOSITORY_ROOT

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from starlette.datastructures import FormData

from impodo.web.presenters.mapping_forms import (
    _mapping_allowed_fields,
    _text_steps_from_form,
)
from impodo.web.presenters.mapping_view import _is_phone_field


class OrderedTextStepFormTests(unittest.TestCase):
    def test_phone_quick_start_is_suggested_only_for_phone_fields(self) -> None:
        for name, label in (
            ("phone", "Phone"),
            ("mobile", "Mobile"),
            ("x_studio_telephone", "Telephone"),
            ("x_contact", "Téléphone principal"),
        ):
            with self.subTest(name=name, label=label):
                self.assertTrue(
                    _is_phone_field(
                        SimpleNamespace(name=name, label=label, type="char")
                    )
                )
        self.assertFalse(
            _is_phone_field(
                SimpleNamespace(name="email", label="Email", type="char")
            )
        )
        self.assertFalse(
            _is_phone_field(
                SimpleNamespace(name="phone", label="Phone", type="integer")
            )
        )

    def test_reads_ordered_steps_without_interpreting_business_text(self) -> None:
        payload = [
            {
                "kind": "find_replace",
                "search_value": "00",
                "replacement_value": "+",
                "search_mode": "starts_with",
                "replace_all": False,
                "characters": "",
            },
            {
                "kind": "remove_separators_between_digits",
                "search_value": "",
                "replacement_value": "",
                "search_mode": "literal",
                "replace_all": True,
                "characters": " .-",
            },
        ]
        form = FormData((("steps", json.dumps(payload)),))

        steps = _text_steps_from_form(form, "steps")

        assert steps is not None
        self.assertEqual(
            [(item.kind, item.search_mode) for item in steps],
            [
                ("find_replace", "starts_with"),
                ("remove_separators_between_digits", "literal"),
            ],
        )
        self.assertEqual(steps[1].characters, " .-")

    def test_rejects_tampered_or_oversized_step_payloads(self) -> None:
        valid = {
            "kind": "find_replace",
            "search_value": "a",
            "replacement_value": "b",
            "search_mode": "literal",
            "replace_all": True,
            "characters": "",
        }
        tampered = FormData(
            (("steps", json.dumps([{**valid, "unexpected": "value"}])),)
        )
        oversized = FormData(
            (("steps", json.dumps([valid for _index in range(21)])),)
        )

        with self.assertRaisesRegex(ValueError, "invalid"):
            _text_steps_from_form(tampered, "steps")
        with self.assertRaisesRegex(ValueError, "invalid"):
            _text_steps_from_form(oversized, "steps")
        self.assertEqual(_text_steps_from_form(FormData(), "steps"), ())

    def test_retired_single_rule_form_names_are_not_allowed(self) -> None:
        selection = SimpleNamespace(datasets=(SimpleNamespace(),))
        schema = SimpleNamespace(
            models=(
                SimpleNamespace(
                    name="res.partner",
                    fields=(SimpleNamespace(type="char"),),
                ),
            )
        )
        form = FormData((("target_model_0", "res.partner"),))

        allowed = _mapping_allowed_fields(form, selection, schema)

        self.assertIn("scalar_text_steps_0_0", allowed)
        self.assertNotIn("scalar_search_0_0", allowed)
        self.assertNotIn("scalar_replacement_0_0", allowed)
        self.assertNotIn("scalar_search_mode_0_0", allowed)
        self.assertNotIn("scalar_replace_all_0_0", allowed)

    def test_button_authored_text_steps_notify_mapping_draft_tracking(self) -> None:
        script = (
            REPOSITORY_ROOT
            / "src"
            / "impodo"
            / "web"
            / "static"
            / "mapping-value-rules.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'storage?.dispatchEvent(new Event("input", { bubbles: true }));',
            script,
        )
        self.assertEqual(script.count("notifyTextStepsChanged(builder);"), 3)

    def test_mapping_page_owns_its_page_assets(self) -> None:
        root = REPOSITORY_ROOT
        template = (
            root / "src" / "impodo" / "web" / "templates" / "mapping" / "page.html"
        ).read_text(encoding="utf-8")
        styles = (root / "src" / "impodo" / "web" / "static" / "mapping.css").read_text(
            encoding="utf-8"
        )
        script = (root / "src" / "impodo" / "web" / "static" / "mapping.js").read_text(
            encoding="utf-8"
        )

        self.assertIn("/mapping.css", template)
        self.assertIn("/mapping-editor.js", template)
        self.assertIn("/mapping-value-rules.js", template)
        self.assertIn("/mapping-catalogs.js", template)
        self.assertIn("/mapping.js", template)
        self.assertIn(".scalar-table-scroll-top", styles)
        self.assertIn(".mapping-save-state.unsaved", styles)
        self.assertIn("window.impodoMappingPosition", script)
        self.assertIn("[data-mapping-form]", script)


if __name__ == "__main__":
    unittest.main()
