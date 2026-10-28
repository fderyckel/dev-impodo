"""Verify authorization before Project catalogue repository access."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import unittest
from unittest.mock import Mock

from impodo.application.project.catalogue_service import ProjectCatalogueService
from impodo.domain.project.catalogue import (
    ProjectCatalogueListQuery,
    ProjectCatalogueNotFoundError,
    ProjectCataloguePage,
    ProjectCatalogueRecord,
    ProjectCatalogueState,
)
from impodo.domain.project.models import (
    MigrationDataClassification,
    MigrationProjectStatus,
)
from impodo.domain.shared.access import (
    ActorIdentity,
    AuthorizationError,
    Capability,
)
from impodo.domain.shared.tenancy import (
    AuthorizationReceipt,
    OrganizationId,
    ProjectVisibilityScope,
    RequestOrganizationScope,
)


NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
ORGANIZATION = OrganizationId("fictional-organization")
FOREIGN_ORGANIZATION = OrganizationId("foreign-organization")
PROJECT_ID = "10000000-0000-4000-8000-000000000001"
OTHER_PROJECT_ID = "10000000-0000-4000-8000-000000000002"


def _record() -> ProjectCatalogueRecord:
    return ProjectCatalogueRecord(
        organization_id=ORGANIZATION,
        project_id=PROJECT_ID,
        display_name="Fictional migration",
        status=MigrationProjectStatus.DRAFT,
        data_classification=MigrationDataClassification.CONFIDENTIAL,
        retention_days=365,
        optimistic_revision=1,
        catalogue_state=ProjectCatalogueState.PENDING,
        artifact_root_key=None,
        current_receipt_hash=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _scope(
    organization_id: OrganizationId = ORGANIZATION,
    *,
    principal_id: str = "fictional-user",
) -> RequestOrganizationScope:
    return RequestOrganizationScope(
        organization_id=organization_id,
        principal_id=principal_id,
    )


def _authorization(
    *,
    capabilities: frozenset[Capability] = frozenset(Capability),
    organization_id: OrganizationId = ORGANIZATION,
    principal_id: str = "fictional-user",
    project_ids: frozenset[str] = frozenset({PROJECT_ID}),
    organization_wide: bool = False,
    expires_at: datetime | None = None,
) -> AuthorizationReceipt:
    return AuthorizationReceipt(
        receipt_id="authorization-001",
        principal_id=principal_id,
        actor_identity=ActorIdentity(
            issuer="urn:impodo:test",
            subject_id=principal_id,
            display_name="Fictional user",
        ),
        organization_id=organization_id,
        capabilities=capabilities,
        project_visibility=ProjectVisibilityScope(
            organization_id=organization_id,
            project_ids=project_ids,
            organization_wide=organization_wide,
        ),
        role_revision_ids=("role-001",),
        grant_revision_ids=("grant-001",),
        policy_revision="policy-001",
        issued_at=NOW - timedelta(minutes=1),
        expires_at=expires_at,
    )


class ProjectCatalogueServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = Mock()
        self.service = ProjectCatalogueService(
            self.repository,
            clock=lambda: NOW,
        )
        self.scope = _scope()
        self.authorization = _authorization()

    def test_create_requires_matching_scope_before_one_repository_call(self) -> None:
        record = _record()
        self.repository.create.return_value = record

        created = self.service.create(
            record,
            scope=self.scope,
            authorization=self.authorization,
        )

        self.assertEqual(created, record)
        self.repository.create.assert_called_once_with(record)
        self.repository.reset_mock()
        with self.assertRaisesRegex(AuthorizationError, "not authorized"):
            self.service.create(
                replace(record, organization_id=FOREIGN_ORGANIZATION),
                scope=self.scope,
                authorization=self.authorization,
            )
        self.repository.create.assert_not_called()

    def test_get_denials_are_one_opaque_not_found_without_repository_access(
        self,
    ) -> None:
        cases = (
            ("invalid identity", "not-a-uuid", self.scope, self.authorization),
            (
                "invisible Project",
                OTHER_PROJECT_ID,
                self.scope,
                self.authorization,
            ),
            (
                "foreign Organization",
                PROJECT_ID,
                _scope(FOREIGN_ORGANIZATION),
                self.authorization,
            ),
            (
                "wrong principal",
                PROJECT_ID,
                _scope(principal_id="other-user"),
                self.authorization,
            ),
            (
                "expired receipt",
                PROJECT_ID,
                self.scope,
                _authorization(expires_at=NOW),
            ),
            (
                "missing capability",
                PROJECT_ID,
                self.scope,
                _authorization(capabilities=frozenset()),
            ),
        )
        for name, project_id, scope, authorization in cases:
            with self.subTest(case=name):
                with self.assertRaisesRegex(
                    ProjectCatalogueNotFoundError,
                    "^Project not found$",
                ):
                    self.service.get(
                        project_id,
                        scope=scope,
                        authorization=authorization,
                    )
        self.repository.get.assert_not_called()

    def test_get_uses_scope_and_project_identity_once(self) -> None:
        record = _record()
        self.repository.get.return_value = record

        result = self.service.get(
            PROJECT_ID,
            scope=self.scope,
            authorization=self.authorization,
        )

        self.assertEqual(result, record)
        self.repository.get.assert_called_once_with(ORGANIZATION, PROJECT_ID)

    def test_list_passes_only_verified_visibility_and_one_bounded_query(self) -> None:
        query = ProjectCatalogueListQuery(page_size=25)
        expected = ProjectCataloguePage(records=(_record(),), next_cursor=None)
        self.repository.list_visible.return_value = expected

        result = self.service.list_visible(
            query,
            scope=self.scope,
            authorization=self.authorization,
        )

        self.assertEqual(result, expected)
        self.repository.list_visible.assert_called_once_with(
            self.authorization.project_visibility,
            query,
        )

    def test_list_rejects_mismatched_or_expired_receipts_before_storage(self) -> None:
        for authorization in (
            _authorization(principal_id="other-user"),
            _authorization(organization_id=FOREIGN_ORGANIZATION),
            _authorization(expires_at=NOW),
            _authorization(capabilities=frozenset()),
        ):
            with self.subTest(receipt=authorization.receipt_id):
                with self.assertRaisesRegex(AuthorizationError, "not authorized"):
                    self.service.list_visible(
                        ProjectCatalogueListQuery(),
                        scope=self.scope,
                        authorization=authorization,
                    )
        self.repository.list_visible.assert_not_called()

    def test_mutations_require_their_exact_capability_and_call_storage_once(
        self,
    ) -> None:
        record = _record()
        self.repository.rename.return_value = record
        self.repository.update_lifecycle_summary.return_value = record
        self.repository.transition_deletion_state.return_value = record

        self.service.rename(
            PROJECT_ID,
            scope=self.scope,
            authorization=_authorization(
                capabilities=frozenset({Capability.PROJECT_EDIT})
            ),
            expected_revision=1,
            display_name="Renamed migration",
            updated_at=NOW,
        )
        self.service.update_lifecycle_summary(
            PROJECT_ID,
            scope=self.scope,
            authorization=_authorization(
                capabilities=frozenset({Capability.PROJECT_EDIT})
            ),
            expected_revision=1,
            status=MigrationProjectStatus.ACTIVE,
            catalogue_state=ProjectCatalogueState.REPAIR_REQUIRED,
            artifact_root_key=None,
            current_receipt_hash=None,
            updated_at=NOW,
        )
        self.service.transition_deletion_state(
            PROJECT_ID,
            scope=self.scope,
            authorization=_authorization(
                capabilities=frozenset({Capability.PROJECT_DELETE})
            ),
            expected_revision=1,
            target_state=ProjectCatalogueState.DELETING,
            occurred_at=NOW,
        )

        self.repository.rename.assert_called_once()
        self.repository.update_lifecycle_summary.assert_called_once()
        self.repository.transition_deletion_state.assert_called_once()

    def test_edit_or_delete_denial_is_opaque_and_opens_no_repository(self) -> None:
        view_only = _authorization(
            capabilities=frozenset({Capability.PROJECT_VIEW})
        )

        with self.assertRaisesRegex(
            ProjectCatalogueNotFoundError,
            "^Project not found$",
        ):
            self.service.rename(
                PROJECT_ID,
                scope=self.scope,
                authorization=view_only,
                expected_revision=1,
                display_name="Denied rename",
                updated_at=NOW,
            )
        with self.assertRaisesRegex(
            ProjectCatalogueNotFoundError,
            "^Project not found$",
        ):
            self.service.transition_deletion_state(
                PROJECT_ID,
                scope=self.scope,
                authorization=view_only,
                expected_revision=1,
                target_state=ProjectCatalogueState.DELETING,
                occurred_at=NOW,
            )

        self.repository.rename.assert_not_called()
        self.repository.transition_deletion_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
