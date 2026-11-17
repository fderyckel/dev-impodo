"""Keep button purpose explicit and the next-step colour centrally owned."""

import re
import unittest

from jinja2 import Environment

from tests.support.paths import REPOSITORY_ROOT

WEB = REPOSITORY_ROOT / "src" / "impodo" / "web"
ROLES = {"action", "navigation", "help", "destructive"}


class ButtonRoleContractTests(unittest.TestCase):
    def test_templates_use_explicit_roles_without_assigning_the_active_colour(self):
        environment = Environment()
        for path in (WEB / "templates").rglob("*.html"):
            with self.subTest(template=path.name):
                text = path.read_text(encoding="utf-8")
                environment.parse(text)
                self.assertNotIn("data-next-active", text)
                for classes in re.findall(r'class="([^"]+)"', text):
                    names = set(classes.split())
                    if "button" in names:
                        self.assertEqual(len(ROLES & names), 1, classes)
                        self.assertFalse({"primary", "secondary", "danger"} & names)

    def test_red_button_fills_are_owned_by_the_single_selected_next_action(self):
        for path in (WEB / "static").glob("*.css"):
            text = path.read_text(encoding="utf-8")
            for selector, declarations in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
                if ".button" in selector and re.search(
                    r"background(?:-color)?:\s*var\(--impodo-red(?:-dark)?\)",
                    declarations,
                ):
                    self.assertIn("data-next-active", selector, path.name)

    def test_every_page_loads_the_shared_presentation_only_coordinator(self):
        base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn("/button-roles.js", base)
        script = (WEB / "static" / "button-roles.js").read_text(encoding="utf-8")
        for operation in ("fetch(", ".submit(", ".requestSubmit(", "location.assign("):
            self.assertNotIn(operation, script)
