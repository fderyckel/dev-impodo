from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
from time import perf_counter
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from impodo.application import preparation_service as preparation_module
from impodo.artifacts import LocalArtifactStore
from impodo.domain.mapping.artifacts import MappingRevision, MappingSubmission
from impodo.domain.mapping.contracts import (
    BusinessControlTotal,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    MappingTargetMode,
    ScalarFieldMapping,
)
from impodo.domain.mapping.validation.evidence import (
    MappingValidationResult,
    MappingValidationStatus,
)
from impodo.inspection import (
    SourceColumnProfile,
    SourceFileCatalog,
    SourceTableCatalog,
)
from impodo.intake import CHUNK_BYTES, MAX_SOURCE_BYTES
from impodo.projects import (
    OdooConnectionMode,
    ProjectStatus,
    SourceFile,
)
from impodo.value_rules import ScalarTransformPolicy
from impodo.web.app import create_local_app


ROOT = Path(__file__).resolve().parents[1]
PREPARATION_SCALE_ROWS = int(
    os.environ.get("IMPODO_PREPARATION_SCALE_ROWS", "100000")
)
PREPARATION_SCALE_COLUMNS = int(
    os.environ.get("IMPODO_PREPARATION_SCALE_COLUMNS", "30")
)
PREPARATION_SCALE_MAPPED_FIELDS = int(
    os.environ.get("IMPODO_PREPARATION_SCALE_MAPPED_FIELDS", "20")
)
PREPARATION_TIME_LIMIT_SECONDS = 120
PREPARATION_PEAK_WORKING_SET_LIMIT_MIB = 900


