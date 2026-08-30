from __future__ import annotations

import unittest

from impodo.domain.odoo.html import odoo_html_values_equal


class OdooHtmlValueTests(unittest.TestCase):
    def test_canonicalizes_html_serialization_without_erasing_markup(self) -> None:
        self.assertTrue(
            odoo_html_values_equal(
                "Assembly-ready & safe",
                "<P>Assembly-ready &amp; safe</P>",
            )
        )
        self.assertTrue(
            odoo_html_values_equal(
                '<p class="note" title="A &amp; B">Text<br></p>',
                '<P title="A &#38; B" class="note">Text<BR /></P>',
            )
        )
        self.assertFalse(
            odoo_html_values_equal("<strong>Text</strong>", "Text")
        )
        self.assertFalse(
            odoo_html_values_equal("<p>Expected</p>", "<p>Changed</p>")
        )


if __name__ == "__main__":
    unittest.main()
