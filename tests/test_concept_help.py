from __future__ import annotations

from pathlib import Path
import re
import shutil
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from impodo.adapters.protected_evidence.credential_vault import MemorySecretStore
from impodo.web.app import create_local_app
from impodo.web.presenters.concepts import CONCEPTS, CONCEPTS_BY_SLUG


ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "impodo" / "web" / "templates"


class ConceptRegistryTests(unittest.TestCase):
    def test_slugs_and_related_concepts_are_complete(self) -> None:
        slugs = tuple(concept.slug for concept in CONCEPTS)

        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(set(CONCEPTS_BY_SLUG), set(slugs))
        for concept in CONCEPTS:
            self.assertTrue(concept.definition)
            self.assertTrue(concept.relationship)
            self.assertTrue(concept.exclusion)
            self.assertTrue(concept.example)
            self.assertTrue(concept.practical_effect)
            self.assertTrue(concept.related_slugs)
            self.assertNotIn(concept.slug, concept.related_slugs)
            self.assertLessEqual(set(concept.related_slugs), set(slugs))

    def test_normal_template_copy_avoids_internal_concept_names(self) -> None:
        forbidden = (
            "DataVersion",
            "MigrationProject",
            "MigrationWorkspace",
            "source package",
            "data package",
            "Cutover Plan",
        )

        for template in sorted(TEMPLATES.glob("*.html")):
            source = template.read_text(encoding="utf-8")
            for phrase in forbidden:
                self.assertNotIn(phrase, source, f"{template.name}: {phrase}")

            visible_fragments = " ".join(re.findall(r">([^<>]+)<", source))
            self.assertNotRegex(
                visible_fragments,
                r"(?i)\bpublish(?:ed|ing)?\b|\bpublication\b",
                template.name,
            )


class ConceptHelpBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT / ".tmp" / f"concept-help-{uuid4()}"
        self.root.mkdir(parents=True)
        self.app = create_local_app(
            self.root,
            launch_token="concept-launch",
            session_secret="concept-session",
            secret_store=MemorySecretStore(),
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
        )
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _launch(self) -> None:
        response = self.client.get(
            "/launch?token=concept-launch",
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)

    def test_concepts_page_requires_the_local_browser_session(self) -> None:
        response = self.client.get("/concepts")

        self.assertEqual(response.status_code, 401)

    def test_concepts_page_has_stable_anchors_and_opens_no_state(self) -> None:
        self._launch()
        context = self.app.state.context
        migration_projects = MagicMock(wraps=context.migration_projects)
        boundary_names = (
            "connection_tester",
            "read_identity_probe",
            "write_identity_probe",
            "schema_reader",
            "model_catalog_reader",
            "readiness_reader",
            "source_capture_factory",
            "write_executor_factory",
            "readback_reader_factory",
        )
        boundary_patches = [
            patch.object(
                context,
                name,
                MagicMock(side_effect=AssertionError(f"{name} must not run")),
            )
            for name in boundary_names
        ]

        with patch.object(context, "migration_projects", migration_projects):
            for boundary_patch in boundary_patches:
                boundary_patch.start()
            try:
                response = self.client.get("/concepts")
            finally:
                for boundary_patch in reversed(boundary_patches):
                    boundary_patch.stop()

        self.assertEqual(response.status_code, 200)
        migration_projects.list.assert_not_called()
        self.assertIn('href="/concepts"', response.text)
        self.assertIn('aria-current="page"', response.text)
        for concept in CONCEPTS:
            self.assertIn(f'id="{concept.slug}"', response.text)
            self.assertIn(concept.definition, response.text)

    def test_project_list_help_is_accessible_and_keeps_one_list_query(self) -> None:
        self._launch()
        context = self.app.state.context
        for number in range(2):
            context.migration_projects.create(
                actor=context.actor,
                display_name=f"Fictional migration {number + 1}",
                migration_purpose="Verify concept help uses one list query",
                source_system_identity="Fictional ERP",
            )
        migration_projects = MagicMock(wraps=context.migration_projects)

        with patch.object(context, "migration_projects", migration_projects):
            response = self.client.get("/projects")

        self.assertEqual(response.status_code, 200)
        migration_projects.list.assert_called_once_with(actor=context.actor)
        migration_projects.get.assert_not_called()
        self.assertIn('href="/concepts#data-project"', response.text)
        self.assertIn('aria-haspopup="dialog"', response.text)
        self.assertIn('aria-controls="concept-help-dialog-data-project"', response.text)
        self.assertIn('aria-label="Explain how a data project is organized"', response.text)
        self.assertIn('id="concept-help-dialog-data-project"', response.text)
        self.assertIn('aria-labelledby="concept-help-title-data-project"', response.text)

    def test_data_project_overview_explains_workspace_and_recipe(self) -> None:
        self._launch()
        context = self.app.state.context
        bundle = context.project_authoring.create(
            actor=context.actor,
            display_name="Fictional customer migration",
            source_mode="FILE",
            creation_request_id=str(uuid4()),
        )

        response = self.client.get(f"/projects/{bundle.project.project_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("This workspace uses Data version 1", response.text)
        self.assertIn(
            "You can complete this migration once without saving a Recipe",
            response.text,
        )
        self.assertIn('href="/concepts#workspace"', response.text)
        self.assertIn('id="concept-help-dialog-workspace"', response.text)
        self.assertIn('href="/concepts#recipe"', response.text)
        self.assertIn('id="concept-help-dialog-recipe"', response.text)
        self.assertNotIn("DataVersion", response.text)
        self.assertNotRegex(response.text, r"(?i)>[^<]*publish(?:ed|ing)?[^<]*<")

    def test_dialog_script_preserves_fallback_and_returns_focus(self) -> None:
        self._launch()

        response = self.client.get("/static/app.js")

        self.assertEqual(response.status_code, 200)
        self.assertIn('typeof dialog.showModal !== "function"', response.text)
        self.assertIn("event.preventDefault()", response.text)
        self.assertIn('dialog.addEventListener("close"', response.text)
        self.assertIn("trigger.focus()", response.text)


if __name__ == "__main__":
    unittest.main()
