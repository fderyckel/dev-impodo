"""Guard workflow-documentation coverage and durable repository references."""

from configparser import ConfigParser
from pathlib import Path
import unittest

import yaml

from scripts.documentation_quality import (
    render_report,
    resolve_code_reference,
    validate_repository,
)


ROOT = Path(__file__).resolve().parents[1]


class DocumentationQualityTests(unittest.TestCase):
    """Keep both audience paths synchronized with code ownership."""

    def test_repository_documentation_has_no_objective_drift(self) -> None:
        issues = validate_repository(ROOT)

        self.assertEqual(tuple(issue.render() for issue in issues), ())

    def test_coverage_report_is_deterministic_and_complete(self) -> None:
        first = render_report(ROOT)
        second = render_report(ROOT)

        self.assertEqual(first, second)
        self.assertIn("| Project setup | yes | yes |", first)
        self.assertIn("| Load into Odoo | yes | yes |", first)
        self.assertEqual(first.count("| yes | yes |"), 7)

    def test_code_reference_resolves_exact_symbols(self) -> None:
        self.assertTrue(
            resolve_code_reference(
                ROOT,
                "src/impodo/projects.py::ProjectService.register",
            )
        )
        self.assertFalse(
            resolve_code_reference(
                ROOT,
                "src/impodo/projects.py::ProjectService.not_a_method",
            )
        )

    def test_advisory_vale_configuration_is_well_formed(self) -> None:
        configuration = ConfigParser()
        vale_path = ROOT / ".vale.ini"
        configuration.read_string(
            "[DEFAULT]\n" + vale_path.read_text(encoding="utf-8"),
            source=str(vale_path),
        )

        self.assertEqual(configuration.defaults()["stylespath"], "docs/styles")
        self.assertIn("docs/user/**/*.md", configuration)
        self.assertIn("docs/developer/**/*.md", configuration)
        styles = sorted((ROOT / "docs/styles").glob("*/*.yml"))
        self.assertTrue(styles)
        for path in styles:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertIsInstance(payload, dict, path)
            self.assertIn("extends", payload, path)
            self.assertIn("message", payload, path)


if __name__ == "__main__":
    unittest.main()
