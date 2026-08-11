from __future__ import annotations

import json
import unittest

from starlette.datastructures import FormData

from impodo.web.presenters.mapping_forms import _text_steps_from_form


class OrderedTextStepFormTests(unittest.TestCase):
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
        self.assertIsNone(_text_steps_from_form(FormData(), "steps"))


if __name__ == "__main__":
    unittest.main()
