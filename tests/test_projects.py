from __future__ import annotations

from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from uc_migration_profiler.intake import SourceIntakeError, SourceIntakeService
from uc_migration_profiler.project_store import DuckDbProjectRepository
from uc_migration_profiler.projects import (
    ProjectConflictError,
    ProjectRegistrationError,
    ProjectService,
    ProjectStatus,
    SourceFile,
)


ROOT = Path(__file__).resolve().parents[1]


class ProjectLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.repository = DuckDbProjectRepository(self.temporary.name)
        self.service = ProjectService(self.repository)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_project_is_persisted_and_stale_updates_are_rejected(self) -> None:
        project = self.service.create_project(
            name="Products migration",
            source_system="Dynamics AX 2012",
        )
        saved = self.repository.get(project.project_id)
        self.assertEqual(saved.name, "Products migration")
        self.assertEqual(saved.status, ProjectStatus.DRAFT)
        self.assertEqual(self.repository.list()[0].project_id, project.project_id)

        updated = self.service.update_governance(
            project.project_id,
            expected_revision=project.revision,
            data_manager="Data Manager",
            functional_owner="Product Owner",
            business_unit="UC",
            data_classification="CONFIDENTIAL",
            retention_days=90,
            support_access=False,
        )
        self.assertEqual(updated.revision, 2)
        with self.assertRaises(ProjectConflictError):
            self.service.update_governance(
                project.project_id,
                expected_revision=project.revision,
                data_manager="Stale",
                functional_owner="Owner",
                business_unit="UC",
                data_classification="CONFIDENTIAL",
                retention_days=90,
                support_access=False,
            )

    def test_registration_fails_closed_until_every_requirement_exists(self) -> None:
        project = self.service.create_project(
            name="Products migration",
            source_system="Dynamics AX 2012",
        )
        with self.assertRaises(ProjectRegistrationError) as caught:
            self.service.register(
                project.project_id,
                expected_revision=project.revision,
            )
        self.assertIn("At least one source file is required", caught.exception.problems)

    def test_duckdb_connections_apply_locked_security_settings(self) -> None:
        with self.repository._connect(  # noqa: SLF001 - adapter contract test
            self.repository.registry_path
        ) as connection:
            settings = connection.execute(
                """
                SELECT current_setting('enable_external_access'),
                       current_setting('autoinstall_known_extensions'),
                       current_setting('autoload_known_extensions'),
                       current_setting('allow_community_extensions'),
                       current_setting('lock_configuration')
                """
            ).fetchone()
        self.assertEqual(settings, (False, False, False, False, True))

    def test_complete_project_can_be_registered(self) -> None:
        project = self.service.create_project(
            name="Products migration",
            source_system="Dynamics AX 2012",
        )
        project = self.service.update_details(
            project.project_id,
            expected_revision=project.revision,
            name=project.name,
            source_system=project.source_system,
            export_status="RECEIVED",
            export_date=date.today().isoformat(),
            description="",
        )
        project = self.service.update_governance(
            project.project_id,
            expected_revision=project.revision,
            data_manager="Data Manager",
            functional_owner="Product Owner",
            business_unit="UC",
            data_classification="CONFIDENTIAL",
            retention_days=90,
            support_access=False,
        )
        project = self.service.update_target(
            project.project_id,
            expected_revision=project.revision,
            target_environment="TEST",
            odoo_base_url="https://odoo.example.test",
            odoo_database="uc_test",
            intended_applications=["Inventory"],
            intended_models=[],
        )
        project = self.service.add_source_file(
            project.project_id,
            expected_revision=project.revision,
            source_file=SourceFile(
                file_id="5df764bb-25df-4a64-95ec-50eafd9635bd",
                display_name="products.csv",
                stored_name="5df764bb-25df-4a64-95ec-50eafd9635bd.csv",
                size_bytes=10,
                sha256="a" * 64,
                received_at=datetime.now(timezone.utc),
            ),
        )
        registered = self.service.register(
            project.project_id,
            expected_revision=project.revision,
        )
        self.assertEqual(registered.status, ProjectStatus.REGISTERED)
        self.assertIsNotNone(registered.registered_at)
        manifest = (
            self.repository.project_directory(project.project_id)
            / "audit"
            / f"project-registration-r{registered.revision}.json"
        )
        self.assertTrue(manifest.is_file())
        self.assertIn('"approval_status":"NOT_STARTED"', manifest.read_text())


class SourceIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.repository = DuckDbProjectRepository(self.temporary.name)
        self.projects = ProjectService(self.repository)
        self.intake = SourceIntakeService(self.repository, self.projects)
        self.project = self.projects.create_project(
            name="Source intake",
            source_system="CSV",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_csv_is_hashed_and_stored_under_generated_name(self) -> None:
        source = self.intake.accept(
            self.project.project_id,
            expected_revision=self.project.revision,
            display_name="customers.csv",
            stream=BytesIO(b"code,name\nC1,Example\n"),
        )
        self.assertEqual(source.display_name, "customers.csv")
        self.assertNotEqual(source.stored_name, source.display_name)
        stored = (
            self.repository.project_directory(self.project.project_id)
            / "inbox"
            / source.stored_name
        )
        self.assertEqual(stored.read_bytes(), b"code,name\nC1,Example\n")

    def test_paths_and_unsupported_formats_are_rejected(self) -> None:
        for filename in ("../customer.csv", r"C:\customer.csv", "macro.xlsm"):
            with self.subTest(filename=filename), self.assertRaises(
                SourceIntakeError
            ):
                self.intake.accept(
                    self.project.project_id,
                    expected_revision=self.project.revision,
                    display_name=filename,
                    stream=BytesIO(b"unsafe"),
                )

    def test_invalid_xlsx_is_rejected_by_isolated_worker(self) -> None:
        with self.assertRaises(SourceIntakeError):
            self.intake.accept(
                self.project.project_id,
                expected_revision=self.project.revision,
                display_name="not-a-workbook.xlsx",
                stream=BytesIO(b"not a ZIP container"),
            )
