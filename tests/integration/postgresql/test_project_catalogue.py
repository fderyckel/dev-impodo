"""Verify the PostgreSQL Project catalogue adapter through bounded sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
import unittest
from unittest.mock import Mock, patch

import psycopg

from impodo.adapters.postgresql.project_catalogue import (
    PsycopgProjectCatalogueRepository,
    open_psycopg_project_catalogue_session,
)
from impodo.domain.project.catalogue import (
    ProjectCatalogueAlreadyExistsError,
    ProjectCatalogueListQuery,
    ProjectCatalogueNotFoundError,
    ProjectCatalogueRecord,
    ProjectCatalogueRevisionConflictError,
    ProjectCatalogueState,
)
from impodo.domain.project.models import (
    MigrationDataClassification,
    MigrationProjectStatus,
)
from impodo.domain.shared.tenancy import OrganizationId, ProjectVisibilityScope


NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
ORGANIZATION_A = OrganizationId("organization-a")
ORGANIZATION_B = OrganizationId("organization-b")
PROJECT_A = "10000000-0000-4000-8000-000000000001"
PROJECT_B = "10000000-0000-4000-8000-000000000002"
PROJECT_C = "10000000-0000-4000-8000-000000000003"


def row(
    project_id: str = PROJECT_A,
    *,
    organization_id: OrganizationId = ORGANIZATION_A,
    display_name: str = "Customer migration",
    revision: int = 1,
    state: ProjectCatalogueState = ProjectCatalogueState.PENDING,
    updated_at: datetime = NOW,
    deletion_requested_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> tuple[object, ...]:
    artifact_root_key = None
    receipt_hash = None
    if state is ProjectCatalogueState.READY:
        artifact_root_key = f"organizations/{organization_id}/projects/{project_id}"
        receipt_hash = "sha256:" + "a" * 64
    return (
        str(organization_id),
        project_id,
        display_name,
        "DRAFT",
        "CONFIDENTIAL",
        365,
        revision,
        state.value,
        artifact_root_key,
        receipt_hash,
        NOW,
        updated_at,
        deletion_requested_at,
        deleted_at,
    )


def record() -> ProjectCatalogueRecord:
    return ProjectCatalogueRecord(
        organization_id=ORGANIZATION_A,
        project_id=PROJECT_A,
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


class _Session:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.events: list[tuple[str, str, tuple[object, ...]]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def fetch_one(self, query, parameters=()):
        self.events.append(("fetch_one", query, parameters))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def fetch_rows(self, query, parameters=()):
        self.events.append(("fetch_rows", query, parameters))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def repository(session: _Session) -> PsycopgProjectCatalogueRepository:
    return PsycopgProjectCatalogueRepository(
        lambda _reference: "private-dsn",
        PurePosixPath("/run/secrets/postgres-runtime-dsn"),
        session_factory=lambda _dsn: session,
    )


class PostgreSQLProjectCatalogueTests(unittest.TestCase):
    def test_create_uses_one_transaction_and_returns_the_stored_row(self) -> None:
        session = _Session([row()])

        stored = repository(session).create(record())

        self.assertEqual(stored.project_id, PROJECT_A)
        self.assertIn("INSERT INTO impodo_control.project_catalogue", session.events[0][1])
        self.assertNotIn("DELETE FROM", session.events[0][1])
        self.assertEqual(session.events[0][2][0:2], (str(ORGANIZATION_A), PROJECT_A))
        self.assertTrue(session.committed)
        self.assertTrue(session.closed)

    def test_get_uses_one_tenant_safe_predicate_and_opaque_not_found(self) -> None:
        session = _Session([None])

        with self.assertRaisesRegex(
            ProjectCatalogueNotFoundError,
            "^Project not found$",
        ):
            repository(session).get(ORGANIZATION_B, PROJECT_A)

        query = session.events[0][1]
        self.assertIn("organization_id = %s AND project_id = %s", query)
        self.assertEqual(session.events[0][2], (str(ORGANIZATION_B), PROJECT_A))
        self.assertTrue(session.rolled_back)
        self.assertTrue(session.closed)

    def test_visible_list_filters_in_sql_and_returns_a_keyset_cursor(self) -> None:
        session = _Session(
            [
                (
                    row(PROJECT_A, updated_at=NOW + timedelta(minutes=2)),
                    row(PROJECT_B, updated_at=NOW + timedelta(minutes=1)),
                    row(PROJECT_C, updated_at=NOW),
                )
            ]
        )
        visibility = ProjectVisibilityScope(
            organization_id=ORGANIZATION_A,
            project_ids=frozenset({PROJECT_A, PROJECT_B, PROJECT_C}),
        )

        page = repository(session).list_visible(
            visibility,
            ProjectCatalogueListQuery(
                statuses=frozenset({MigrationProjectStatus.DRAFT}),
                page_size=2,
            ),
        )

        self.assertEqual([item.project_id for item in page.records], [PROJECT_A, PROJECT_B])
        self.assertIsNotNone(page.next_cursor)
        query = session.events[0][1]
        parameters = session.events[0][2]
        self.assertIn("organization_id = %s", query)
        self.assertIn("project_id = ANY(%s::uuid[])", query)
        self.assertIn("ORDER BY updated_at DESC, project_id ASC", query)
        self.assertEqual(parameters[0], str(ORGANIZATION_A))
        self.assertEqual(parameters[-1], 3)

    def test_empty_project_visibility_does_not_open_postgresql(self) -> None:
        factory = Mock()
        catalogue = PsycopgProjectCatalogueRepository(
            lambda _reference: "private-dsn",
            PurePosixPath("/run/secrets/postgres-runtime-dsn"),
            session_factory=factory,
        )

        page = catalogue.list_visible(
            ProjectVisibilityScope(organization_id=ORGANIZATION_A),
            ProjectCatalogueListQuery(),
        )

        self.assertEqual(page.records, ())
        factory.assert_not_called()

    def test_every_update_predicate_includes_tenant_identity_and_revision(self) -> None:
        cases = (
            (
                "rename",
                lambda catalogue: catalogue.rename(
                    ORGANIZATION_A,
                    PROJECT_A,
                    expected_revision=1,
                    display_name="Renamed migration",
                    updated_at=NOW,
                ),
            ),
            (
                "lifecycle",
                lambda catalogue: catalogue.update_lifecycle_summary(
                    ORGANIZATION_A,
                    PROJECT_A,
                    expected_revision=1,
                    status=MigrationProjectStatus.ACTIVE,
                    catalogue_state=ProjectCatalogueState.REPAIR_REQUIRED,
                    artifact_root_key=None,
                    current_receipt_hash=None,
                    updated_at=NOW,
                ),
            ),
            (
                "deletion",
                lambda catalogue: catalogue.transition_deletion_state(
                    ORGANIZATION_A,
                    PROJECT_A,
                    expected_revision=1,
                    target_state=ProjectCatalogueState.DELETING,
                    occurred_at=NOW,
                ),
            ),
        )
        for name, mutation in cases:
            with self.subTest(mutation=name):
                session = _Session([row(revision=2)])
                mutation(repository(session))
                query = session.events[0][1]
                self.assertIn("organization_id = %s", query)
                self.assertIn("project_id = %s", query)
                self.assertIn("optimistic_revision = %s", query)
                self.assertNotIn("DELETE FROM", query)

    def test_stale_revision_reports_no_current_values_or_other_tenant(self) -> None:
        session = _Session([None, (2, "PENDING")])

        with self.assertRaisesRegex(
            ProjectCatalogueRevisionConflictError,
            "^Project revision is stale$",
        ) as caught:
            repository(session).rename(
                ORGANIZATION_A,
                PROJECT_A,
                expected_revision=1,
                display_name="Private customer value",
                updated_at=NOW,
            )

        self.assertNotIn("Private", str(caught.exception))
        self.assertNotIn(str(ORGANIZATION_A), str(caught.exception))
        self.assertTrue(session.rolled_back)

    def test_unique_failure_is_bounded_and_rolls_back(self) -> None:
        session = _Session([psycopg.errors.UniqueViolation("private row")])

        with self.assertRaisesRegex(
            ProjectCatalogueAlreadyExistsError,
            "^Project already exists$",
        ) as caught:
            repository(session).create(record())

        self.assertNotIn("private", str(caught.exception))
        self.assertTrue(session.rolled_back)
        self.assertTrue(session.closed)

    def test_default_psycopg_session_has_bounded_runtime_role_options(self) -> None:
        connection = Mock()
        with patch(
            "impodo.adapters.postgresql.project_catalogue.psycopg.connect",
            return_value=connection,
        ) as connect:
            session = open_psycopg_project_catalogue_session("private-dsn")
            session.close()

        options = connect.call_args.kwargs["options"]
        self.assertIn("role=impodo_runtime", options)
        self.assertIn("statement_timeout=5000", options)
        self.assertIn("lock_timeout=1000", options)
        self.assertFalse(connect.call_args.kwargs["autocommit"])


if __name__ == "__main__":
    unittest.main()
