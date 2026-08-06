from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import tempfile
from time import perf_counter
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from impodo.access import LOCAL_ACTOR
from impodo.governance import DryRun, DryRunStatus
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    ScalarFieldMapping,
)
from impodo.normalization import (
    NormalizationCandidate,
    NormalizationEvaluation,
    NormalizationOutcome,
    evaluate_normalization,
    start_dry_run,
)
from impodo.adapters.duckdb.database import DuckDbDatabase
from impodo.adapters.duckdb.constants import SCHEMA_VERSION
from impodo.adapters.duckdb.normalization_repository import NormalizationRepository
from impodo.adapters.duckdb.project_repository import ProjectRepository
from impodo.adapters.duckdb.quality_repository import QualityRepository
from impodo.adapters.duckdb.staging_repository import StagingRepository
from impodo.projects import DataClassification
from impodo.quality import default_quality_ruleset, evaluate_quality
from impodo.workspace_contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.workspace_errors import WorkspaceError
from impodo.web.app import create_local_app

from tests.test_quality import (
    MAPPING_HASH,
    PHYSICAL_HASH,
    SCHEMA_HASH,
    SOURCE_HASH,
    _canonical_row,
    _prepared,
    _project,
    _staging,
)


ROOT = Path(__file__).resolve().parents[1]


def _mapping() -> DatasetMapping:
    return DatasetMapping(
        dataset_id="dataset:contacts",
        target_model="res.partner",
        fields=(ScalarFieldMapping(target_field="name"),),
    )


def _quality(project, staging, rows):
    ruleset = default_quality_ruleset(
        project_id=project.project_id,
        mapping_hash=MAPPING_HASH,
        schema_hash=SCHEMA_HASH,
        datasets=("contacts",),
    )
    return ruleset, evaluate_quality(
        project=project,
        staging=staging,
        physical_rows={"dataset:contacts": tuple(item.source_row for item in rows)},
        ruleset=ruleset,
    )


class NormalizationEvaluationTests(unittest.TestCase):
    def test_no_change_still_requires_final_approval_and_unknown_policy_blocks(self) -> None:
        project = _project()
        rows = (_canonical_row("5", 2),)
        staging = _staging(project.project_id, rows)
        _, quality = _quality(project, staging, rows)
        evaluation = evaluate_normalization(
            project=project,
            staging=staging,
            quality=quality,
            mappings={"contacts": _mapping()},
            candidates=(),
        )
        dry_run = start_dry_run(
            evaluation,
            run_id=str(uuid4()),
            source_hashes={"contacts.csv": SOURCE_HASH},
        )

        self.assertEqual(dry_run.status, DryRunStatus.REVIEW_REQUIRED)
        frozen = dry_run.approve(
            actor=LOCAL_ACTOR,
            approved_at=datetime.now(timezone.utc),
        ).freeze(canonical_dataset_hash=evaluation.eligible_dataset_hash)
        self.assertEqual(frozen.status, DryRunStatus.FROZEN)

        with self.assertRaisesRegex(ValueError, "no safe review policy"):
            evaluate_normalization(
                project=project,
                staging=staging,
                quality=quality,
                mappings={"contacts": _mapping()},
                candidates=(
                    NormalizationCandidate(
                        dataset="contacts",
                        source_row=2,
                        source_label="Name",
                        target_field="name",
                        raw_display="A",
                        proposed_display="B",
                        rules="Unknown custom operation",
                        outcome="changed",
                    ),
                ),
            )

    def test_review_evidence_is_deterministic_and_masks_restricted_values(self) -> None:
        project = replace(
            _project(),
            data_classification=DataClassification.RESTRICTED,
        )
        rows = (
            _canonical_row(
                "5",
                2,
                source_identity=(" A ",),
                target_identity=("A",),
            ),
            _canonical_row(
                "6",
                3,
                source_identity=("B",),
                target_identity=("B",),
            ),
        )
        staging = _staging(project.project_id, rows)
        _, quality = _quality(project, staging, rows)
        candidates = (
            NormalizationCandidate(
                dataset="contacts",
                source_row=2,
                source_label="Name",
                target_field="name",
                raw_display=" A ",
                proposed_display="A",
                rules="Source + Trim",
                outcome="changed",
            ),
            NormalizationCandidate(
                dataset="contacts",
                source_row=3,
                source_label="Name",
                target_field="name",
                raw_display="B",
                proposed_display="Customer",
                rules="Constant",
                outcome="provided",
            ),
        )

        first = evaluate_normalization(
            project=project,
            staging=staging,
            quality=quality,
            mappings={"contacts": _mapping()},
            candidates=candidates,
        )
        repeated = evaluate_normalization(
            project=project,
            staging=staging,
            quality=quality,
            mappings={"contacts": _mapping()},
            candidates=reversed(candidates),
        )

        self.assertEqual(first.to_json(), repeated.to_json())
        self.assertEqual(first.changed_record_count, 2)
        self.assertEqual(first.pending_group_count, 1)
        self.assertEqual(
            {item.outcome for item in first.groups},
            {
                NormalizationOutcome.AUTOMATIC,
                NormalizationOutcome.DECISION_REQUIRED,
            },
        )
        self.assertEqual(
            {item.before for item in first.effects},
            {"Hidden for restricted data"},
        )
        self.assertEqual(
            NormalizationEvaluation.from_json(first.to_json()),
            first,
        )

    def test_governance_round_trip_freezes_only_after_required_decision(self) -> None:
        project = _project()
        rows = (_canonical_row("5", 2),)
        staging = _staging(project.project_id, rows)
        _, quality = _quality(project, staging, rows)
        evaluation = evaluate_normalization(
            project=project,
            staging=staging,
            quality=quality,
            mappings={"contacts": _mapping()},
            candidates=(
                NormalizationCandidate(
                    dataset="contacts",
                    source_row=2,
                    source_label="Name",
                    target_field="name",
                    raw_display="A",
                    proposed_display="Customer",
                    rules="Constant",
                    outcome="provided",
                ),
            ),
        )
        dry_run = start_dry_run(
            evaluation,
            run_id=str(uuid4()),
            source_hashes={"contacts.csv": SOURCE_HASH},
        )
        with self.assertRaisesRegex(ValueError, "still require approval"):
            dry_run.approve(
                actor=LOCAL_ACTOR,
                approved_at=datetime.now(timezone.utc),
            )
        reviewed = dry_run.approve_group(
            evaluation.groups[0].decision_key,
            actor=LOCAL_ACTOR,
            decided_at=datetime.now(timezone.utc),
        )
        frozen = reviewed.approve(
            actor=LOCAL_ACTOR,
            approved_at=datetime.now(timezone.utc),
        ).freeze(canonical_dataset_hash=evaluation.eligible_dataset_hash)

        self.assertEqual(frozen.status, DryRunStatus.FROZEN)
        self.assertEqual(DryRun.from_json(frozen.to_json()), frozen)


class NormalizationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        (ROOT / ".tmp").mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / ".tmp")
        database = DuckDbDatabase(self.temporary.name)
        self.projects = ProjectRepository(database)
        self.staging = StagingRepository(database)
        self.quality = QualityRepository(database, self.projects)
        self.repository = NormalizationRepository(database, self.projects)
        self.project = _project()
        self.projects.create(self.project, actor=LOCAL_ACTOR)
        now = datetime.now(timezone.utc)
        selection = SourceSelection(
            selection_id=str(uuid4()),
            version=1,
            project_id=self.project.project_id,
            created_at=now,
            created_by=LOCAL_ACTOR.identity.display_name,
            datasets=(
                SourceDataset(
                    dataset_id="dataset:contacts",
                    name="contacts",
                    file_id=str(uuid4()),
                    table_key="csv",
                    source_sha256=SOURCE_HASH,
                    catalog_hash="sha256:" + "a" * 64,
                    encoding="utf-8",
                    delimiter=",",
                    header_row=1,
                    row_count=1,
                    columns=(
                        SourceDatasetColumn(
                            1,
                            "Reference",
                            "column:reference",
                            "string",
                        ),
                    ),
                ),
            ),
            content_hash=PHYSICAL_HASH,
        )
        database_path = (
            self.repository.project_directory(self.project.project_id)
            / "project.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            connection.execute(
                "INSERT INTO source_selection VALUES (1, ?)",
                [selection.to_json()],
            )
            connection.execute(
                "INSERT INTO mapping_revision VALUES ('mapping:contacts', 1, NULL, ?, ?, ?, ?, '{}')",
                [MAPPING_HASH, PHYSICAL_HASH, SCHEMA_HASH, now.isoformat()],
            )
            connection.execute(
                "INSERT INTO mapping_current VALUES (1, 'mapping:contacts', 1)"
            )
            connection.execute(
                "INSERT INTO mapping_submission VALUES (?, 'mapping:contacts', 1, ?, ?, ?, '{}')",
                [
                    str(uuid4()),
                    MAPPING_HASH,
                    "sha256:" + "b" * 64,
                    now.isoformat(),
                ],
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_schema_15_project_adds_empty_prepared_review_boundary(self) -> None:
        database_path = (
            self.repository.project_directory(self.project.project_id)
            / "project.duckdb"
        )
        tables = (
            "normalization_current",
            "normalization_transition",
            "normalization_group",
            "normalization_effect",
            "normalization_run",
        )
        with self.repository._connect(database_path) as connection:
            for table in tables:
                connection.execute(f"DROP TABLE {table}")
            connection.execute("UPDATE schema_version SET version = 15")

        self.assertIsNone(
            self.repository.get_current_normalization_summary(
                self.project.project_id
            )
        )
        with self.repository._connect(database_path) as connection:
            version = connection.execute(
                "SELECT version FROM schema_version"
            ).fetchone()
            restored = {
                str(item[0])
                for item in connection.execute("SHOW TABLES").fetchall()
                if str(item[0]).startswith("normalization_")
            }
            self.assertEqual(version, (SCHEMA_VERSION,))
        self.assertEqual(restored, set(tables))

    def test_invalid_dry_run_evidence_is_wrapped_at_repository_boundary(
        self,
    ) -> None:
        row = _canonical_row("5", 2)
        staging = _staging(self.project.project_id, (row,))
        _ruleset, quality = _quality(self.project, staging, (row,))
        evaluation = evaluate_normalization(
            project=self.project,
            staging=staging,
            quality=quality,
            mappings={"contacts": _mapping()},
            candidates=(),
        )

        with (
            patch(
                "impodo.adapters.duckdb.normalization_repository.start_dry_run",
                side_effect=ValueError("invalid source hash"),
            ),
            self.assertRaisesRegex(
                WorkspaceError,
                "Prepared review source evidence is invalid",
            ),
        ):
            self.repository.publish_normalization_run(
                self.project.project_id,
                evaluation,
                staging_run_id="staging-run",
                quality_run_id="quality-run",
                source_hashes={"source:file": SOURCE_HASH},
                actor=LOCAL_ACTOR,
            )

        database_path = (
            self.repository.project_directory(self.project.project_id)
            / "project.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM normalization_run"
            ).fetchone()
        self.assertEqual(count, (0,))

    def test_review_decisions_survive_refresh_and_frozen_publish_is_idempotent(self) -> None:
        row = _canonical_row("5", 2)
        rows = (row,)
        staging_run = _staging(self.project.project_id, rows)
        staging = self.staging.publish_canonical_staging(
            self.project.project_id,
            staging_run,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        ruleset, quality_run = _quality(self.project, staging_run, rows)
        self.quality.publish_quality_ruleset(
            self.project.project_id,
            ruleset,
            actor=LOCAL_ACTOR,
        )
        quality = self.quality.publish_quality_run(
            self.project.project_id,
            quality_run,
            staging_run_id=staging.run_id,
            actor=LOCAL_ACTOR,
        )
        evaluation = evaluate_normalization(
            project=self.project,
            staging=staging_run,
            quality=quality_run,
            mappings={"contacts": _mapping()},
            candidates=(
                NormalizationCandidate(
                    dataset="contacts",
                    source_row=2,
                    source_label="Name",
                    target_field="name",
                    raw_display="A",
                    proposed_display="Customer",
                    rules="Constant",
                    outcome="provided",
                ),
            ),
        )
        published = self.repository.publish_normalization_run(
            self.project.project_id,
            evaluation,
            staging_run_id=staging.run_id,
            quality_run_id=quality.run_id,
            source_hashes={"contacts.csv": SOURCE_HASH},
            actor=LOCAL_ACTOR,
        )
        app = create_local_app(
            self.temporary.name,
            launch_token="launch-secret",
            session_secret="session-secret",
        )
        with TestClient(app) as client:
            launched = client.get(
                "/launch?token=launch-secret",
                follow_redirects=False,
            )
            self.assertEqual(launched.status_code, 303)
            page = client.get(
                f"/projects/{self.project.project_id}/normalization"
            )
            self.assertEqual(page.status_code, 200)
            self.assertIn("Review what Impodo prepared", page.text)
            self.assertIn("Accept this change", page.text)
            self.assertIn("Nothing is sent to Odoo", page.text)
            self.assertIn("<summary>Support details</summary>", page.text)
        decided = self.repository.decide_normalization_group(
            self.project.project_id,
            published.run_id,
            evaluation.groups[0].group_id,
            approve=True,
            expected_version=published.lifecycle_version,
            actor=LOCAL_ACTOR,
        )
        frozen = self.repository.approve_and_freeze_normalization(
            self.project.project_id,
            published.run_id,
            expected_version=decided.lifecycle_version,
            actor=LOCAL_ACTOR,
        )
        repeated = self.repository.publish_normalization_run(
            self.project.project_id,
            evaluation,
            staging_run_id=staging.run_id,
            quality_run_id=quality.run_id,
            source_hashes={"contacts.csv": SOURCE_HASH},
            actor=LOCAL_ACTOR,
        )

        self.assertTrue(frozen.frozen)
        self.assertEqual(repeated.run_id, frozen.run_id)
        self.assertEqual(repeated.lifecycle_version, frozen.lifecycle_version)
        self.assertEqual(
            self.repository.get_normalization_evaluation(
                self.project.project_id,
                frozen.run_id,
            ),
            evaluation,
        )
        self.assertEqual(
            self.repository.get_normalization_dry_run(
                self.project.project_id,
                frozen.run_id,
            ).status,
            DryRunStatus.FROZEN,
        )
        current_project = self.projects.get(self.project.project_id)
        changed_project = replace(
            current_project,
            data_manager="New Data Manager",
            revision=current_project.revision + 1,
            updated_at=datetime.now(timezone.utc),
        )
        self.projects.save(
            changed_project,
            expected_revision=current_project.revision,
            event_type="PROJECT_GOVERNANCE_UPDATED",
            event_detail="",
            actor=LOCAL_ACTOR,
        )
        self.assertIsNone(
            self.repository.get_current_normalization_summary(
                self.project.project_id
            )
        )
        database_path = (
            self.repository.project_directory(self.project.project_id)
            / "project.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            retired = connection.execute(
                "SELECT status, retired_reason FROM normalization_run WHERE run_id = ?",
                [frozen.run_id],
            ).fetchone()
        self.assertEqual(
            retired,
            ("INVALIDATED", "PROJECT_GOVERNANCE_CHANGED"),
        )

    @unittest.skipUnless(
        os.environ.get("IMPODO_RUN_NORMALIZATION_SCALE") == "1",
        "25,000-row normalization scale probe is opt-in",
    )
    def test_25k_effect_capture_and_persistence_probe(self) -> None:
        import psutil

        started = perf_counter()
        base = _canonical_row("5", 2)
        rows = tuple(
            replace(
                base,
                row_id="sha256:" + sha256(f"row:{index}".encode()).hexdigest(),
                source_row=index,
                source_identity=(f" C{index:05d} ",),
                target_identity=(f"C{index:05d}",),
                proposed_values={"name": f"C{index:05d}"},
                lineage=replace(
                    base.lineage,
                    source_row=index,
                    physical_source_rows=(index,),
                ),
            )
            for index in range(2, 25_002)
        )
        staging_run = _staging(self.project.project_id, rows)
        staging = self.staging.publish_canonical_staging(
            self.project.project_id,
            staging_run,
            mapping_version=1,
            actor=LOCAL_ACTOR,
        )
        ruleset, quality_run = _quality(self.project, staging_run, rows)
        self.quality.publish_quality_ruleset(
            self.project.project_id,
            ruleset,
            actor=LOCAL_ACTOR,
        )
        quality = self.quality.publish_quality_run(
            self.project.project_id,
            quality_run,
            staging_run_id=staging.run_id,
            actor=LOCAL_ACTOR,
        )
        evaluation = evaluate_normalization(
            project=self.project,
            staging=staging_run,
            quality=quality_run,
            mappings={"contacts": _mapping()},
            candidates=(
                NormalizationCandidate(
                    dataset="contacts",
                    source_row=index,
                    source_label="Name",
                    target_field="name",
                    raw_display=f" C{index:05d} ",
                    proposed_display=f"C{index:05d}",
                    rules="Source + Trim",
                    outcome="changed",
                )
                for index in range(2, 25_002)
            ),
        )
        published = self.repository.publish_normalization_run(
            self.project.project_id,
            evaluation,
            staging_run_id=staging.run_id,
            quality_run_id=quality.run_id,
            source_hashes={"contacts.csv": SOURCE_HASH},
            actor=LOCAL_ACTOR,
        )
        elapsed = perf_counter() - started
        database_path = (
            self.repository.project_directory(self.project.project_id)
            / "project.duckdb"
        )
        peak_mib = psutil.Process().memory_info().peak_wset / (1024 * 1024)
        database_mib = database_path.stat().st_size / (1024 * 1024)

        self.assertEqual(published.eligible_record_count, 25_000)
        self.assertEqual(published.changed_record_count, 25_000)
        self.assertLessEqual(elapsed, 60)
        self.assertLess(peak_mib, 512)
        self.assertLess(database_mib, 128)
        print(
            "Slice 4 scale probe: "
            f"{elapsed:.3f}s, {peak_mib:.1f} MiB peak working set, "
            f"{database_mib:.1f} MiB DuckDB"
        )


if __name__ == "__main__":
    unittest.main()
