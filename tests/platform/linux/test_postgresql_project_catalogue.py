"""Exercise the Project catalogue contract against disposable PostgreSQL."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path, PurePosixPath
import tempfile
from threading import Lock
from typing import LiteralString
import unittest

import psycopg

from impodo.adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from impodo.adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from impodo.adapters.postgresql.migrations import POSTGRESQL_MIGRATIONS
from impodo.adapters.postgresql.project_catalogue import (
    PostgreSQLProjectCatalogueSession,
    PsycopgProjectCatalogueRepository,
    open_psycopg_project_catalogue_session,
)
from impodo.domain.project.catalogue import (
    ProjectCatalogueListQuery,
    ProjectCatalogueNotFoundError,
    ProjectCatalogueRecord,
    ProjectCatalogueRevisionConflictError,
    ProjectCatalogueState,
)
from impodo.domain.project.foundation import MigrationConflictError
from impodo.domain.project.models import (
    MigrationDataClassification,
    MigrationProject,
    MigrationProjectStatus,
)
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.shared.tenancy import OrganizationId, ProjectVisibilityScope


NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
ORGANIZATION_A = OrganizationId("fictional-organization-a")
ORGANIZATION_B = OrganizationId("fictional-organization-b")
PROJECT_A = "10000000-0000-4000-8000-000000000001"
PROJECT_B = "10000000-0000-4000-8000-000000000002"
PROJECT_C = "10000000-0000-4000-8000-000000000003"
RECEIPT_HASH = "sha256:" + "a" * 64
LOCAL_CREATE_OPERATION = "20000000-0000-4000-8000-000000000001"


class _SessionCounter:
    def __init__(self) -> None:
        self._lock = Lock()
        self.opened = 0
        self.closed = 0

    def record_open(self) -> None:
        with self._lock:
            self.opened += 1

    def record_close(self) -> None:
        with self._lock:
            self.closed += 1


class _CountingSession:
    def __init__(
        self,
        session: PostgreSQLProjectCatalogueSession,
        counter: _SessionCounter,
    ) -> None:
        self._session = session
        self._counter = counter

    def fetch_one(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[object, ...] | None:
        return self._session.fetch_one(query, parameters)

    def fetch_rows(
        self,
        query: LiteralString,
        parameters: tuple[object, ...] = (),
    ) -> tuple[tuple[object, ...], ...]:
        return self._session.fetch_rows(query, parameters)

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def close(self) -> None:
        self._session.close()
        self._counter.record_close()


def _record(
    organization_id: OrganizationId,
    project_id: str,
    *,
    display_name: str,
    updated_at: datetime,
) -> ProjectCatalogueRecord:
    return ProjectCatalogueRecord(
        organization_id=organization_id,
        project_id=project_id,
        display_name=display_name,
        status=MigrationProjectStatus.DRAFT,
        data_classification=MigrationDataClassification.CONFIDENTIAL,
        retention_days=365,
        optimistic_revision=1,
        catalogue_state=ProjectCatalogueState.PENDING,
        artifact_root_key=None,
        current_receipt_hash=None,
        created_at=NOW,
        updated_at=updated_at,
    )


class PostgreSQLProjectCatalogueLiveTests(unittest.TestCase):
    """Use only fictional rows in the Linux-owned disposable database."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dsn = os.environ.get("IMPODO_TEST_POSTGRES_DSN", "")
        if not cls.dsn:
            raise RuntimeError(
                "IMPODO_TEST_POSTGRES_DSN must name a disposable PostgreSQL database"
            )
        with psycopg.connect(cls.dsn, autocommit=True) as connection:
            connection.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT FROM pg_roles WHERE rolname = 'impodo_runtime'
                    ) THEN
                        CREATE ROLE impodo_runtime NOLOGIN NOSUPERUSER NOCREATEDB
                            NOCREATEROLE NOREPLICATION NOBYPASSRLS;
                    END IF;
                END
                $$
                """
            )
            connection.execute("DROP SCHEMA IF EXISTS impodo_control CASCADE")
            connection.execute("CREATE SCHEMA impodo_control")
            for migration in POSTGRESQL_MIGRATIONS:
                for statement in migration.statements:
                    connection.execute(statement.query, statement.parameters)
        cls.repository = PsycopgProjectCatalogueRepository(
            lambda _reference: cls.dsn,
            PurePosixPath("/run/secrets/postgres-runtime-dsn"),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        with psycopg.connect(cls.dsn, autocommit=True) as connection:
            connection.execute("DROP SCHEMA IF EXISTS impodo_control CASCADE")

    def setUp(self) -> None:
        with psycopg.connect(self.dsn) as connection:
            connection.execute("TRUNCATE impodo_control.project_catalogue CASCADE")
            connection.execute("DELETE FROM impodo_control.organization")
            for organization_id, display_name in (
                (ORGANIZATION_A, "Fictional organization A"),
                (ORGANIZATION_B, "Fictional organization B"),
            ):
                connection.execute(
                    """
                    INSERT INTO impodo_control.organization (
                        organization_id, display_name, status,
                        policy_revision, optimistic_revision, deletion_state
                    ) VALUES (%s, %s, 'ACTIVE', 1, 1, 'NONE')
                    """,
                    (str(organization_id), display_name),
                )

    def test_same_project_id_is_isolated_by_organization_and_revision(self) -> None:
        project_a = self.repository.create(
            _record(
                ORGANIZATION_A,
                PROJECT_A,
                display_name="Organization A migration",
                updated_at=NOW,
            )
        )
        project_b = self.repository.create(
            _record(
                ORGANIZATION_B,
                PROJECT_A,
                display_name="Organization B migration",
                updated_at=NOW,
            )
        )

        renamed = self.repository.rename(
            ORGANIZATION_A,
            PROJECT_A,
            expected_revision=project_a.optimistic_revision,
            display_name="Organization A renamed",
            updated_at=NOW + timedelta(minutes=1),
        )

        self.assertEqual(renamed.optimistic_revision, 2)
        self.assertEqual(
            self.repository.get(ORGANIZATION_B, PROJECT_A),
            project_b,
        )
        with self.assertRaises(ProjectCatalogueRevisionConflictError):
            self.repository.rename(
                ORGANIZATION_A,
                PROJECT_A,
                expected_revision=1,
                display_name="Stale rename",
                updated_at=NOW + timedelta(minutes=2),
            )
        with self.assertRaisesRegex(
            ProjectCatalogueNotFoundError,
            "^Project not found$",
        ):
            self.repository.get(
                OrganizationId("fictional-organization-c"),
                PROJECT_A,
            )

    def test_visible_list_filters_before_stable_keyset_pagination(self) -> None:
        for project_id, updated_at in (
            (PROJECT_A, NOW + timedelta(minutes=3)),
            (PROJECT_B, NOW + timedelta(minutes=2)),
            (PROJECT_C, NOW + timedelta(minutes=1)),
        ):
            self.repository.create(
                _record(
                    ORGANIZATION_A,
                    project_id,
                    display_name=f"Visible {project_id[-1]}",
                    updated_at=updated_at,
                )
            )
        self.repository.create(
            _record(
                ORGANIZATION_B,
                PROJECT_A,
                display_name="Other Organization",
                updated_at=NOW + timedelta(minutes=4),
            )
        )
        visibility = ProjectVisibilityScope(
            organization_id=ORGANIZATION_A,
            project_ids=frozenset({PROJECT_A, PROJECT_C}),
        )

        first = self.repository.list_visible(
            visibility,
            ProjectCatalogueListQuery(
                statuses=frozenset({MigrationProjectStatus.DRAFT}),
                catalogue_states=frozenset({ProjectCatalogueState.PENDING}),
                page_size=1,
            ),
        )
        second = self.repository.list_visible(
            visibility,
            ProjectCatalogueListQuery(
                statuses=frozenset({MigrationProjectStatus.DRAFT}),
                catalogue_states=frozenset({ProjectCatalogueState.PENDING}),
                page_size=1,
                cursor=first.next_cursor,
            ),
        )

        self.assertEqual([item.project_id for item in first.records], [PROJECT_A])
        self.assertEqual([item.project_id for item in second.records], [PROJECT_C])
        self.assertIsNotNone(first.next_cursor)
        self.assertIsNone(second.next_cursor)

    def test_lifecycle_projection_and_deletion_leave_the_row_present(self) -> None:
        created = self.repository.create(
            _record(
                ORGANIZATION_A,
                PROJECT_A,
                display_name="Lifecycle trial",
                updated_at=NOW,
            )
        )
        ready = self.repository.update_lifecycle_summary(
            ORGANIZATION_A,
            PROJECT_A,
            expected_revision=created.optimistic_revision,
            status=MigrationProjectStatus.ACTIVE,
            catalogue_state=ProjectCatalogueState.READY,
            artifact_root_key="organizations/a/projects/one",
            current_receipt_hash=RECEIPT_HASH,
            updated_at=NOW + timedelta(minutes=1),
        )
        deleting = self.repository.transition_deletion_state(
            ORGANIZATION_A,
            PROJECT_A,
            expected_revision=ready.optimistic_revision,
            target_state=ProjectCatalogueState.DELETING,
            occurred_at=NOW + timedelta(minutes=2),
        )
        deleted = self.repository.transition_deletion_state(
            ORGANIZATION_A,
            PROJECT_A,
            expected_revision=deleting.optimistic_revision,
            target_state=ProjectCatalogueState.DELETED,
            occurred_at=NOW + timedelta(minutes=3),
        )

        with psycopg.connect(self.dsn) as connection:
            row_count, runtime_can_delete = connection.execute(
                """
                SELECT count(*),
                       has_table_privilege(
                           'impodo_runtime',
                           'impodo_control.project_catalogue',
                           'DELETE'
                       )
                FROM impodo_control.project_catalogue
                WHERE organization_id = %s AND project_id = %s
                """,
                (str(ORGANIZATION_A), PROJECT_A),
            ).fetchone()

        self.assertEqual(deleted.optimistic_revision, 4)
        self.assertEqual(deleted.catalogue_state, ProjectCatalogueState.DELETED)
        self.assertEqual(row_count, 1)
        self.assertFalse(runtime_can_delete)

    def test_shared_local_and_hosted_revision_semantics_match(self) -> None:
        """Compare only the bounded semantics both persistence models own."""

        with tempfile.TemporaryDirectory(prefix="impodo-catalogue-") as temporary:
            local_repository = MigrationFoundationRepository(
                MigrationFoundationDatabase(Path(temporary))
            )
            local_project = MigrationProject(
                project_id=PROJECT_A,
                display_name="Shared semantic trial",
                migration_purpose="Verify bounded persistence semantics",
                source_system_identity="Fictional legacy source",
                data_classification=MigrationDataClassification.CONFIDENTIAL,
                retention_days=365,
                status=MigrationProjectStatus.DRAFT,
                optimistic_revision=1,
                created_at=NOW,
                updated_at=NOW,
            )
            local_created = local_repository.create_project(
                local_project,
                operation_id=LOCAL_CREATE_OPERATION,
                request_hash=content_hash(
                    {"project_id": PROJECT_A, "operation": "create"}
                ),
                actor=LOCAL_ACTOR,
            )
            hosted_created = self.repository.create(
                _record(
                    ORGANIZATION_A,
                    PROJECT_A,
                    display_name=local_project.display_name,
                    updated_at=NOW,
                )
            )

            renamed_at = NOW + timedelta(minutes=1)
            local_renamed = local_repository.save_project(
                replace(
                    local_created,
                    display_name="Shared semantic rename",
                    updated_at=renamed_at,
                ),
                expected_revision=1,
                event_type="MIGRATION_PROJECT_RENAMED",
                actor=LOCAL_ACTOR,
            )
            hosted_renamed = self.repository.rename(
                ORGANIZATION_A,
                PROJECT_A,
                expected_revision=1,
                display_name="Shared semantic rename",
                updated_at=renamed_at,
            )

            self.assertEqual(local_renamed.display_name, hosted_renamed.display_name)
            self.assertEqual(
                local_renamed.optimistic_revision,
                hosted_renamed.optimistic_revision,
            )
            self.assertEqual(local_renamed.status, hosted_renamed.status)
            with self.assertRaises(MigrationConflictError):
                local_repository.save_project(
                    replace(
                        local_created,
                        display_name="Stale local rename",
                        updated_at=NOW + timedelta(minutes=2),
                    ),
                    expected_revision=1,
                    event_type="MIGRATION_PROJECT_RENAMED",
                    actor=LOCAL_ACTOR,
                )
            with self.assertRaises(ProjectCatalogueRevisionConflictError):
                self.repository.rename(
                    ORGANIZATION_A,
                    PROJECT_A,
                    expected_revision=1,
                    display_name="Stale hosted rename",
                    updated_at=NOW + timedelta(minutes=2),
                )

    def test_concurrent_reads_close_every_independent_session(self) -> None:
        created = self.repository.create(
            _record(
                ORGANIZATION_A,
                PROJECT_A,
                display_name="Concurrent read trial",
                updated_at=NOW,
            )
        )
        counter = _SessionCounter()

        def open_counted(dsn: str) -> PostgreSQLProjectCatalogueSession:
            session = open_psycopg_project_catalogue_session(dsn)
            counter.record_open()
            return _CountingSession(session, counter)

        repository = PsycopgProjectCatalogueRepository(
            lambda _reference: self.dsn,
            PurePosixPath("/run/secrets/postgres-runtime-dsn"),
            session_factory=open_counted,
        )

        def read_project(_index: int) -> ProjectCatalogueRecord:
            return repository.get(ORGANIZATION_A, PROJECT_A)

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = tuple(executor.map(read_project, range(12)))

        self.assertEqual(results, (created,) * 12)
        self.assertEqual(counter.opened, 12)
        self.assertEqual(counter.closed, 12)


if __name__ == "__main__":
    unittest.main()