@unittest.skipUnless(
    os.environ.get("IMPODO_RUN_PREPARATION_SCALE") == "1",
    "100,000-row preparation scale probe is opt-in",
)
class PreparationWorkflowScaleTests(unittest.TestCase):
    """Exercise the real local preparation services and DuckDB repositories."""

    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        self.root = Path(self.temporary.name)
        self.artifacts = LocalArtifactStore(self.root)
        self.app = create_local_app(
            self.root,
            artifact_store=self.artifacts,
        )
        self.context = self.app.state.context

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_complete_preparation_workflow(self) -> None:
        import psutil

        if PREPARATION_SCALE_COLUMNS < PREPARATION_SCALE_MAPPED_FIELDS:
            self.fail("The source fixture must include every mapped field")
        if PREPARATION_SCALE_MAPPED_FIELDS < 3:
            self.fail("The benchmark requires identity, changed, and numeric fields")

        fixture_started = perf_counter()
        project_id, source_sha256, source_size_bytes = (
            self._prepare_project_and_evidence(
                row_count=PREPARATION_SCALE_ROWS,
                column_count=PREPARATION_SCALE_COLUMNS,
                mapped_field_count=PREPARATION_SCALE_MAPPED_FIELDS,
            )
        )
        fixture_seconds = perf_counter() - fixture_started

        phases: dict[str, float] = {}

        def timed(name, callback):
            def invoke(*args, **kwargs):
                started = perf_counter()
                try:
                    return callback(*args, **kwargs)
                finally:
                    phases[name] = perf_counter() - started

            return invoke

        original_stage = preparation_module.stage_browser_mapping
        original_publish = (
            self.context.preparation.staging.publish_canonical_staging
        )
        original_quality = self.context.preparation.quality.evaluate_and_publish
        original_normalization = (
            self.context.preparation.normalization.evaluate_and_publish
        )

        started = perf_counter()
        with (
            patch(
                "impodo.application.preparation_service."
                "require_supported_browser_scale",
            ),
            patch(
                "impodo.domain.staging.evaluator."
                "require_supported_browser_scale",
            ),
            patch(
                "impodo.application.preparation_service.stage_browser_mapping",
                timed("load_and_evaluate", original_stage),
            ),
            patch.object(
                self.context.preparation.staging,
                "publish_canonical_staging",
                timed("staging_publication", original_publish),
            ),
            patch.object(
                self.context.preparation.quality,
                "evaluate_and_publish",
                timed("quality", original_quality),
            ),
            patch.object(
                self.context.preparation.normalization,
                "evaluate_and_publish",
                timed("normalization", original_normalization),
            ),
        ):
            normalization = self.context.preparation.prepare(
                project_id,
                actor=self.context.actor,
            )
        elapsed = perf_counter() - started

        staging = (
            self.context.preparation.staging.get_current_staging_summary(
                project_id
            )
        )
        quality = self.context.preparation.quality.current_summary(project_id)
        current_normalization = (
            self.context.preparation.normalization.current_summary(project_id)
        )
        self.assertIsNotNone(staging)
        self.assertIsNotNone(quality)
        self.assertIsNotNone(current_normalization)
        assert staging is not None
        assert quality is not None
        assert current_normalization is not None

        memory = psutil.Process().memory_info()
        peak_mib = getattr(memory, "peak_wset", memory.rss) / (1024 * 1024)
        database_path = self.root / project_id / "project.duckdb"
        database_mib = database_path.stat().st_size / (1024 * 1024)
        phase_text = ", ".join(
            f"{name}={seconds:.3f}s"
            for name, seconds in phases.items()
        )
        print(
            "Complete preparation scale probe: "
            f"rows={PREPARATION_SCALE_ROWS:,}, "
            f"columns={PREPARATION_SCALE_COLUMNS}, "
            f"mapped_fields={PREPARATION_SCALE_MAPPED_FIELDS}, "
            f"fixture={fixture_seconds:.3f}s, total={elapsed:.3f}s, "
            f"peak={peak_mib:.1f} MiB, database={database_mib:.1f} MiB, "
            f"source_bytes={source_size_bytes}, source_sha256={source_sha256}, "
            f"{phase_text}, staging_hash={staging.content_hash}, "
            f"quality_hash={quality.content_hash}, "
            f"normalization_hash={normalization.content_hash}"
        )

        self.assertEqual(staging.total_rows, PREPARATION_SCALE_ROWS)
        self.assertEqual(quality.ready_count, PREPARATION_SCALE_ROWS)
        self.assertEqual(quality.review_count, 0)
        self.assertEqual(quality.quarantined_count, 0)
        self.assertEqual(quality.blocked_count, 0)
        self.assertEqual(
            current_normalization.content_hash,
            normalization.content_hash,
        )
        self.assertEqual(
            normalization.eligible_record_count,
            PREPARATION_SCALE_ROWS,
        )
        self.assertEqual(
            normalization.changed_record_count,
            PREPARATION_SCALE_ROWS,
        )
        self.assertEqual(staging.failed_control_total_count, 0)

        if PREPARATION_SCALE_ROWS >= 100_000:
            failures = []
            if elapsed >= PREPARATION_TIME_LIMIT_SECONDS:
                failures.append(
                    f"{elapsed:.3f}s is not below "
                    f"{PREPARATION_TIME_LIMIT_SECONDS}s"
                )
            if peak_mib >= PREPARATION_PEAK_WORKING_SET_LIMIT_MIB:
                failures.append(
                    f"{peak_mib:.1f} MiB is not below "
                    f"{PREPARATION_PEAK_WORKING_SET_LIMIT_MIB} MiB"
                )
            if failures:
                self.fail("; ".join(failures))

    def _prepare_project_and_evidence(
        self,
        *,
        row_count: int,
        column_count: int,
        mapped_field_count: int,
    ) -> tuple[str, str, int]:
        project = self.context.projects.create_project(
            actor=self.context.actor,
            name="100k complete preparation benchmark",
            source_system="Deterministic CSV fixture",
        )
        source_path = self.root / "preparation-scale-input.csv"
        headers = _headers(column_count)
        with source_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(headers)
            for index in range(row_count):
                writer.writerow(_source_values(index, column_count))

        file_id = str(uuid4())
        with source_path.open("rb") as stream:
            stored = self.artifacts.store_source(
                project.project_id,
                artifact_id=file_id,
                suffix=".csv",
                stream=stream,
                maximum_bytes=MAX_SOURCE_BYTES,
                chunk_bytes=CHUNK_BYTES,
                validator=lambda _path: None,
            )
        source = SourceFile(
            file_id=file_id,
            display_name="preparation-scale.csv",
            stored_name=stored.storage_key,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            received_at=datetime.now(timezone.utc),
        )
        project = self.context.projects.add_source_file(
            project.project_id,
            actor=self.context.actor,
            expected_revision=project.revision,
            source_file=source,
        )
        now = datetime.now(timezone.utc)
        registered = replace(
            project,
            data_manager="Performance tester",
            functional_owner="Performance tester",
            business_unit="Engineering",
            odoo_connection_mode=OdooConnectionMode.LOCAL,
            odoo_base_url="http://127.0.0.1:8069",
            odoo_database="odoo19_scale",
            intended_models=("res.partner",),
            status=ProjectStatus.REGISTERED,
            revision=project.revision + 1,
            updated_at=now,
            registered_at=now,
        )
        self.context.projects.repository.save(
            registered,
            expected_revision=project.revision,
            event_type="TEST_PROJECT_REGISTERED",
            event_detail="100k complete preparation benchmark",
            actor=self.context.actor,
        )

        catalog = _catalog(
            source,
            row_count=row_count,
            column_count=column_count,
            inspected_at=now,
        )
        self.context.sources.sources.save_source_catalogs(
            registered.project_id,
            (catalog,),
            actor=self.context.actor,
        )
        self.context.sources.confirm_source(
            registered.project_id,
            source.file_id,
            selected_table_keys=("csv",),
            warnings_acknowledged=False,
            actor=self.context.actor,
        )
        selection = self.context.sources.freeze_selection(
            registered.project_id,
            dataset_names={(source.file_id, "csv"): "contacts"},
            actor=self.context.actor,
        )
        dataset = selection.datasets[0]
        columns = dataset.columns
        fields = tuple(
            ScalarFieldMapping(
                target_field=(
                    "ref"
                    if index == 0
                    else "name"
                    if index == 1
                    else "credit_limit"
                    if index == 2
                    else f"x_scale_{index:02d}"
                ),
                source_column_key=columns[index].stable_key,
                transform=(
                    ScalarTransformPolicy(trim=True)
                    if index == 1
                    else ScalarTransformPolicy()
                ),
                value_type="decimal" if index == 2 else "string",
                required=index in {0, 1},
            )
            for index in range(mapped_field_count)
        )
        definition = MappingDefinition(
            mapping_id=str(uuid4()),
            source_selection_hash=selection.content_hash,
            schema_hash="sha256:" + "5" * 64,
            datasets=(
                DatasetMapping(
                    dataset_id=dataset.dataset_id,
                    target_model="res.partner",
                    mode=MappingTargetMode.UPSERT,
                    source_identity_column_keys=(columns[0].stable_key,),
                    target_identity=(
                        IdentityComponentMapping(
                            source_column_keys=(columns[0].stable_key,),
                            target_fields=("ref",),
                        ),
                    ),
                    fields=fields,
                    control_totals=(
                        BusinessControlTotal(
                            name="Credit limit total",
                            target_field="credit_limit",
                            expected_total=str(row_count),
                            unit="EUR",
                        ),
                    ),
                ),
            ),
        )
        validation = MappingValidationResult(
            mapping_content_hash=definition.content_hash,
            source_selection_hash=definition.source_selection_hash,
            schema_hash=definition.schema_hash,
            status=MappingValidationStatus.VALID,
            issues=(),
            coverage=(),
            deferred_runtime_checks=(),
        )
        revision = MappingRevision(
            mapping_id=definition.mapping_id,
            version=1,
            parent_version=None,
            definition=definition,
            created_at=now,
            created_by=self.context.actor.identity.display_name,
        )
        mapping_repository = self.context.mapping_workspace.mappings
        mapping_repository.save_mapping_revision(
            registered.project_id,
            revision,
            validation=validation,
            expected_parent_version=None,
            actor=self.context.actor,
        )
        mapping_repository.save_mapping_submission(
            registered.project_id,
            MappingSubmission(
                submission_id=str(uuid4()),
                mapping_id=definition.mapping_id,
                version=revision.version,
                mapping_content_hash=definition.content_hash,
                validation_hash=validation.validation_hash,
                warning_acknowledgements=(),
                submitted_at=now,
                submitted_by=self.context.actor.identity.display_name,
            ),
            actor=self.context.actor,
        )
        return registered.project_id, stored.sha256, stored.size_bytes


