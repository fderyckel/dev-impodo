"""Guard workflow-documentation coverage and durable repository references."""

from configparser import ConfigParser
from pathlib import Path
import unittest

import yaml

from scripts.documentation_quality import (
    load_manifest,
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
        self.assertIn(
            "| Project and authoring workspace setup | yes | yes |",
            first,
        )
        self.assertIn("| Load into Odoo | yes | yes |", first)
        self.assertIn(
            "| Integrated multi-Recipe Test run | yes | yes |",
            first,
        )
        self.assertIn(
            "| Integrated Test qualification | yes | yes |",
            first,
        )
        self.assertEqual(first.count("| yes | yes |"), 9)

    def test_workflow_registers_documentation_standards_and_skill(self) -> None:
        manifest = load_manifest(ROOT)
        shared = manifest["shared"]

        self.assertEqual(
            shared["writing_standard"],
            "docs/style-guide.md#plain-semantic-language",
        )
        self.assertEqual(
            shared["audience_standard"],
            "docs/style-guide.md#data-manager-first-explanations",
        )
        self.assertEqual(
            shared["editing_workflow"],
            "docs/style-guide.md#documentation-editing-workflow",
        )
        self.assertIn(
            ".agents/skills/impodo-documentation/SKILL.md",
            shared["skills"],
        )

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
        expected_sections = {
            "docs/*.md",
            "docs/architecture/**/*.md",
            "docs/bpmn/**/*.md",
            "docs/decisions/**/*.md",
            "docs/developer/**/*.md",
            "docs/plans/**/*.md",
            "docs/reports/**/*.md",
            "docs/testing/**/*.md",
            "docs/user/**/*.md",
        }
        self.assertTrue(expected_sections.issubset(configuration.sections()))
        for section in expected_sections:
            styles = configuration[section]["basedonstyles"].split(",")
            self.assertIn("ImpodoPlain", {style.strip() for style in styles})

        styles = sorted((ROOT / "docs/styles").glob("*/*.yml"))
        self.assertTrue(styles)
        for path in styles:
            payload = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertIsInstance(payload, dict, path)
            self.assertIn("extends", payload, path)
            self.assertIn("message", payload, path)

        plain_style_names = {
            path.name for path in (ROOT / "docs/styles/ImpodoPlain").glob("*.yml")
        }
        self.assertTrue(
            {
                "CompressedRelationships.yml",
                "Filler.yml",
                "OdooVersion.yml",
                "PlainWords.yml",
                "ProductNames.yml",
                "Spelling.yml",
            }.issubset(plain_style_names)
        )

        user_style_names = {
            path.name for path in (ROOT / "docs/styles/ImpodoUser").glob("*.yml")
        }
        self.assertIn("DataManagerTerms.yml", user_style_names)

        spelling_vocabulary = ROOT / "docs/styles/config/ignore/Impodo.txt"
        self.assertTrue(spelling_vocabulary.is_file())
        vocabulary = [
            line.strip()
            for line in spelling_vocabulary.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(vocabulary), len(set(vocabulary)))


if __name__ == "__main__":
    unittest.main()
