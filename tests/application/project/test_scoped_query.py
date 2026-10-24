"""Test the local adapter for Organization-scoped Project discovery."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock
from uuid import uuid4

from impodo.adapters.duckdb.scoped_project_query import (
    LocalScopedMigrationProjectQuery,
)
from impodo.domain.project.foundation import MigrationNotFoundError
from impodo.domain.shared.access import Capability, LOCAL_ACTOR
from impodo.domain.shared.tenancy import (
    AuthorizationReceipt,
    OrganizationId,
    ProjectVisibilityScope,
    RequestOrganizationScope,
)
from impodo.web.composition.local_identity import (
    LOCAL_ORGANIZATION_ID,
    LocalRequestIdentityResolver,
)


NOW = datetime(2026, 10, 23, 9, 0, tzinfo=timezone.utc)


class LocalScopedProjectQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = MagicMock()
        self.query = LocalScopedMigrationProjectQuery(
            self.repository,
            organization_id=LOCAL_ORGANIZATION_ID,
            clock=lambda: NOW,
        )
        self.identity = LocalRequestIdentityResolver(
            clock=lambda: NOW,
        ).resolve({"authenticated": True})

    def test_local_list_delegates_once_for_the_implicit_scope(self) -> None:
        expected = (MagicMock(), MagicMock())
        self.repository.list_project_summaries.return_value = expected

        result = self.query.list_visible_project_summaries(
            scope=self.identity.organization_scope,
            authorization=self.identity.authorization,
        )

        self.assertEqual(result, expected)
        self.repository.list_project_summaries.assert_called_once_with()

    def test_foreign_scope_is_denied_before_the_repository_opens(self) -> None:
        project_id = str(uuid4())
        foreign = OrganizationId("foreign-organization")
        scope = RequestOrganizationScope(
            organization_id=foreign,
            principal_id="foreign-user",
        )
        receipt = AuthorizationReceipt(
            receipt_id="foreign-authorization",
            principal_id="foreign-user",
            actor_identity=LOCAL_ACTOR.identity,
            organization_id=foreign,
            capabilities=frozenset({Capability.PROJECT_VIEW}),
            project_visibility=ProjectVisibilityScope(
                organization_id=foreign,
                organization_wide=True,
            ),
            role_revision_ids=("foreign-role",),
            grant_revision_ids=("foreign-grant",),
            policy_revision="foreign-policy",
            issued_at=NOW,
            expires_at=NOW + timedelta(hours=1),
        )

        with self.assertRaisesRegex(MigrationNotFoundError, "Project not found"):
            self.query.get_visible_project(
                project_id,
                scope=scope,
                authorization=receipt,
            )

        self.repository.get_project.assert_not_called()

    def test_required_capability_is_denied_before_the_repository_opens(
        self,
    ) -> None:
        project_id = str(uuid4())
        view_only = replace(
            self.identity.authorization,
            capabilities=frozenset({Capability.PROJECT_VIEW}),
        )

        with self.assertRaisesRegex(MigrationNotFoundError, "Project not found"):
            self.query.get_visible_project(
                project_id,
                scope=self.identity.organization_scope,
                authorization=view_only,
                capability=Capability.PROJECT_DELETE,
            )

        self.repository.get_project.assert_not_called()


if __name__ == "__main__":
    unittest.main()
