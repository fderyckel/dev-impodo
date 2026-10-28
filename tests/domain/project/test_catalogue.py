"""Protect the bounded Project catalogue value contract."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import unittest

from impodo.domain.project.catalogue import (
    ProjectCatalogueCursor,
    ProjectCatalogueListQuery,
    ProjectCatalogueRecord,
    ProjectCatalogueState,
)
from impodo.domain.project.foundation import MigrationFoundationError
from impodo.domain.project.models import (
    MigrationDataClassification,
    MigrationProjectStatus,
)
from impodo.domain.shared.tenancy import OrganizationId


NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
PROJECT_ID = "10000000-0000-4000-8000-000000000001"
RECEIPT_HASH = "sha256:" + "a" * 64


def record() -> ProjectCatalogueRecord:
    return ProjectCatalogueRecord(
        organization_id=OrganizationId("organization-001"),
        project_id=PROJECT_ID,
        display_name="Customer migration",
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


class ProjectCatalogueContractTests(unittest.TestCase):
    def test_cursor_round_trip_uses_versioned_bounded_format(self) -> None:
        cursor = ProjectCatalogueCursor(updated_at=NOW, project_id=PROJECT_ID)

        token = cursor.encode()

        self.assertTrue(token.startswith("pc1."))
        self.assertLessEqual(len(token), 200)
        self.assertEqual(ProjectCatalogueCursor.decode(token), cursor)
        with self.assertRaisesRegex(MigrationFoundationError, "cursor is invalid"):
            ProjectCatalogueCursor.decode("pc2.not-supported")

    def test_visible_page_contract_bounds_filters_and_page_size(self) -> None:
        query = ProjectCatalogueListQuery(
            statuses=frozenset({MigrationProjectStatus.ACTIVE}),
            catalogue_states=frozenset({ProjectCatalogueState.READY}),
            page_size=100,
            cursor=ProjectCatalogueCursor(NOW, PROJECT_ID).encode(),
        )

        self.assertEqual(query.page_size, 100)
        self.assertEqual(query.statuses, frozenset({MigrationProjectStatus.ACTIVE}))
        for invalid in (0, 101, True):
            with self.subTest(page_size=invalid):
                with self.assertRaisesRegex(
                    MigrationFoundationError,
                    "page_size is invalid",
                ):
                    ProjectCatalogueListQuery(page_size=invalid)

    def test_ready_and_deleted_records_require_bounded_evidence_times(self) -> None:
        ready = replace(
            record(),
            catalogue_state=ProjectCatalogueState.READY,
            artifact_root_key="organizations/001/projects/001",
            current_receipt_hash=RECEIPT_HASH,
        )
        deleting = replace(
            ready,
            catalogue_state=ProjectCatalogueState.DELETING,
            deletion_requested_at=NOW,
        )
        deleted = replace(
            deleting,
            catalogue_state=ProjectCatalogueState.DELETED,
            deleted_at=NOW,
        )

        self.assertEqual(ready.current_receipt_hash, RECEIPT_HASH)
        self.assertEqual(deleted.catalogue_state, ProjectCatalogueState.DELETED)
        with self.assertRaisesRegex(MigrationFoundationError, "proven artifact"):
            replace(record(), catalogue_state=ProjectCatalogueState.READY)
        with self.assertRaisesRegex(MigrationFoundationError, "request time"):
            replace(record(), catalogue_state=ProjectCatalogueState.DELETING)

    def test_artifact_pointer_rejects_traversal(self) -> None:
        with self.assertRaisesRegex(
            MigrationFoundationError,
            "artifact_root_key is invalid",
        ):
            replace(record(), artifact_root_key="../other-organization")


if __name__ == "__main__":
    unittest.main()
