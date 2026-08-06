from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

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
from impodo.adapters.duckdb.constants import SCHEMA_VERSION
from impodo.projects import (
    OdooConnectionMode,
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
        database = DuckDbDatabase(self.temporary.name)
        self.repository = ProjectRepository(database)
        self.schemas = SchemaRepository(database)
        self.transformation_impacts = TransformationImpactRepository(database)
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

    def test_version_one_project_database_is_migrated(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Legacy project",
            source_system="CSV",
        )
        database_path = (
            self.repository.project_directory(project.project_id)
            / "project.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            connection.execute("DROP TABLE source_catalog")
            connection.execute(
                "ALTER TABLE project DROP COLUMN odoo_connection_mode"
            )
            connection.execute("UPDATE schema_version SET version = 1")

        migrated = self.repository.get(project.project_id)
        self.assertEqual(
            migrated.odoo_connection_mode,
            OdooConnectionMode.REMOTE,
        )
        with self.repository._connect(database_path) as connection:
            version = connection.execute(
                "SELECT version FROM schema_version"
            ).fetchone()
            catalog_table = connection.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_name = 'source_catalog'
                """
            ).fetchone()
            mapping_table = connection.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_name = 'mapping_revision'
                """
            ).fetchone()
            working_draft_table = connection.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_name = 'mapping_working_draft'
                """
            ).fetchone()
            model_catalog_table = connection.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_name = 'odoo_model_catalog'
                """
            ).fetchone()
        self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertEqual(catalog_table, ("source_catalog",))
        self.assertEqual(mapping_table, ("mapping_revision",))
        self.assertEqual(
            working_draft_table,
            ("mapping_working_draft",),
        )
        self.assertEqual(model_catalog_table, ("odoo_model_catalog",))

    def test_version_seven_target_label_is_removed_fail_closed(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Legacy target label",
            source_system="CSV",
        )
        database_path = (
            self.repository.project_directory(project.project_id)
            / "project.duckdb"
        )
        legacy_column = "_".join(("target", "environment"))
        with self.repository._connect(database_path) as connection:
            connection.execute(
                f'ALTER TABLE project ADD COLUMN "{legacy_column}" VARCHAR'
            )

            connection.execute(
                f'UPDATE project SET "{legacy_column}" = ?',
                ["legacy-label"],
            )
            connection.execute(
                "INSERT INTO odoo_schema_catalog VALUES (1, '{}')"
            )
            connection.execute("UPDATE schema_version SET version = 7")

        migrated = self.repository.get(project.project_id)

        self.assertEqual(migrated.approval_status.value, "INVALIDATED")
        self.assertIsNone(
            self.schemas.get_odoo_schema_catalog(project.project_id)
        )
        with self.repository._connect(database_path) as connection:
            legacy_column_row = connection.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_name = 'project' AND column_name = ?
                """,
                [legacy_column],
            ).fetchone()
            migration_event = connection.execute(
                """
                SELECT event_type
                  FROM audit_event
                 WHERE event_type = 'TARGET_CONTRACT_MIGRATED'
                """
            ).fetchone()
        self.assertIsNone(legacy_column_row)
        self.assertEqual(migration_event, ("TARGET_CONTRACT_MIGRATED",))

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

        with self.assertRaisesRegex(ProjectConflictError, "reload before deleting"):
            self.service.delete_project(
                project.project_id,
                actor=LOCAL_ACTOR,
                expected_revision=project.revision,
            )
        self.assertTrue(project_dir.is_dir())

        deleted = self.service.delete_project(
            registered.project_id,
            actor=LOCAL_ACTOR,
            expected_revision=registered.revision,
        )

        self.assertEqual(deleted, registered)
        self.assertFalse(project_dir.exists())
        self.assertEqual(self.repository.list(), ())
        with self.assertRaises(ProjectNotFoundError):
            self.repository.get(registered.project_id)

    def test_version_ten_database_adds_working_mapping_draft(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Version ten project",
            source_system="CSV",
        )
        database_path = (
            self.repository.project_directory(project.project_id)
            / "project.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            connection.execute("DROP TABLE mapping_working_draft")
            connection.execute("UPDATE schema_version SET version = 10")

        self.repository.get(project.project_id)

        with self.repository._connect(database_path) as connection:
            version = connection.execute(
                "SELECT version FROM schema_version"
            ).fetchone()
            working_draft_table = connection.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_name = 'mapping_working_draft'
                """
            ).fetchone()
        self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertEqual(
            working_draft_table,
            ("mapping_working_draft",),
        )

    def test_version_sixteen_retires_and_recovers_field_list_draft(
        self,
    ) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Mapping draft retirement",
            source_system="CSV",
        )
        database_path = (
            self.repository.project_directory(project.project_id)
            / "project.duckdb"
        )
        now = datetime.now(timezone.utc)
        selection = SourceSelection(
            selection_id="selection-1",
            version=1,
            project_id=project.project_id,
            created_at=now,
            created_by="Migration test",
            datasets=(
                SourceDataset(
                    dataset_id="dataset:customers",
                    name="customers",
                    file_id="file-1",
                    table_key="csv",
                    source_sha256="a" * 64,
                    catalog_hash="sha256:" + "b" * 64,
                    encoding="utf-8",
                    delimiter=",",
                    header_row=1,
                    row_count=1,
                    columns=(
                        SourceDatasetColumn(
                            ordinal=1,
                            source_name="name",
                            stable_key="column:1:name",
                            candidate_type="text",
                        ),
                    ),
                ),
            ),
            content_hash="sha256:" + "c" * 64,
        )
        schema_hash = "sha256:" + "d" * 64
        old_payload = {
            "mapping_id": "mapping-1",
            "version": 3,
            "status": "DRAFT",
            "source_selection_hash": selection.content_hash,
            "schema_hash": schema_hash,
            "updated_at": now.isoformat(),
            "updated_by": "Migration test",
            "entries": [
                {
                    "dataset_name": "customers",
                    "source_column": "name",
                    "target_model": "res.partner",
                    "target_field": "name",
                }
            ],
        }
        old_payload["content_hash"] = content_hash(
            {
                key: old_payload[key]
                for key in (
                    "mapping_id",
                    "version",
                    "status",
                    "source_selection_hash",
                    "schema_hash",
                    "entries",
                )
            }
        )
        with self.repository._connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE mapping_draft (
                    singleton_id INTEGER PRIMARY KEY,
                    draft_json VARCHAR NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO mapping_draft VALUES (1, ?)",
                [json.dumps(old_payload)],
            )
            connection.execute(
                "INSERT INTO source_selection VALUES (1, ?)",
                [selection.to_json()],
            )
            connection.execute(
                "INSERT INTO odoo_schema_catalog VALUES (1, ?)",
                [json.dumps({"content_hash": schema_hash})],
            )
            connection.execute("UPDATE schema_version SET version = 16")

        self.repository.get(project.project_id)

        with self.repository._connect(database_path) as connection:
            tables = {
                str(row[0])
                for row in connection.execute("SHOW TABLES").fetchall()
            }
            retired = connection.execute(
                """
                SELECT retirement_reason, payload_json
                  FROM retired_evidence
                 WHERE evidence_type = 'FIELD_LIST_MAPPING_DRAFT'
                   AND evidence_key = 'singleton'
                """
            ).fetchone()
            recovered_json = connection.execute(
                """
                SELECT draft_json
                  FROM mapping_working_draft
                 WHERE singleton_id = 1
                """
            ).fetchone()[0]
        recovered = MappingWorkingDraft.from_json(str(recovered_json))
        self.assertEqual(
            retired,
            ("CONVERTED_TO_WORKING_DRAFT", json.dumps(old_payload)),
        )
        self.assertNotIn("mapping_draft", tables)
        self.assertEqual(recovered.mapping_id, "mapping-1")
        self.assertEqual(
            recovered.definition.datasets[0].fields[0].source_column_key,
            "column:1:name",
        )

    def test_version_eleven_database_adds_durable_staging(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Version eleven project",
            source_system="CSV",
        )
        database_path = (
            self.repository.project_directory(project.project_id)
            / "project.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            connection.execute("DROP TABLE canonical_staging_current")
            connection.execute("DROP TABLE canonical_staging_row")
            connection.execute("DROP TABLE canonical_staging_run")
            connection.execute(
                "ALTER TABLE readiness_run DROP COLUMN staging_run_id"
            )
            connection.execute(
                "ALTER TABLE readiness_run DROP COLUMN staging_content_hash"
            )
            connection.execute("UPDATE schema_version SET version = 11")

        self.repository.get(project.project_id)

        with self.repository._connect(database_path) as connection:
            version = connection.execute(
                "SELECT version FROM schema_version"
            ).fetchone()
            tables = {
                str(item[0])
                for item in connection.execute(
                    """
                    SELECT table_name
                      FROM information_schema.tables
                     WHERE table_name LIKE 'canonical_staging_%'
                    """
                ).fetchall()
            }
            readiness_columns = {
                str(item[0])
                for item in connection.execute(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_name = 'readiness_run'
                    """
                ).fetchall()
            }
            staging_columns = {
                str(item[0])
                for item in connection.execute(
                    """
                    SELECT column_name
                      FROM information_schema.columns
                     WHERE table_name = 'canonical_staging_run'
                    """
                ).fetchall()
            }
        self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertEqual(
            tables,
            {
                "canonical_staging_current",
                "canonical_staging_row",
                "canonical_staging_run",
            },
        )
        self.assertIn("staging_run_id", readiness_columns)
        self.assertIn("staging_content_hash", readiness_columns)
        self.assertIn("compiled_plan_hash", staging_columns)

    def test_version_seventeen_preserves_history_without_promoting_it(self) -> None:
        project = self.service.create_project(
            actor=LOCAL_ACTOR,
            name="Version seventeen project",
            source_system="CSV",
        )
        database_path = (
            self.repository.project_directory(project.project_id)
            / "project.duckdb"
        )
        historical_run_id = str(uuid4())
        with self.repository._connect(database_path) as connection:
            connection.execute(
                """
                INSERT INTO readiness_run (
                    run_id, mapping_id, mapping_version,
                    mapping_content_hash, target_hash, staging_run_id,
                    staging_content_hash, quality_run_id,
                    quality_content_hash, checked_at, checked_by, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    historical_run_id,
                    "legacy-mapping",
                    1,
                    "sha256:" + "1" * 64,
                    "sha256:" + "2" * 64,
                    "",
                    "",
                    "",
                    "",
                    "2026-08-01T00:00:00+00:00",
                    "Legacy operator",
                    "{}",
                ],
            )
            for table in (
                "preflight_current",
                "preflight_dataset",
                "preflight_decision",
                "preflight_target_snapshot",
                "preflight_transition",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("UPDATE schema_version SET version = 17")

        self.repository.get(project.project_id)

        with self.repository._connect(database_path) as connection:
            version = connection.execute(
                "SELECT version FROM schema_version"
            ).fetchone()
            historical = connection.execute(
                """
                SELECT run_id, normalization_run_id, requirement_plan_hash
                  FROM readiness_run
                 WHERE run_id = ?
                """,
                [historical_run_id],
            ).fetchone()
            current_count = connection.execute(
                "SELECT COUNT(*) FROM preflight_current"
            ).fetchone()
        self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertEqual(historical, (historical_run_id, "", ""))
        self.assertEqual(current_count, (0,))

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
