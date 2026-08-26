from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.application.workspace.preparation.bounded_quality import (
    build_bounded_quality_run,
    materialize_staging_run,
)
from impodo.domain.staging.preparation_session import (
    CanonicalPreparedSessionRow,
    PreparationSessionBindings,
)
from impodo.domain.staging.canonical_projection import (
    canonical_quality_identity_key,
    canonical_quality_record_label,
)
from impodo.domain.prepared_snapshot import PreparedSnapshot
from impodo.domain.staging.transformation_impact import (
    TransformationImpactReport,
)
from impodo.domain.shared.models import LogicalReference, PreparedRecord, canonical_json_bytes
from impodo.domain.workspace.workbench import WorkspaceState, OdooConnectionMode, WorkspaceStatus
from impodo.domain.preparation.quality import (
    QualityOutcomePolicy,
    QualityRuleFamily,
    QualityRun,
    default_quality_ruleset,
    evaluate_quality,
)
from impodo.domain.preparation.staging_contracts import (
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
        database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.workspace_states = WorkspaceStateRepository(database)
        self.repository = PreparationSessionRepository(database)
        now = datetime.now(timezone.utc)
        self.workspace_state = WorkspaceState(
            workspace_id=str(uuid4()),
            name="Bounded preparation",
            source_system="CSV",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_local",
            intended_models=("res.partner",),
            status=WorkspaceStatus.REGISTERED,
            registered_at=now,
        )
        self.workspace_states.initialize_workbench(self.workspace_state, actor=LOCAL_ACTOR)
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

    def test_direct_rows_finalize_in_place_and_failed_pending_run_is_removed(
        self,
    ) -> None:
        session = self.repository.begin_direct_session(
            self.workspace_state.workspace_id,
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
                    row_json=canonical_json_bytes(canonical.to_portable_dict()).decode(
                        "utf-8"
                    ),
                    physical_sources={"dataset:contacts": (source_row,)},
                )
            )
        self.repository.append_direct_rows(
            self.workspace_state.workspace_id,
            session.session_id,
            rows,
        )
        finalization_arguments = {
            "dataset_evidence": {
                "contacts": (
                    "dataset:contacts",
                    StagingDatasetRole.DIRECT,
                    2,
                    "res.partner",
                )
            },
            "run_issues": (),
            "control_totals": (),
            "impact_report": TransformationImpactReport(
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
        }
        original_update = self.repository._update_direct_rows
        failed_once = False

        def fail_after_first_update(*args, **kwargs):
            nonlocal failed_once
            original_update(*args, **kwargs)
            if not failed_once:
                failed_once = True
                raise RuntimeError("injected interrupted finalization")

        with patch.object(
            self.repository,
            "_update_direct_rows",
            side_effect=fail_after_first_update,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "injected interrupted finalization",
            ):
                self.repository.finalize_direct_session(
                    self.workspace_state.workspace_id,
                    session.session_id,
                    **finalization_arguments,
                )

        stored = self.repository.finalize_direct_session(
            self.workspace_state.workspace_id,
            session.session_id,
            **finalization_arguments,
        )

        restored = tuple(stored.rows)
        self.assertEqual(len(restored), 2)
        self.assertTrue(
            all(row.disposition is StagingDisposition.BLOCKED for row in restored)
        )
        self.assertTrue(
            all(
                sum(issue.code == "SOURCE_IDENTITY_DUPLICATE" for issue in row.issues)
                == 1
                for row in restored
            )
        )
        database_path = (
            self.repository.workspace_directory(self.workspace_state.workspace_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            counts = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM canonical_staging_row WHERE run_id = ?),
                    (SELECT status FROM canonical_staging_run WHERE run_id = ?)
                """,
                [
                    session.session_id,
                    session.session_id,
                ],
            ).fetchone()
        self.assertEqual(counts, (2, "PENDING"))

        self.repository.fail_session(
            self.workspace_state.workspace_id,
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

    def test_prepared_snapshot_pointer_advances_only_after_publication(self) -> None:
        dataset_id = "dataset:" + "a" * 24

        def snapshot(source_digit: str, parquet_digit: str) -> PreparedSnapshot:
            return PreparedSnapshot.create(
                workspace_id=self.workspace_state.workspace_id,
                dataset_id=dataset_id,
                dataset_name="contacts",
                source_snapshot_hash="sha256:" + source_digit * 64,
                mapping_hash=MAPPING_HASH,
                schema_hash=SCHEMA_HASH,
                transformation_program_hash=PLAN_HASH,
                row_count=0,
                physical_schema_hash="sha256:" + "8" * 64,
                parquet_sha256="sha256:" + parquet_digit * 64,
                created_at=datetime.now(timezone.utc),
            )

        first = snapshot("7", "9")
        published = self.repository.begin_direct_session(
            self.workspace_state.workspace_id,
            self.bindings,
            actor=LOCAL_ACTOR,
        )
        self.repository.bind_prepared_snapshot(
            self.workspace_state.workspace_id,
            published.session_id,
            first,
        )
        self.assertEqual(
            self.repository.find_prepared_snapshot(
                self.workspace_state.workspace_id,
                dataset_id,
                first.logical_hash,
            ),
            first,
        )
        self.assertEqual(
            self.repository.current_prepared_snapshots(self.workspace_state.workspace_id),
            (),
        )
        self.repository.finalize_direct_session(
            self.workspace_state.workspace_id,
            published.session_id,
            dataset_evidence={
                "contacts": (
                    dataset_id,
                    StagingDatasetRole.DIRECT,
                    0,
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
        self.repository.mark_published(
            self.workspace_state.workspace_id,
            published.session_id,
        )
        self.assertEqual(
            self.repository.current_prepared_snapshots(self.workspace_state.workspace_id),
            (first,),
        )

        second = snapshot("6", "5")
        failed = self.repository.begin_direct_session(
            self.workspace_state.workspace_id,
            self.bindings,
            actor=LOCAL_ACTOR,
        )
        self.repository.bind_prepared_snapshot(
            self.workspace_state.workspace_id,
            failed.session_id,
            second,
        )
        self.repository.fail_session(
            self.workspace_state.workspace_id,
            failed.session_id,
            "BOUNDED_PREPARATION_FAILED",
        )
        self.assertEqual(
            self.repository.current_prepared_snapshots(self.workspace_state.workspace_id),
            (first,),
        )
        self.assertEqual(
            self.repository.find_prepared_snapshot(
                self.workspace_state.workspace_id,
                dataset_id,
                second.logical_hash,
            ),
            second,
        )

    def test_relationship_edges_resolve_set_wise_and_preserve_explicit_states(
        self,
    ) -> None:
        bindings = replace(
            self.bindings,
            source_hashes={"bom": SOURCE_HASH, "products": SOURCE_HASH},
        )
        session = self.repository.begin_direct_session(
            self.workspace_state.workspace_id,
            bindings,
            actor=LOCAL_ACTOR,
        )

        def direct_row(
            *,
            ordinal: int,
            dataset: str,
            source_row: int,
            identity: str,
            reference: str | None = None,
        ) -> CanonicalPreparedSessionRow:
            logical = (
                LogicalReference(
                    origin="incoming",
                    key=(reference,),
                    dataset="products",
                )
                if reference is not None
                else None
            )
            record = PreparedRecord(
                dataset=dataset,
                source_row=source_row,
                target_model=(
                    "mrp.bom.line" if dataset == "bom" else "product.product"
                ),
                source_identity=(identity,),
                target_identity=(identity,),
                target_scope=(),
                scalar_values={"name": identity},
                references=({"product_id": logical} if logical is not None else {}),
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
                physical_dataset_id=f"dataset:{dataset}",
                physical_source_rows=(source_row,),
            )
            return CanonicalPreparedSessionRow(
                row_id=canonical.row_id,
                ordinal=ordinal,
                dataset=canonical.dataset,
                source_row=canonical.source_row,
                target_model=canonical.target_model,
                disposition=canonical.disposition,
                source_identity=canonical.source_identity,
                row_json=canonical_json_bytes(canonical.to_portable_dict()).decode(
                    "utf-8"
                ),
                references=record.references,
                physical_sources={f"dataset:{dataset}": (source_row,)},
                record_label=canonical_quality_record_label(
                    canonical.source_identity,
                    canonical.target_identity,
                    canonical.source_row,
                ),
                quality_identity_key=canonical_quality_identity_key(
                    dataset=canonical.dataset,
                    target_model=canonical.target_model,
                    target_identity=canonical.target_identity,
                    target_scope=canonical.target_scope,
                ),
            )

        rows = (
            direct_row(
                ordinal=0,
                dataset="bom",
                source_row=2,
                identity="BOM-1",
                reference="P1",
            ),
            direct_row(
                ordinal=1,
                dataset="bom",
                source_row=3,
                identity="BOM-2",
                reference="MISSING",
            ),
            direct_row(
                ordinal=2,
                dataset="bom",
                source_row=4,
                identity="BOM-3",
                reference="P2",
            ),
            direct_row(
                ordinal=3,
                dataset="bom",
                source_row=5,
                identity="BOM-4",
                reference="P2",
            ),
            direct_row(
                ordinal=4,
                dataset="products",
                source_row=2,
                identity="P1",
            ),
            direct_row(
                ordinal=5,
                dataset="products",
                source_row=3,
                identity="P2",
            ),
            direct_row(
                ordinal=6,
                dataset="products",
                source_row=4,
                identity="P2",
            ),
            direct_row(
                ordinal=7,
                dataset="products",
                source_row=5,
                identity="P3",
                reference="P2",
            ),
            direct_row(
                ordinal=8,
                dataset="products",
                source_row=6,
                identity="P4",
                reference="P3",
            ),
            direct_row(
                ordinal=9,
                dataset="products",
                source_row=7,
                identity="P5",
                reference="P4",
            ),
            direct_row(
                ordinal=10,
                dataset="products",
                source_row=8,
                identity="C1",
                reference="C2",
            ),
            direct_row(
                ordinal=11,
                dataset="products",
                source_row=9,
                identity="C2",
                reference="C1",
            ),
        )
        self.repository.append_direct_rows(
            self.workspace_state.workspace_id,
            session.session_id,
            rows,
        )
        stored = self.repository.finalize_direct_session(
            self.workspace_state.workspace_id,
            session.session_id,
            dataset_evidence={
                "bom": (
                    "dataset:bom",
                    StagingDatasetRole.CHILD,
                    4,
                    "mrp.bom.line",
                ),
                "products": (
                    "dataset:products",
                    StagingDatasetRole.PARENT,
                    8,
                    "product.product",
                ),
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

        database_path = (
            Path(self.temporary.name) / self.workspace_state.workspace_id / "workspace-engine.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            states = connection.execute(
                """
                SELECT normalized_key_json, match_state, resolution_state,
                       match_count, resolved_parent_ordinal IS NOT NULL
                  FROM preparation_relationship_edge
                 WHERE session_id = ?
                 ORDER BY child_ordinal
                """,
                [session.session_id],
            ).fetchall()
        self.assertEqual(
            states,
            [
                ('["P1"]', "UNIQUE", "RESOLVED", 1, True),
                ('["MISSING"]', "MISSING", "MISSING", 0, False),
                ('["P2"]', "DUPLICATE", "AMBIGUOUS", 2, False),
                ('["P2"]', "DUPLICATE", "AMBIGUOUS", 2, False),
                ('["P2"]', "DUPLICATE", "AMBIGUOUS", 2, False),
                ('["P3"]', "UNIQUE", "RESOLVED", 1, True),
                ('["P4"]', "UNIQUE", "RESOLVED", 1, True),
                ('["C2"]', "UNIQUE", "RESOLVED", 1, True),
                ('["C1"]', "UNIQUE", "RESOLVED", 1, True),
            ],
        )

        duplicate_parent_ids = tuple(
            row.row_id
            for row in stored.rows
            if row.dataset == "products" and row.source_identity == ("P2",)
        )
        execute_count = 0
        original_connect = self.repository._connect

        class CountingConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, *args, **kwargs):
                nonlocal execute_count
                execute_count += 1
                return self.connection.execute(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        @contextmanager
        def counting_connect(path):
            with original_connect(path) as connection:
                yield CountingConnection(connection)

        with (
            patch.object(self.repository, "_connect", counting_connect),
            patch.object(
                self.repository,
                "_ensure_workspace_database_schema",
                return_value=None,
            ),
        ):
            findings = stored.rows.bounded_relationship_findings(
                duplicate_parent_ids,
                ("bom", "products"),
            )
        self.assertEqual(execute_count, 9)
        self.assertEqual(
            tuple((str(item[2]), int(item[3]), str(item[7])) for item in findings),
            (
                ("bom", 3, "MISSING"),
                ("bom", 4, "AMBIGUOUS"),
                ("bom", 5, "AMBIGUOUS"),
                ("products", 5, "AMBIGUOUS"),
                ("products", 6, "UNSAFE_PARENT"),
                ("products", 7, "UNSAFE_PARENT"),
            ),
        )

        materialized = materialize_staging_run(stored)
        ruleset = default_quality_ruleset(
            workspace_id=self.workspace_state.workspace_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("bom", "products"),
        )
        physical_rows = {
            "dataset:bom": (2, 3, 4, 5),
            "dataset:products": (2, 3, 4, 5, 6, 7, 8, 9),
        }
        expected = evaluate_quality(
            workspace_state=self.workspace_state,
            staging=materialized,
            physical_rows=physical_rows,
            ruleset=ruleset,
            published_staging_content_hash=stored.validated_content_hash,
        )
        with patch.object(
            self.repository,
            "_bounded_relationship_findings",
            wraps=self.repository._bounded_relationship_findings,
        ) as relationship_pass:
            bounded = build_bounded_quality_run(
                workspace_state=self.workspace_state,
                staging=stored,
                physical_rows=physical_rows,
                ruleset=ruleset,
                published_staging_content_hash=(stored.validated_content_hash or ""),
            )
        self.assertEqual(relationship_pass.call_count, 1)
        observed = QualityRun(
            workspace_id=bounded.workspace_id,
            staging_content_hash=bounded.staging_content_hash,
            ruleset_hash=bounded.ruleset_hash,
            mapping_hash=bounded.mapping_hash,
            schema_hash=bounded.schema_hash,
            retention_context_hash=bounded.retention_context_hash,
            row_results=tuple(bounded.row_results),
            source_accounting=tuple(bounded.source_accounting),
            issues=tuple(bounded.issues),
            quarantine=tuple(bounded.quarantine),
            effective_dataset_hash=bounded.effective_dataset_hash,
            evaluator_version=bounded.evaluator_version,
            contract_version=bounded.contract_version,
        )
        self.assertEqual(observed.row_results, expected.row_results)
        self.assertEqual(observed.source_accounting, expected.source_accounting)
        self.assertEqual(observed.issues, expected.issues)
        self.assertEqual(observed.quarantine, expected.quarantine)
        self.assertEqual(observed.to_json(), expected.to_json())

        warning_ruleset = replace(
            ruleset,
            rules=tuple(
                replace(rule, outcome=QualityOutcomePolicy.WARNING)
                if rule.family is QualityRuleFamily.RELATIONSHIP_READINESS
                else rule
                for rule in ruleset.rules
            ),
        )
        warning_expected = evaluate_quality(
            workspace_state=self.workspace_state,
            staging=materialized,
            physical_rows=physical_rows,
            ruleset=warning_ruleset,
            published_staging_content_hash=stored.validated_content_hash,
        )
        warning_bounded = build_bounded_quality_run(
            workspace_state=self.workspace_state,
            staging=stored,
            physical_rows=physical_rows,
            ruleset=warning_ruleset,
            published_staging_content_hash=stored.validated_content_hash or "",
        )
        warning_observed = replace(
            observed,
            ruleset_hash=warning_bounded.ruleset_hash,
            row_results=tuple(warning_bounded.row_results),
            source_accounting=tuple(warning_bounded.source_accounting),
            issues=tuple(warning_bounded.issues),
            quarantine=tuple(warning_bounded.quarantine),
            effective_dataset_hash=warning_bounded.effective_dataset_hash,
        )
        self.assertEqual(
            warning_observed.to_json(),
            warning_expected.to_json(),
        )

        with self.repository._connect(database_path) as connection:
            connection.execute(
                "UPDATE canonical_staging_run SET status = 'PUBLISHED' "
                "WHERE run_id = ?",
                [session.session_id],
            )
        self.repository.mark_published(
            self.workspace_state.workspace_id,
            session.session_id,
        )
        with self.repository._connect(database_path) as connection:
            retained_edges = connection.execute(
                "SELECT COUNT(*) FROM preparation_relationship_edge "
                "WHERE session_id = ?",
                [session.session_id],
            ).fetchone()
            relationship_indexes = connection.execute(
                "SELECT COUNT(*) FROM duckdb_indexes() "
                "WHERE table_name = 'preparation_relationship_edge'"
            ).fetchone()
        self.assertEqual(retained_edges, (9,))
        self.assertEqual(relationship_indexes, (0,))


if __name__ == "__main__":
    unittest.main()