def _headers(column_count: int) -> tuple[str, ...]:
    return (
        "reference",
        "name",
        "credit_limit",
        *(f"field_{index:02d}" for index in range(3, column_count)),
    )


def _source_values(index: int, column_count: int) -> tuple[str, ...]:
    return (
        f"C{index:06d}",
        f" Contact {index:06d} ",
        "1",
        *(
            f"value-{column:02d}-{index % 100:02d}"
            for column in range(3, column_count)
        ),
    )


def _catalog(
    source: SourceFile,
    *,
    row_count: int,
    column_count: int,
    inspected_at: datetime,
) -> SourceFileCatalog:
    headers = _headers(column_count)
    profiles = tuple(
        SourceColumnProfile(
            ordinal=index + 1,
            name=name,
            candidate_type="decimal" if index == 2 else "string",
            null_count=0,
            non_null_count=row_count,
            distinct_count=(
                row_count if index in {0, 1} else 1 if index == 2 else 100
            ),
            distinct_count_is_exact=True,
            duplicate_count=(
                0
                if index in {0, 1}
                else row_count - 1
                if index == 2
                else max(0, row_count - 100)
            ),
            minimum="1" if index == 2 else None,
            maximum="1" if index == 2 else None,
            minimum_length=1 if index == 2 else None,
            maximum_length=1 if index == 2 else None,
        )
        for index, name in enumerate(headers)
    )
    return SourceFileCatalog(
        contract_version=1,
        file_id=source.file_id,
        display_name=source.display_name,
        source_sha256=source.sha256,
        source_size_bytes=source.size_bytes,
        format="CSV",
        inspected_at=inspected_at,
        encoding="utf-8",
        delimiter=",",
        tables=(
            SourceTableCatalog(
                table_key="csv",
                name="contacts",
                kind="CSV",
                hidden=False,
                header_row=1,
                row_count=row_count,
                column_count=column_count,
                columns=profiles,
                preview_rows=tuple(
                    _source_values(index, column_count)
                    for index in range(min(5, row_count))
                ),
            ),
        ),
    )


if __name__ == "__main__":
    unittest.main()
