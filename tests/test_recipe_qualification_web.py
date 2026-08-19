"""Verify the focused R4 browser surface without changing the workspace flow."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from impodo.secrets import MemorySecretStore
from impodo.access import LOCAL_ACTOR
from impodo.domain.serialization import content_hash
from impodo.recipes import DataVersionPurpose
from impodo.web.app import create_local_app


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "recipes" / "phase-r0" / "customer-recipe-v3.json"
POST_HEADERS = {
    "Origin": "http://testserver",
    "Sec-Fetch-Site": "same-origin",
}


class RecipeQualificationWebTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.app = create_local_app(
            self.temporary.name,
            launch_token="launch-secret",
            session_secret="session-secret",
            secret_store=MemorySecretStore(),
            preparation_jobs_enabled=False,
            odoo_capture_jobs_enabled=False,
        )
        self.client = TestClient(self.app)
        self.client.get("/launch?token=launch-secret")
        recipes = self.client.get("/recipes")
        self.csrf = re.search(
            r'name="csrf_token" value="([^"]+)"',
            recipes.text,
        ).group(1)
        created = self.client.post(
            "/recipes/new",
            data={
                "csrf_token": self.csrf,
                "creation_request_id": str(uuid4()),
                "name": "Customer rollout recipe",
                "source_system": "CSV export",
                "source_mode": "FILE",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )
        self.assertEqual(created.status_code, 303)
        self.recipe_url = created.headers["location"]

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def test_qualification_is_a_thin_recipe_surface_over_existing_stages(self) -> None:
        overview = self.client.get(self.recipe_url)
        qualification = self.client.get(f"{self.recipe_url}/qualification")

        self.assertEqual(overview.status_code, 200)
        self.assertIn("Complete Recipe setup", overview.text)
        self.assertEqual(qualification.status_code, 200)
        self.assertIn("Recipe Test qualification", qualification.text)
        self.assertIn("Complete the exact Test rehearsal", qualification.text)
        self.assertIn("Publish the reusable Recipe rules", qualification.text)
        self.assertNotIn("Production API key", qualification.text)

        blocked = self.client.post(
            f"{self.recipe_url}/qualify",
            data={
                "csrf_token": self.csrf,
                "expected_recipe_revision": "2",
                "create_count": "0",
                "update_count": "0",
                "unchanged_count": "0",
                "verified_count": "0",
            },
            headers=POST_HEADERS,
        )
        self.assertEqual(blocked.status_code, 422)
        self.assertIn("Complete the exact Test rehearsal", blocked.text)

    def test_selected_revision_starts_a_clean_production_data_version(self) -> None:
        context = self.app.state.context
        recipe_id = self.recipe_url.rsplit("/", 1)[-1]
        recipe = context.recipes.get(recipe_id, actor=LOCAL_ACTOR)
        data_version = context.recipes.data_versions(
            recipe_id,
            actor=LOCAL_ACTOR,
        )[0]
        envelope = deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))
        envelope["provenance"]["recipe_id"] = recipe_id
        envelope["provenance"]["recipe_revision"] = 1
        envelope["payload_hash"] = content_hash(
            {key: value for key, value in envelope.items() if key != "payload_hash"}
        )
        context.recipes.publish_revision(
            recipe_id,
            expected_recipe_revision=recipe.optimistic_revision,
            envelope_bytes=json.dumps(envelope).encode("utf-8"),
            actor=LOCAL_ACTOR,
        )
        application_id = str(uuid4())
        application_hash = "sha256:" + "1" * 64
        target_binding_hash = "sha256:" + "2" * 64
        context.recipes.record_application_projection(
            actor=LOCAL_ACTOR,
            application_id=application_id,
            recipe_id=recipe_id,
            recipe_revision=1,
            data_version_id=data_version.data_version_id,
            workspace_project_id=data_version.workspace_project_id,
            source_selection_hash="sha256:" + "3" * 64,
            parameter_values_hash="sha256:" + "4" * 64,
            target_binding_hash=target_binding_hash,
            credential_generation="test-read-generation",
            binding_hash="sha256:" + "5" * 64,
            issue_hash="sha256:" + "6" * 64,
            mapping_id=str(uuid4()),
            mapping_content_hash="sha256:" + "7" * 64,
            status="APPLIED",
            evidence_storage_key="protected/test-application",
            evidence_hash=application_hash,
            created_at=datetime.now(timezone.utc),
        )
        recipe = context.recipes.get(recipe_id, actor=LOCAL_ACTOR)
        qualification = context.recipes.publish_qualification(
            recipe_id,
            expected_recipe_revision=recipe.optimistic_revision,
            evidence={
                "application_evidence_hash": application_hash,
                "application_id": application_id,
                "comparison_hash": "sha256:" + "8" * 64,
                "control_hash": "sha256:" + "9" * 64,
                "data_version_id": data_version.data_version_id,
                "environment": "TEST",
                "execution_hash": "sha256:" + "a" * 64,
                "findings": [],
                "preparation_hash": "sha256:" + "b" * 64,
                "quality_hash": "sha256:" + "c" * 64,
                "read_back_hash": "sha256:" + "d" * 64,
                "recipe_revision": 1,
                "reconciliation_hash": "sha256:" + "e" * 64,
                "status": "TEST_QUALIFIED",
                "test_target_binding_hash": target_binding_hash,
                "workspace_project_id": data_version.workspace_project_id,
            },
            actor=LOCAL_ACTOR,
        )
        recipe = context.recipes.get(recipe_id, actor=LOCAL_ACTOR)
        context.recipes.select_cutover_candidate(
            recipe_id,
            expected_recipe_revision=recipe.optimistic_revision,
            recipe_revision=1,
            qualification_id=str(qualification.detail["qualification_id"]),
            qualification_evidence_hash=str(qualification.detail["evidence_hash"]),
            actor=LOCAL_ACTOR,
        )
        recipe = context.recipes.get(recipe_id, actor=LOCAL_ACTOR)
        candidate = context.recipes.cutover_candidate(recipe_id, actor=LOCAL_ACTOR)

        form = self.client.get(f"{self.recipe_url}/production")
        self.assertEqual(form.status_code, 200)
        self.assertIn("Run with latest data", form.text)
        self.assertIn("Production access starts empty", form.text)
        self.assertNotIn('name="read_api_key"', form.text)
        created = self.client.post(
            f"{self.recipe_url}/production",
            data={
                "csrf_token": self.csrf,
                "expected_recipe_revision": str(recipe.optimistic_revision),
                "expected_cutover_candidate_id": candidate.cutover_candidate_id,
                "label": "Latest Customer Production export",
                "parameter__parameter:batch_reference": "ROLLOUT-001",
                "parameter__parameter:export_as_of_date": "2026-08-19",
                "control__control:customers.open_balance": "1250.00",
            },
            headers=POST_HEADERS,
            follow_redirects=False,
        )

        self.assertEqual(created.status_code, 303)
        self.assertRegex(created.headers["location"], r"/projects/.+/files")
        current_recipe = context.recipes.get(recipe_id, actor=LOCAL_ACTOR)
        current = next(
            item
            for item in context.recipes.data_versions(recipe_id, actor=LOCAL_ACTOR)
            if item.data_version_id == current_recipe.current_data_version_id
        )
        project = context.queries.get(current.workspace_project_id)
        self.assertEqual(current.purpose, DataVersionPurpose.PRODUCTION)
        self.assertEqual(current.pinned_recipe_revision, 1)
        self.assertIsNone(project.odoo_connection_mode)
        self.assertFalse(project.odoo_base_url)
        self.assertFalse(project.odoo_database)
        self.assertIsNone(
            context.recipe_applications.applications.get_target_binding(
                project.project_id
            )
        )
        self.assertIsNone(
            context.recipe_applications.schemas.get_odoo_schema_catalog(
                project.project_id
            )
        )
        self.assertIsNone(
            context.recipe_applications.sources.get_source_selection(
                project.project_id
            )
        )
        overview = self.client.get(self.recipe_url)
        self.assertIn("Complete Production data setup", overview.text)
        target = self.client.get(f"/projects/{project.project_id}/target")
        self.assertIn("Connect the Production Odoo server", target.text)
        self.assertIn("Test access is never reused", target.text)
        self.assertIn('name="read_api_key"', target.text)


if __name__ == "__main__":
    unittest.main()
