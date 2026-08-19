"""Verify the focused R4 browser surface without changing the workspace flow."""

from __future__ import annotations

import re
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from impodo.secrets import MemorySecretStore
from impodo.web.app import create_local_app


ROOT = Path(__file__).resolve().parents[1]
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


if __name__ == "__main__":
    unittest.main()
