from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import duckdb

from impodo.access import (
    CapabilityAuthorizationPolicy,
    LOCAL_ACTOR,
)
from impodo.artifacts import LocalArtifactStore
from impodo.intake import SourceIntakeError, SourceIntakeService
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.adapters.duckdb.schema_repository import SchemaRepository
from impodo.adapters.duckdb.transformation_impact_repository import (
    TransformationImpactRepository,
)
from impodo.adapters.duckdb.constants import SCHEMA_GENERATION, SCHEMA_VERSION
from impodo.adapters.duckdb.schema.upgrades import PROJECT_SCHEMA_UPGRADES
from impodo.projects import (
    OdooConnectionMode,
    ProjectCompatibilityError,
    ProjectConflictError,
    ProjectError,
    ProjectNotFoundError,
    ProjectRegistrationError,
    ProjectService,
    ProjectStatus,
    SourceFile,
)
from impodo.workspace_contracts import (
    MappingWorkingDraft,
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.serialization import CanonicalJsonObjectHasher, content_hash
from impodo.domain.staging.transformation_impact import (
    TransformationImpactFilter,
    TransformationImpactIdentity,
    TransformationImpactReport,
    TransformationImpactRow,
    TransformationRuleImpact,
)


ROOT = Path(__file__).resolve().parents[1]


class CanonicalSerializationTests(unittest.TestCase):
    def test_incremental_object_hash_matches_materialized_canonical_json(self) -> None:
        payload = {
            "contract_version": 1,
            "rows": [
                {"row_id": "row-1", "value": "Élodie"},
                {"row_id": "row-2", "value": None},
            ],
            "source_hash": "sha256:" + "a" * 64,
        }
        hasher = CanonicalJsonObjectHasher()
        hasher.add_value("contract_version", 1)
        hasher.start_array("rows")
        for row in payload["rows"]:
            hasher.add_encoded_array_item(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        hasher.end_array()
        hasher.add_value("source_hash", payload["source_hash"])

        self.assertEqual(hasher.finish(), content_hash(payload))


class ProjectLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.database = DuckDbDatabase(self.temporary.name)
        self.repository = ProjectRepository(self.database)
        self.schemas = SchemaRepository(self.database)
        self.transformation_impacts = TransformationImpactRepository(self.database)
        self.service = ProjectService(
            self.repository,
            CapabilityAuthorizationPolicy(),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_project_is_persisted_and_stale_updates_are_rejected(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Products migration",
            source_system="Dynamics AX 2012",
        )
        saved = self.repository.get(project.project_id)
        self.assertEqual(saved.name, "Products migration")
        self.assertEqual(saved.status, ProjectStatus.DRAFT)
        self.assertEqual(self.repository.list()[0].project_id, project.project_id)

        updated = self.service.update_governance(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=project.revision,
            data_manager="Data Manager",
            functional_owner="Product Owner",
            business_unit="Example Business Unit",
            data_classification="CONFIDENTIAL",
            retention_days=90,
            support_access=False,
        )
        self.assertEqual(updated.revision, 2)
        database_path = (
            self.repository.project_directory(project.project_id)
            / "project.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            audit_actor = connection.execute(
                """
                SELECT actor_issuer, actor_subject, actor_display_name
                  FROM audit_event
                 ORDER BY event_id DESC
                 LIMIT 1
                """
            ).fetchone()
        self.assertEqual(
            audit_actor,
            (
                LOCAL_ACTOR.identity.issuer,
                LOCAL_ACTOR.identity.subject_id,
                LOCAL_ACTOR.identity.display_name,
            ),
        )
        with self.assertRaises(ProjectConflictError):
            self.service.update_governance(
                project.project_id,
                actor=LOCAL_ACTOR,
                expected_revision=project.revision,
                data_manager="Stale",
                functional_owner="Owner",
                business_unit="Example Business Unit",
                data_classification="CONFIDENTIAL",
                retention_days=90,
                support_access=False,
            )

    def test_registration_fails_closed_until_every_requirement_exists(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Products migration",
            source_system="Dynamics AX 2012",
        )
        with self.assertRaises(ProjectRegistrationError) as caught:
            self.service.register(
                project.project_id,
                actor=LOCAL_ACTOR,
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

    def test_local_and_remote_target_urls_are_kept_separate(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Connection modes",
            source_system="CSV",
        )
        local = self.service.update_target(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=project.revision,
            odoo_connection_mode="LOCAL",
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_applications=["Contacts"],
            intended_models=["res.partner"],
        )
        self.assertEqual(local.odoo_connection_mode, OdooConnectionMode.LOCAL)

        remote = self.service.update_target(
            local.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=local.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://odoo.example.com",
            odoo_database="odoo_review",
            intended_applications=["Contacts"],
            intended_models=["res.partner"],
        )
        self.assertEqual(remote.odoo_connection_mode, OdooConnectionMode.REMOTE)

        invalid_targets = (
            ("LOCAL", "http://localhost:8069"),
            ("LOCAL", "http://192.168.1.20:8069"),
            ("LOCAL", "http://127.0.0.1:8069/odoo"),
            ("REMOTE", "http://odoo.example.com"),
            ("REMOTE", "https://127.0.0.1:8069"),
        )
        for mode, base_url in invalid_targets:
            with self.subTest(mode=mode, base_url=base_url), self.assertRaises(
                ProjectError
            ):
                self.service.update_target(
                    remote.project_id,
                    actor=LOCAL_ACTOR,
                    expected_revision=remote.revision,
                    odoo_connection_mode=mode,
                    odoo_base_url=base_url,
                    odoo_database="odoo_review",
                    intended_applications=["Contacts"],
                    intended_models=[],
                )

    def test_historical_project_schema_layout_is_rejected(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Historical development project",
            source_system="Legacy source",
        )
        database_path = (
            self.repository.project_directory(project.project_id)
            / "project.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            connection.execute("DROP TABLE schema_version")
            connection.execute(
                "CREATE TABLE schema_version (version INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO schema_version VALUES (1)")

        with self.assertRaisesRegex(ProjectCompatibilityError, "older Impodo"):
            self.repository.get(project.project_id)

        deleted = self.service.delete_project(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=project.revision,
        )
        self.assertEqual(deleted, project)
        self.assertFalse(database_path.parent.exists())
        self.assertEqual(self.repository.list(), ())

    def test_supported_project_schema_upgrades_once_and_atomically(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Forward-compatible project",
            source_system="CSV",
        )
        database_path = (
            self.repository.project_directory(project.project_id)
            / "project.duckdb"
        )
        upgrade_calls: list[int] = []

        def upgrade_to_version_two(
            connection: duckdb.DuckDBPyConnection,
        ) -> None:
            upgrade_calls.append(1)
            connection.execute(
                "ALTER TABLE project ADD COLUMN schema_upgrade_probe VARCHAR"
            )

        with (
            patch(
                "impodo.adapters.duckdb.schema.project.SCHEMA_VERSION",
                2,
            ),
            patch.dict(
                PROJECT_SCHEMA_UPGRADES,
                {1: upgrade_to_version_two},
                clear=True,
            ),
        ):
            self.repository.get(project.project_id)
            self.repository.get(project.project_id)

        with self.repository._connect(database_path) as connection:
            schema_row = connection.execute(
                """
                SELECT generation, version
                  FROM schema_version
                 WHERE singleton_id = 1
                """
            ).fetchone()
            probe_column = connection.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_name = 'project'
                   AND column_name = 'schema_upgrade_probe'
                """
            ).fetchone()
        self.assertEqual(upgrade_calls, [1])
        self.assertEqual(schema_row, (SCHEMA_GENERATION, 2))
        self.assertEqual(probe_column, ("schema_upgrade_probe",))

    def test_failed_project_schema_upgrade_rolls_back(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Rollback-safe project",
            source_system="CSV",
        )
        database_path = (
            self.repository.project_directory(project.project_id)
            / "project.duckdb"
        )

        def fail_upgrade(connection: duckdb.DuckDBPyConnection) -> None:
            connection.execute(
                "ALTER TABLE project ADD COLUMN failed_upgrade_probe VARCHAR"
            )
            raise RuntimeError("injected schema upgrade failure")

        with (
            patch(
                "impodo.adapters.duckdb.schema.project.SCHEMA_VERSION",
                2,
            ),
            patch.dict(
                PROJECT_SCHEMA_UPGRADES,
                {1: fail_upgrade},
                clear=True,
            ),
            self.assertRaisesRegex(RuntimeError, "injected schema upgrade"),
        ):
            self.repository.get(project.project_id)

        with self.repository._connect(database_path) as connection:
            version = connection.execute(
                """
                SELECT version
                  FROM schema_version
                 WHERE singleton_id = 1
                """
            ).fetchone()
            probe_column = connection.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_name = 'project'
                   AND column_name = 'failed_upgrade_probe'
                """
            ).fetchone()
        self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertIsNone(probe_column)

    def test_transformation_impact_snapshot_is_bounded_filterable_and_atomic(
        self,
    ) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Products migration",
            source_system="Dynamics AX 2012",
        )
        identity = TransformationImpactIdentity(
            physical_selection_hash="sha256:" + "1" * 64,
            source_selection_hash="sha256:" + "2" * 64,
            mapping_content_hash="sha256:" + "3" * 64,
            schema_hash="sha256:" + "4" * 64,
            derived_plan_hash=None,
        )
        rows = tuple(
            TransformationImpactRow(
                dataset="products",
                source_row=(index // 2) + 2,
                source_column="Product name",
                target_field="name" if index % 3 else "default_code",
                raw_value=f" source {index} ",
                proposed_value=f"Source {index}",
                rules="Trim",
                outcome="invalid" if index % 2 else "changed",
                message="Needs review" if index % 2 else "",
            )
            for index in range(205)
        )
        zero_match_rule = TransformationRuleImpact(
            dataset_id="dataset:products",
            target_field="default_code",
            rule_kind="find_replace_literal",
            rule_fingerprint="sha256:" + "9" * 64,
            evaluated_value_count=205,
            matched_value_count=0,
            changed_value_count=0,
        )

        def build(write_row):
            for row in rows:
                write_row(row)
            return TransformationImpactReport(
                mapping_content_hash=identity.mapping_content_hash,
                evaluated_count=205,
                changed_count=103,
                fallback_count=0,
                null_count=0,
                invalid_count=102,
                provided_count=0,
                unchanged_count=0,
                rows=(),
                rule_impacts=(zero_match_rule,),
                detail_limit=0,
            )

        snapshot = self.transformation_impacts.replace_transformation_impact_snapshot(
            project.project_id,
            identity,
            build,
            actor=LOCAL_ACTOR,
        )
        first = self.transformation_impacts.get_transformation_impact_page(
            project.project_id,
            identity,
            TransformationImpactFilter(),
            page_size=100,
        )
        second = self.transformation_impacts.get_transformation_impact_page(
            project.project_id,
            identity,
            TransformationImpactFilter(),
            page_size=100,
            after=first.next_after,
        )
        invalid = self.transformation_impacts.get_transformation_impact_page(
            project.project_id,
            identity,
            TransformationImpactFilter(outcome="invalid", query="review"),
            page_size=100,
        )

        self.assertEqual(snapshot.affected_row_count, 103)
        self.assertEqual(snapshot.report.rule_impacts, (zero_match_rule,))
        self.assertEqual(
            snapshot.unacknowledged_rule_impacts,
            (zero_match_rule,),
        )
        self.assertEqual((first.start_position, first.end_position), (1, 100))
        self.assertEqual(len(first.rows), 100)
        self.assertIsNone(first.previous_before)
        self.assertIsNotNone(first.next_after)
        self.assertEqual((second.start_position, second.end_position), (101, 200))
        self.assertIsNotNone(second.previous_before)
        self.assertEqual(invalid.matching_count, 102)
        self.assertEqual(len(invalid.rows), 100)
        self.assertEqual(
            len(
                tuple(
                    self.transformation_impacts.iter_transformation_impact_rows(
                        project.project_id,
                        identity,
                        TransformationImpactFilter(target_field="name"),
                    )
                )
            ),
            136,
        )

        reused = self.transformation_impacts.replace_transformation_impact_snapshot(
            project.project_id,
            identity,
            lambda _write: self.fail("matching snapshots must be reused"),
            actor=LOCAL_ACTOR,
        )
        self.assertEqual(reused.created_at, snapshot.created_at)

        self.transformation_impacts.acknowledge_transformation_rule(
            project.project_id,
            identity,
            zero_match_rule.rule_fingerprint,
            actor=LOCAL_ACTOR,
        )
        acknowledged = self.transformation_impacts.get_transformation_impact_snapshot(
            project.project_id,
            identity,
        )
        assert acknowledged is not None
        self.assertEqual(acknowledged.unacknowledged_rule_impacts, ())
        review = self.transformation_impacts.get_transformation_rule_review(
            project.project_id,
            mapping_content_hash=identity.mapping_content_hash,
            source_selection_hash=identity.source_selection_hash,
            schema_hash=identity.schema_hash,
        )
        assert review is not None
        self.assertEqual(review.unacknowledged_rule_impacts, ())

        replacement_identity = replace(
            identity,
            mapping_content_hash="sha256:" + "5" * 64,
        )

        def fail_after_one_row(write_row):
            write_row(rows[0])
            raise RuntimeError("simulated preparation failure")

        with self.assertRaisesRegex(RuntimeError, "simulated preparation failure"):
            self.transformation_impacts.replace_transformation_impact_snapshot(
                project.project_id,
                replacement_identity,
                fail_after_one_row,
                actor=LOCAL_ACTOR,
            )
        self.assertIsNotNone(
            self.transformation_impacts.get_transformation_impact_snapshot(
                project.project_id,
                identity,
            )
        )

    def test_delete_permanently_removes_registered_project_and_artifacts(
        self,
    ) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Disposable rehearsal",
            source_system="CSV",
        )
        project_dir = self.repository.project_directory(project.project_id)
        (project_dir / "reports" / "review.txt").write_text(
            "disposable",
            encoding="utf-8",
        )
        now = datetime.now(timezone.utc)
        registered = replace(
            project,
            status=ProjectStatus.REGISTERED,
            revision=project.revision + 1,
            updated_at=now,
            registered_at=now,
        )
        self.repository.save(
            registered,
            expected_revision=project.revision,
            event_type="TEST_PROJECT_REGISTERED",
            event_detail="",
            actor=LOCAL_ACTOR,
        )
        scoped = self.service.update_schema_scope(
            registered.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=registered.revision,
            permitted_models=("res.partner",),
        )
        summary = self.repository.list()[0]
        self.assertEqual(summary.revision, scoped.revision)
        self.assertEqual(summary.updated_at, scoped.updated_at)

        with self.assertRaisesRegex(ProjectConflictError, "reload before deleting"):
            self.service.delete_project(
                project.project_id,
                actor=LOCAL_ACTOR,
                expected_revision=registered.revision,
            )
        self.assertTrue(project_dir.is_dir())

        deleted = self.service.delete_project(
            scoped.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=scoped.revision,
        )

        self.assertEqual(deleted, scoped)
        self.assertFalse(project_dir.exists())
        self.assertEqual(self.repository.list(), ())
        with self.assertRaises(ProjectNotFoundError):
            self.repository.get(scoped.project_id)

    def test_pending_registry_summary_is_recovered_after_interrupted_write(
        self,
    ) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Interrupted summary",
            source_system="CSV",
        )

        with patch.object(
            self.repository,
            "_update_registry",
            side_effect=RuntimeError("simulated registry interruption"),
        ):
            with self.assertRaisesRegex(RuntimeError, "registry interruption"):
                self.service.update_details(
                    project.project_id,
                    actor=LOCAL_ACTOR,
                    expected_revision=project.revision,
                    name="Recovered summary",
                    source_system="CSV",
                    export_status="PLANNED",
                    export_date="",
                    description="",
                )

        committed = self.repository.get(project.project_id)
        self.assertEqual(committed.revision, project.revision + 1)
        self.assertEqual(self.repository.list()[0].revision, project.revision)
        with self.repository._connect(self.repository.registry_path) as connection:
            pending = connection.execute(
                "SELECT project_id FROM project_registry_sync_pending"
            ).fetchall()
        self.assertEqual(pending, [(project.project_id,)])

        recovered = ProjectRepository(self.database)

        self.assertEqual(recovered.list()[0].revision, committed.revision)
        with recovered._connect(recovered.registry_path) as connection:
            pending = connection.execute(
                "SELECT project_id FROM project_registry_sync_pending"
            ).fetchall()
        self.assertEqual(pending, [])

    def test_registry_summary_does_not_regress_when_writes_finish_out_of_order(
        self,
    ) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Original summary",
            source_system="CSV",
        )
        older = replace(
            project,
            name="Older summary",
            revision=project.revision + 1,
        )
        newer = replace(
            project,
            name="Newest summary",
            revision=project.revision + 2,
        )

        self.repository._update_registry(newer)
        self.repository._update_registry(older)

        summary = self.repository.list()[0]
        self.assertEqual(summary.revision, newer.revision)
        self.assertEqual(summary.name, newer.name)





    def test_complete_project_can_be_registered(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Products migration",
            source_system="Dynamics AX 2012",
        )
        project = self.service.update_details(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=project.revision,
            name=project.name,
            source_system=project.source_system,
            export_status="RECEIVED",
            export_date=date.today().isoformat(),
            description="",
        )
        project = self.service.update_governance(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=project.revision,
            data_manager="Data Manager",
            functional_owner="Product Owner",
            business_unit="Example Business Unit",
            data_classification="CONFIDENTIAL",
            retention_days=90,
            support_access=False,
        )
        project = self.service.update_target(
            project.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=project.revision,
            odoo_connection_mode="REMOTE",
            odoo_base_url="https://odoo.example.test",
            odoo_database="odoo_review",
            intended_applications=[],
            intended_models=[],
        )
        project = self.service.add_source_file(
            project.project_id,
            actor=LOCAL_ACTOR,
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
            actor=LOCAL_ACTOR,
            expected_revision=project.revision,
        )
        self.assertEqual(registered.status, ProjectStatus.REGISTERED)
        self.assertIsNotNone(registered.registered_at)
        persisted = self.repository.get(project.project_id)
        self.assertEqual(persisted.source_files, registered.source_files)
        manifest = (
            self.repository.project_directory(project.project_id)
            / "audit"
            / f"project-registration-r{registered.revision}.json"
        )
        self.assertTrue(manifest.is_file())
        manifest_text = manifest.read_text()
        self.assertIn('"approval_status":"NOT_STARTED"', manifest_text)
        self.assertIn('"odoo_connection_mode":"REMOTE"', manifest_text)


class SourceIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.repository = ProjectRepository(DuckDbDatabase(self.temporary.name))
        self.projects = ProjectService(
            self.repository,
            CapabilityAuthorizationPolicy(),
        )
        self.artifacts = LocalArtifactStore(self.temporary.name)
        self.intake = SourceIntakeService(self.projects, self.artifacts)
        self.project = self.projects.create_project(
            actor=LOCAL_ACTOR,
            name="Source intake",
            source_system="CSV",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_csv_is_hashed_and_stored_under_generated_name(self) -> None:
        source = self.intake.accept(
            self.project.project_id,
            actor=LOCAL_ACTOR,
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
                    actor=LOCAL_ACTOR,
                    expected_revision=self.project.revision,
                    display_name=filename,
                    stream=BytesIO(b"unsafe"),
                )

    def test_invalid_xlsx_is_rejected_by_isolated_worker(self) -> None:
        with self.assertRaises(SourceIntakeError):
            self.intake.accept(
                self.project.project_id,
                actor=LOCAL_ACTOR,
                expected_revision=self.project.revision,
                display_name="not-a-workbook.xlsx",
                stream=BytesIO(b"not a ZIP container"),
            )
