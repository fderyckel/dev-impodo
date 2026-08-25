"""Verify that Authoring and Recipe-run workspaces cannot mix journeys."""

from __future__ import annotations

import unittest
from uuid import uuid4

from starlette.requests import Request

from impodo.migration_foundation import MigrationIdentifierConfusionError
from impodo.migration_runs import MigrationRunPurpose
from impodo.web.workspace_journeys import (
    WorkspaceJourney,
    classify_workspace_journey,
    enforce_workspace_journey,
    workspace_route_is_allowed,
)
from impodo.workspace_access import WorkspaceAccessContext


class WorkspaceJourneyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application_id = str(uuid4())
        self.access = WorkspaceAccessContext(
            project_id=str(uuid4()),
            workspace_id=str(uuid4()),
            data_version_id=str(uuid4()),
            migration_run_id=str(uuid4()),
            recipe_application_id=self.application_id,
        )

    def test_ownership_selects_one_exact_journey(self) -> None:
        self.assertEqual(
            classify_workspace_journey(MigrationRunPurpose.AUTHORING, None),
            WorkspaceJourney.AUTHORING,
        )
        self.assertEqual(
            classify_workspace_journey(MigrationRunPurpose.TEST, None),
            WorkspaceJourney.RECIPE_RUN_SETUP,
        )
        self.assertEqual(
            classify_workspace_journey(
                MigrationRunPurpose.PRODUCTION,
                self.application_id,
            ),
            WorkspaceJourney.RECIPE_APPLICATION,
        )
        with self.assertRaises(MigrationIdentifierConfusionError):
            classify_workspace_journey(
                MigrationRunPurpose.AUTHORING,
                self.application_id,
            )

    def test_setup_allows_inputs_but_not_authoring_or_loading(self) -> None:
        workspace_id = self.access.workspace_id
        for area in ("files", "sources", "datasets", "schema", "target"):
            self.assertTrue(
                workspace_route_is_allowed(
                    WorkspaceJourney.RECIPE_RUN_SETUP,
                    f"/workspaces/{workspace_id}/{area}",
                    workspace_id,
                )
            )
        for area in ("overview", "mapping", "prepare", "summary", "load"):
            self.assertFalse(
                workspace_route_is_allowed(
                    WorkspaceJourney.RECIPE_RUN_SETUP,
                    f"/workspaces/{workspace_id}/{area}",
                    workspace_id,
                )
            )

    def test_application_allows_review_and_load_but_not_authoring(self) -> None:
        workspace_id = self.access.workspace_id
        for area in (
            "prepare",
            "preparation",
            "resolution",
            "normalization",
            "summary",
            "load",
        ):
            self.assertTrue(
                workspace_route_is_allowed(
                    WorkspaceJourney.RECIPE_APPLICATION,
                    f"/workspaces/{workspace_id}/{area}",
                    workspace_id,
                )
            )
        for area in (
            "overview",
            "files",
            "sources",
            "schema",
            "mapping",
        ):
            self.assertFalse(
                workspace_route_is_allowed(
                    WorkspaceJourney.RECIPE_APPLICATION,
                    f"/workspaces/{workspace_id}/{area}",
                    workspace_id,
                )
            )

    def test_stale_application_url_returns_to_the_run_without_mutating(self) -> None:
        request = _request(
            f"/workspaces/{self.access.workspace_id}/mapping",
            method="POST",
        )
        response = enforce_workspace_journey(
            request,
            self.access,
            MigrationRunPurpose.TEST,
        )

        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            f"/projects/{self.access.project_id}/runs/{self.access.migration_run_id}",
        )
        self.assertIn("saved Recipe rules", request.session["flash"])

    def test_production_setup_returns_to_its_own_activation_page(self) -> None:
        setup_access = WorkspaceAccessContext(
            project_id=self.access.project_id,
            workspace_id=str(uuid4()),
            data_version_id=self.access.data_version_id,
            migration_run_id=self.access.migration_run_id,
        )
        request = _request(f"/workspaces/{setup_access.workspace_id}/overview")

        response = enforce_workspace_journey(
            request,
            setup_access,
            MigrationRunPurpose.PRODUCTION,
        )

        self.assertIsNotNone(response)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(
            response.headers["location"],
            f"/projects/{setup_access.project_id}/production-runs/"
            f"{setup_access.migration_run_id}/activate",
        )


def _request(path: str, *, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": (),
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "session": {"authenticated": True},
        }
    )


if __name__ == "__main__":
    unittest.main()
