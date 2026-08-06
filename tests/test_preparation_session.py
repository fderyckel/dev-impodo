from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from impodo.access import LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.domain.staging.preparation_session import (
    CanonicalPreparedSessionRow,
    PreparationSessionBindings,
    PreparationSessionStatus,
    PreparedSessionRow,
)
from impodo.domain.staging.transformation_impact import (
    TransformationImpactReport,
    TransformationImpactRow,
)
from impodo.models import LogicalReference, PreparedRecord, canonical_json_bytes
from impodo.projects import MigrationProject, OdooConnectionMode, ProjectStatus
from impodo.staging_contracts import (
    BROWSER_EVALUATOR_VERSION,
    STAGING_CONTRACT_VERSION,
    StagingDatasetRole,
    StagingDisposition,
    canonical_row_from_prepared,
)


ROOT = Path(__file__).resolve().parents[1]
PHYSICAL_HASH = "sha256:" + "1" * 64
SELECTION_HASH = "sha256:" + "2" * 64
MAPPING_HASH = "sha256:" + "3" * 64
SCHEMA_HASH = "sha256:" + "4" * 64
SOURCE_HASH = "sha256:" + "5" * 64
PLAN_HASH = "sha256:" + "6" * 64


class PreparationSessionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        database = DuckDbDatabase(self.temporary.name)
        self.projects = ProjectRepository(database)
        self.repository = PreparationSessionRepository(database)
        now = datetime.now(timezone.utc)
        self.project = MigrationProject(
            project_id=str(uuid4()),
            name="Bounded preparation",
            source_system="CSV",
            data_manager="Data Manager",
            functional_owner="Functional Owner",
            business_unit="Operations",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_models=("res.partner",),
            status=ProjectStatus.REGISTERED,
            registered_at=now,
        )
        self.projects.create(self.project, actor=LOCAL_ACTOR)
        self.bindings = PreparationSessionBindings(
            mapping_id="mapping:contacts",
            mapping_version=1,
            physical_selection_hash=PHYSICAL_HASH,
            source_selection_hash=SELECTION_HASH,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            derived_plan_hash=None,
            compiled_plan_hash=PLAN_HASH,
            contract_version=STAGING_CONTRACT_VERSION,
            evaluator_version=BROWSER_EVALUATOR_VERSION,
            source_hashes={"contacts": SOURCE_HASH},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_typed_rows_duplicates_impacts_and_lineage_finalize_in_batches(
        self,
    ) -> None:
        session = self.repository.begin_session(
            self.project.project_id,
            self.bindings,
        )
        incoming = LogicalReference(
            origin="incoming",
            key=("COMPANY-BE",),
            dataset="companies",
            target_fields=("name",),
        )
        rows = tuple(
            PreparedSessionRow(
                record=PreparedRecord(
                    dataset="contacts",
                    source_row=source_row,
                    target_model="res.partner",
                    source_identity=("C001",),
                    target_identity=("C001", incoming),
                    target_scope=(),
                    scalar_values={"credit_limit": Decimal("10.2500")},
                    references={"company_id": incoming},
                    source_trace_id=f"sha256:{source_row:064x}",
                ),
                physical_sources={"dataset:contacts": (source_row,)},
            )
            for source_row in (2, 3)
        )
        self.repository.append_provisional_rows(
            self.project.project_id,
            session.session_id,
            rows,
        )
        impact = TransformationImpactRow(
            dataset="contacts",
            source_row=2,
            source_column="Credit limit",
            target_field="credit_limit",
            raw_value=" 10.2500 ",
            proposed_value="10.2500",
            rules="Trim + Parse decimal",
            outcome="changed",
        )
        self.repository.append_impacts(
            self.project.project_id,
            session.session_id,
            (impact,),
        )

        stored = self.repository.finalize_session(
            self.project.project_id,
            session.session_id,
            modes={"contacts": "upsert"},
            field_sources={
                "contacts": {
                    "$source_identity": ("column:reference",),
                    "credit_limit": ("column:credit_limit",),
                }
            },
            dataset_evidence={
                "contacts": (
                    "dataset:contacts",
                    StagingDatasetRole.DIRECT,
                    2,
                    "res.partner",
                )
            },
            run_issues=(),
            control_totals=(),
            impact_report=TransformationImpactReport(
                mapping_content_hash=MAPPING_HASH,
                evaluated_count=1,
                changed_count=1,
                fallback_count=0,
                null_count=0,
                invalid_count=0,
                provided_count=0,
                unchanged_count=0,
                rows=(),
                detail_limit=0,
            ),
        )

        self.assertEqual(len(stored.rows), 2)
        self.assertEqual(len(stored.rows[:1]), 1)
        restored = tuple(stored.rows)
        self.assertTrue(
            all(row.disposition is StagingDisposition.BLOCKED for row in restored)
        )
        self.assertTrue(
            all(
                any(issue.code == "SOURCE_IDENTITY_DUPLICATE" for issue in row.issues)
                for row in restored
            )
        )
        self.assertIsInstance(restored[0].references["company_id"], LogicalReference)
        self.assertEqual(
            restored[0].proposed_values["credit_limit"],
            Decimal("10.2500"),
        )
        self.assertEqual(
            self.repository.physical_rows(
                self.project.project_id,
                session.session_id,
            ),
            {"dataset:contacts": (2, 3)},
        )
        self.assertEqual(
            tuple(
                self.repository.iter_impacts(
                    self.project.project_id,
                    session.session_id,
                )
            ),
            (impact,),
        )
        summary = self.repository.get_session(
            self.project.project_id,
            session.session_id,
        )
        self.assertEqual(summary.status, PreparationSessionStatus.READY)
        self.assertEqual(summary.provisional_row_count, 2)
        self.assertEqual(summary.canonical_row_count, 2)
        self.assertEqual(summary.impact_row_count, 1)

    def test_failure_removes_temporary_values_but_retains_safe_status(self) -> None:
        session = self.repository.begin_session(
            self.project.project_id,
            self.bindings,
        )
        row = PreparedSessionRow(
            record=PreparedRecord(
                dataset="contacts",
                source_row=2,
                target_model="res.partner",
                source_identity=("C001",),
                target_identity=("C001",),
                target_scope=(),
                scalar_values={"name": "Alice"},
                references={},
            ),
            physical_sources={"dataset:contacts": (2,)},
        )
        self.repository.append_provisional_rows(
            self.project.project_id,
            session.session_id,
            (row,),
        )

        self.repository.fail_session(
            self.project.project_id,
            session.session_id,
            "SOURCE_HASH_MISMATCH",
        )

        summary = self.repository.get_session(
            self.project.project_id,
            session.session_id,
        )
        self.assertEqual(summary.status, PreparationSessionStatus.FAILED)
        self.assertEqual(summary.failure_code, "SOURCE_HASH_MISMATCH")
        database_path = (
            self.repository.project_directory(self.project.project_id)
            / "project.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            stored = connection.execute(
                """
                SELECT COUNT(*) FROM preparation_provisional_row
                 WHERE session_id = ?
                """,
                [session.session_id],
            ).fetchone()
        self.assertEqual(stored, (0,))

    def test_encoded_canonical_duplicates_use_exception_finalization(self) -> None:
        session = self.repository.begin_session(
            self.project.project_id,
            self.bindings,
        )
        rows: list[CanonicalPreparedSessionRow] = []
        for ordinal, source_row in enumerate((2, 3)):
            record = PreparedRecord(
                dataset="contacts",
                source_row=source_row,
                target_model="res.partner",
                source_identity=("C001",),
                target_identity=("C001",),
                target_scope=(),
                scalar_values={"name": "Alice"},
                references={},
            )
            canonical = canonical_row_from_prepared(
                record,
                mode="upsert",
                source_hash=SOURCE_HASH,
                source_selection_hash=SELECTION_HASH,
                mapping_hash=MAPPING_HASH,
                schema_hash=SCHEMA_HASH,
                derived_plan_hash=None,
                field_sources={"name": ("column:name",)},
                physical_dataset_id="dataset:contacts",
                physical_source_rows=(source_row,),
            )
            rows.append(
                CanonicalPreparedSessionRow(
                    row_id=canonical.row_id,
                    ordinal=ordinal,
                    dataset=canonical.dataset,
                    source_row=canonical.source_row,
                    target_model=canonical.target_model,
                    disposition=canonical.disposition,
                    source_identity=canonical.source_identity,
                    row_json=canonical_json_bytes(
                        canonical.to_portable_dict()
                    ).decode("utf-8"),
                    physical_sources={"dataset:contacts": (source_row,)},
                )
            )
        self.repository.append_session_batch(
            self.project.project_id,
            session.session_id,
            rows,
            (),
        )

        stored = self.repository.finalize_session(
            self.project.project_id,
            session.session_id,
            modes={"contacts": "upsert"},
            field_sources={"contacts": {"name": ("column:name",)}},
            dataset_evidence={
                "contacts": (
                    "dataset:contacts",
                    StagingDatasetRole.DIRECT,
                    2,
                    "res.partner",
                )
            },
            run_issues=(),
            control_totals=(),
            impact_report=TransformationImpactReport(
                mapping_content_hash=MAPPING_HASH,
                evaluated_count=0,
                changed_count=0,
                fallback_count=0,
                null_count=0,
                invalid_count=0,
                provided_count=0,
                unchanged_count=0,
                rows=(),
                detail_limit=0,
            ),
        )

        restored = tuple(stored.rows)
        self.assertEqual(len(restored), 2)
        self.assertTrue(
            all(row.disposition is StagingDisposition.BLOCKED for row in restored)
        )
        self.assertTrue(
            all(
                any(issue.code == "SOURCE_IDENTITY_DUPLICATE" for issue in row.issues)
                for row in restored
            )
        )

    def test_direct_rows_finalize_in_place_and_failed_pending_run_is_removed(
        self,
    ) -> None:
        session = self.repository.begin_direct_session(
            self.project.project_id,
            self.bindings,
            actor=LOCAL_ACTOR,
        )
        rows: list[CanonicalPreparedSessionRow] = []
        for ordinal, source_row in enumerate((2, 3)):
            record = PreparedRecord(
                dataset="contacts",
                source_row=source_row,
                target_model="res.partner",
                source_identity=("C001",),
                target_identity=("C001",),
                target_scope=(),
                scalar_values={"name": "Alice"},
                references={},
            )
            canonical = canonical_row_from_prepared(
                record,
                mode="upsert",
                source_hash=SOURCE_HASH,
                source_selection_hash=SELECTION_HASH,
                mapping_hash=MAPPING_HASH,
                schema_hash=SCHEMA_HASH,
                derived_plan_hash=None,
                field_sources={"name": ("column:name",)},
                physical_dataset_id="dataset:contacts",
                physical_source_rows=(source_row,),
            )
            rows.append(
                CanonicalPreparedSessionRow(
                    row_id=canonical.row_id,
                    ordinal=ordinal,
                    dataset=canonical.dataset,
                    source_row=canonical.source_row,
                    target_model=canonical.target_model,
                    disposition=canonical.disposition,
                    source_identity=canonical.source_identity,
                    row_json=canonical_json_bytes(
                        canonical.to_portable_dict()
                    ).decode("utf-8"),
                    physical_sources={"dataset:contacts": (source_row,)},
                )
            )
        self.repository.append_direct_rows(
            self.project.project_id,
            session.session_id,
            rows,
        )
        stored = self.repository.finalize_direct_session(
            self.project.project_id,
            session.session_id,
            dataset_evidence={
                "contacts": (
                    "dataset:contacts",
                    StagingDatasetRole.DIRECT,
                    2,
                    "res.partner",
                )
            },
            run_issues=(),
            control_totals=(),
            impact_report=TransformationImpactReport(
                mapping_content_hash=MAPPING_HASH,
                evaluated_count=0,
                changed_count=0,
                fallback_count=0,
                null_count=0,
                invalid_count=0,
                provided_count=0,
                unchanged_count=0,
                rows=(),
                detail_limit=0,
            ),
        )

        restored = tuple(stored.rows)
        self.assertEqual(len(restored), 2)
        self.assertTrue(
            all(row.disposition is StagingDisposition.BLOCKED for row in restored)
        )
        database_path = (
            self.repository.project_directory(self.project.project_id)
            / "project.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_staging_row WHERE run_id = ?),
                    (SELECT COUNT(*) FROM preparation_provisional_row WHERE session_id = ?),
                    (SELECT COUNT(*) FROM preparation_final_row WHERE session_id = ?),
                    (SELECT status FROM canonical_staging_run WHERE run_id = ?)
                """,
                [
                    session.session_id,
                    session.session_id,
                    session.session_id,
                    session.session_id,
                ],
            ).fetchone()
        self.assertEqual(counts, (2, 0, 0, "PENDING"))

        self.repository.fail_session(
            self.project.project_id,
            session.session_id,
            "DIRECT_PUBLICATION_FAILED",
        )
        with self.repository._connect(database_path) as connection:
            remaining = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_staging_row WHERE run_id = ?),
                    (SELECT COUNT(*) FROM canonical_staging_run WHERE run_id = ?)
                """,
                [session.session_id, session.session_id],
            ).fetchone()
        self.assertEqual(remaining, (0, 0))


if __name__ == "__main__":
    unittest.main()
