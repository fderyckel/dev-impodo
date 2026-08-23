from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
from time import perf_counter
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4


from impodo.access import LOCAL_ACTOR
from impodo.application.bounded_normalization import (
    BoundedNormalizationUnsupported,
    _BoundedNormalizationEffects,
)
from impodo.application.bounded_quality import build_bounded_quality_run
from impodo.application.normalization_service import NormalizationService
from impodo.domain.errors import NormalizationReviewPolicyError, ReadinessError
from impodo.governance import DryRun, DryRunStatus
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    ReferenceLookupMapping,
    ScalarFieldMapping,
    ScalarValueSource,
    ValueMapping,
)
from impodo.domain.staging.transformation_impact import TransformationImpactRow
from impodo.normalization import (
    NormalizationCandidate,
    NormalizationEvaluation,
    NormalizationOutcome,
    NormalizationPolicyAction,
    compile_normalization_review_policy,
    evaluate_normalization,
    start_dry_run,
)
from impodo.adapters.duckdb.database import DuckDbWorkspaceDatabase
from impodo.adapters.duckdb.normalization_repository import NormalizationRepository
from impodo.adapters.duckdb.workspace_state_repository import WorkspaceStateRepository
from impodo.adapters.duckdb.quality_repository import QualityRepository
from impodo.adapters.duckdb.staging_repository import StagingRepository
from impodo.workspace_state import DataClassification
from impodo.domain.source_binding import FileSourceBinding
from impodo.quality import default_quality_ruleset, evaluate_quality
from impodo.workspace_contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.workspace_errors import WorkspaceError
from impodo.value_rules import ScalarTransformPolicy, TextTransformStep

from tests.test_quality import (
    MAPPING_HASH,
    PHYSICAL_HASH,
    SCHEMA_HASH,
    SOURCE_HASH,
    _canonical_row,
    _project,
    _staging,
    _stored_staging,
)


ROOT = Path(__file__).resolve().parents[1]


def _mapping(*fields: ScalarFieldMapping) -> DatasetMapping:
    return DatasetMapping(
        dataset_id="dataset:contacts",
        target_model="res.partner",
        fields=fields or (ScalarFieldMapping(target_field="name"),),
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
    def test_high_volume_bounded_rejection_never_materializes(self) -> None:
        project = _project()
        rows = (_canonical_row("5", 2),)
        staging = _staging(project.project_id, rows)
        stored_staging = _stored_staging(staging)
        ruleset = default_quality_ruleset(
            project_id=project.project_id,
            mapping_hash=MAPPING_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
        )
        materialized_quality = evaluate_quality(
            project=project,
            staging=staging,
            physical_rows={"dataset:contacts": (2,)},
            ruleset=ruleset,
        )
        stored_quality = build_bounded_quality_run(
            project=project,
            staging=stored_staging,
            physical_rows={"dataset:contacts": (2,)},
            ruleset=ruleset,
            published_staging_content_hash=staging.content_hash,
        ).with_content_hash(materialized_quality.content_hash)
        repository = MagicMock()
        service = NormalizationService(repository, MagicMock())
        mapping = _mapping()
        revision = SimpleNamespace(definition=SimpleNamespace(datasets=(mapping,)))
        selection = SimpleNamespace(
            datasets=(
                SimpleNamespace(
                    dataset_id=mapping.dataset_id,
                    name="contacts",
                ),
            )
        )

        with (
            patch(
                "impodo.application.normalization_service."
                "build_bounded_normalization_evaluation",
                side_effect=BoundedNormalizationUnsupported,
            ),
            patch(
                "impodo.application.normalization_service.evaluate_normalization",
                side_effect=AssertionError("whole-run fallback executed"),
            ),
            self.assertRaisesRegex(
                ReadinessError,
                "Whole-run fallback is disabled",
            ),
        ):
            service.evaluate_and_publish(
                project,
                revision,
                selection,
                stored_staging,
                SimpleNamespace(
                    content_hash=staging.content_hash,
                    run_id="staging:1",
                ),
                stored_quality,
                SimpleNamespace(
                    content_hash=materialized_quality.content_hash,
                    run_id="quality:1",
                ),
                (),
                {"file:1": SOURCE_HASH},
                actor=LOCAL_ACTOR,
                allow_materialized_fallback=False,
            )

        repository.publish_normalization_run.assert_not_called()

    def test_no_change_still_requires_final_approval_and_unknown_policy_blocks(
        self,
    ) -> None:
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

        with self.assertRaisesRegex(
            ValueError,
            "does not define a supported review policy",
        ):
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

    def test_ordered_text_changes_use_structural_policy_in_both_paths(
        self,
    ) -> None:
        project = _project()
        row = _canonical_row("5", 2)
        rows = (row,)
        staging = _staging(project.project_id, rows)
        _, quality = _quality(project, staging, rows)
        mapping = _mapping(
            ScalarFieldMapping(
                target_field="phone",
                source_column_key="column:phone",
                transform=ScalarTransformPolicy(
                    text_steps=(
                        TextTransformStep(
                            search_value="00",
                            replacement_value="+",
                            search_mode="starts_with",
                            replace_all=False,
                        ),
                        TextTransformStep(
                            kind="remove_separators_between_digits",
                            characters=" .-/",
                        ),
                    )
                ),
            )
        )

        def candidate(rules: str) -> NormalizationCandidate:
            return NormalizationCandidate(
                dataset="contacts",
                source_row=2,
                source_label="Phone",
                target_field="phone",
                raw_display="00352 12-34",
                proposed_display="+3521234",
                rules=rules,
                outcome="changed",
            )

        materialized = evaluate_normalization(
            project=project,
            staging=staging,
            quality=quality,
            mappings={"contacts": mapping},
            candidates=(candidate("Source + 2 ordered text changes"),),
        )
        renamed_display = evaluate_normalization(
            project=project,
            staging=staging,
            quality=quality,
            mappings={"contacts": mapping},
            candidates=(candidate("Localized display wording"),),
        )
        factory = _BoundedNormalizationEffects(
            project=project,
            mapping_hash=staging.mapping_hash,
            mappings={"contacts": mapping},
            eligible_row_ids=quality.eligible_row_ids,
        )
        bounded = factory._effect(
            TransformationImpactRow(
                dataset="contacts",
                source_row=2,
                source_column="Phone",
                target_field="phone",
                raw_value="00352 12-34",
                proposed_value="+3521234",
                rules="Any display wording",
                outcome="changed",
            ),
            row.row_id,
        )

        self.assertEqual(materialized.to_json(), renamed_display.to_json())
        self.assertEqual(materialized.pending_group_count, 1)
        self.assertEqual(
            materialized.groups[0].outcome,
            NormalizationOutcome.DECISION_REQUIRED,
        )
        self.assertEqual(
            materialized.groups[0].name,
            "Apply ordered text changes",
        )
        self.assertIsNotNone(bounded)
        assert bounded is not None
        effect, metadata = bounded
        self.assertEqual(effect.group_id, materialized.groups[0].group_id)
        self.assertEqual(metadata["rule_id"], materialized.groups[0].rule_id)
        self.assertEqual(metadata["outcome"], materialized.groups[0].outcome)
        self.assertEqual(metadata["name"], materialized.groups[0].name)

    def test_unsupported_structural_policy_keeps_specific_recovery_code(
        self,
    ) -> None:
        project = _project()
        row = _canonical_row("5", 2)
        staging = _staging(project.project_id, (row,))
        _, quality = _quality(project, staging, (row,))
        mapping = _mapping()
        service = NormalizationService(MagicMock(), MagicMock())
        revision = SimpleNamespace(definition=SimpleNamespace(datasets=(mapping,)))
        selection = SimpleNamespace(
            datasets=(
                SimpleNamespace(
                    dataset_id=mapping.dataset_id,
                    name="contacts",
                ),
            )
        )

        with self.assertRaises(NormalizationReviewPolicyError) as raised:
            service.evaluate_and_publish(
                project,
                revision,
                selection,
                staging,
                SimpleNamespace(
                    content_hash=staging.content_hash,
                    run_id="staging:1",
                ),
                quality,
                SimpleNamespace(
                    content_hash=quality.content_hash,
                    run_id="quality:1",
                ),
                (
                    TransformationImpactRow(
                        dataset="contacts",
                        source_row=2,
                        source_column="Name",
                        target_field="name",
                        raw_value="A",
                        proposed_value="B",
                        rules="Unknown custom operation",
                        outcome="changed",
                    ),
                ),
                {"contacts.csv": SOURCE_HASH},
                actor=LOCAL_ACTOR,
            )

        self.assertEqual(
            raised.exception.failure_code,
            "NORMALIZATION_REVIEW_POLICY_UNSUPPORTED",
        )
        self.assertIn("No source file or Odoo record was changed", str(raised.exception))
        service.repository.publish_normalization_run.assert_not_called()

    def test_structural_policy_covers_supported_scalar_change_kinds(self) -> None:
        mapping = _mapping(
            ScalarFieldMapping(
                target_field="trimmed",
                transform=ScalarTransformPolicy(trim=True),
            ),
            ScalarFieldMapping(target_field="parsed", value_type="integer"),
            ScalarFieldMapping(
                target_field="constant",
                value_source=ScalarValueSource.CONSTANT,
                literal_value="value",
            ),
            ScalarFieldMapping(
                target_field="fallback",
                value_source=ScalarValueSource.SOURCE_WITH_FALLBACK,
                literal_value="value",
            ),
            ScalarFieldMapping(
                target_field="matched",
                value_mappings=(ValueMapping("A", "B"),),
            ),
            ScalarFieldMapping(
                target_field="reference",
                source_column_key="column:reference",
                reference_lookup=ReferenceLookupMapping(
                    reference_id=str(uuid4()),
                    reference_content_hash=SOURCE_HASH,
                    key_source_column_keys=("column:reference",),
                    value_field="name",
                ),
            ),
            ScalarFieldMapping(
                target_field="formula",
                transform=ScalarTransformPolicy(formula="source"),
            ),
            ScalarFieldMapping(
                target_field="text",
                transform=ScalarTransformPolicy(
                    text_steps=(TextTransformStep(search_value="A"),)
                ),
            ),
            ScalarFieldMapping(
                target_field="case",
                transform=ScalarTransformPolicy(case_mode="uppercase"),
            ),
            ScalarFieldMapping(
                target_field="blank",
                transform=ScalarTransformPolicy(empty_as_null=True),
            ),
            ScalarFieldMapping(
                target_field="rounded",
                value_type="decimal",
                transform=ScalarTransformPolicy(decimal_places=2),
            ),
        )

        policies = compile_normalization_review_policy(
            {"contacts": mapping}
        ).fields
        expected = {
            "trimmed": (
                NormalizationOutcome.AUTOMATIC,
                NormalizationPolicyAction.WHITESPACE,
            ),
            "parsed": (
                NormalizationOutcome.AUTOMATIC,
                NormalizationPolicyAction.PARSE,
            ),
            "constant": (
                NormalizationOutcome.DECISION_REQUIRED,
                NormalizationPolicyAction.CONSTANT,
            ),
            "fallback": (
                NormalizationOutcome.DECISION_REQUIRED,
                NormalizationPolicyAction.FALLBACK,
            ),
            "matched": (
                NormalizationOutcome.DECISION_REQUIRED,
                NormalizationPolicyAction.VALUE_MATCH,
            ),
            "reference": (
                NormalizationOutcome.DECISION_REQUIRED,
                NormalizationPolicyAction.REFERENCE_LOOKUP,
            ),
            "formula": (
                NormalizationOutcome.DECISION_REQUIRED,
                NormalizationPolicyAction.FORMULA,
            ),
            "text": (
                NormalizationOutcome.DECISION_REQUIRED,
                NormalizationPolicyAction.TEXT_CHANGE,
            ),
            "case": (
                NormalizationOutcome.DECISION_REQUIRED,
                NormalizationPolicyAction.CASE,
            ),
            "blank": (
                NormalizationOutcome.DECISION_REQUIRED,
                NormalizationPolicyAction.EMPTY_TO_NULL,
            ),
            "rounded": (
                NormalizationOutcome.DECISION_REQUIRED,
                NormalizationPolicyAction.ROUNDING,
            ),
        }

        self.assertEqual(
            {
                target_field: (
                    policies[("contacts", target_field)].outcome,
                    policies[("contacts", target_field)].action,
                )
                for target_field in expected
            },
            expected,
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
        mapping = _mapping(
            ScalarFieldMapping(
                target_field="name",
                transform=ScalarTransformPolicy(trim=True),
            ),
            ScalarFieldMapping(
                target_field="company_type",
                value_source=ScalarValueSource.CONSTANT,
                literal_value="company",
            ),
        )
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
                source_label="Constant value",
                target_field="company_type",
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
            mappings={"contacts": mapping},
            candidates=candidates,
        )
        repeated = evaluate_normalization(
            project=project,
            staging=staging,
            quality=quality,
            mappings={"contacts": mapping},
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
        legacy = replace(first, evaluator_version=2)
        self.assertEqual(
            NormalizationEvaluation.from_json(legacy.to_json()),
            legacy,
        )

    def test_governance_round_trip_freezes_only_after_required_decision(self) -> None:
        project = _project()
        rows = (_canonical_row("5", 2),)
        staging = _staging(project.project_id, rows)
        _, quality = _quality(project, staging, rows)
        mapping = _mapping(
            ScalarFieldMapping(
                target_field="name",
                value_source=ScalarValueSource.CONSTANT,
                literal_value="Customer",
            )
        )
        evaluation = evaluate_normalization(
            project=project,
            staging=staging,
            quality=quality,
            mappings={"contacts": mapping},
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
        database = DuckDbWorkspaceDatabase(self.temporary.name)
        self.projects = WorkspaceStateRepository(database)
        self.staging = StagingRepository(database)
        self.quality = QualityRepository(database, self.projects)
        self.repository = NormalizationRepository(database, self.projects)
        self.project = _project()
        self.projects.create_unlinked(self.project, actor=LOCAL_ACTOR)
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
                    source=FileSourceBinding(
                        file_id=str(uuid4()),
                        table_key="csv",
                        source_sha256=SOURCE_HASH,
                        catalog_hash="sha256:" + "a" * 64,
                        encoding="utf-8",
                        delimiter=",",
                        header_row=1,
                    ),
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
            self.repository.workspace_directory(self.project.project_id)
            / "workspace-engine.duckdb"
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
            self.repository.workspace_directory(self.project.project_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM normalization_run"
            ).fetchone()
        self.assertEqual(count, (0,))

    def test_review_decisions_survive_refresh_and_frozen_publish_is_idempotent(
        self,
    ) -> None:
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
        mapping = _mapping(
            ScalarFieldMapping(
                target_field="name",
                value_source=ScalarValueSource.CONSTANT,
                literal_value="Customer",
            )
        )
        evaluation = evaluate_normalization(
            project=self.project,
            staging=staging_run,
            quality=quality_run,
            mappings={"contacts": mapping},
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
        blocked = self.repository.decide_normalization_group(
            self.project.project_id,
            published.run_id,
            evaluation.groups[0].group_id,
            approve=False,
            expected_version=published.lifecycle_version,
            actor=LOCAL_ACTOR,
            reason="The supplied value needs correction.",
        )
        self.assertEqual(blocked.status, DryRunStatus.BLOCKED.value)
        reopened = self.repository.reopen_normalization_review(
            self.project.project_id,
            published.run_id,
            expected_version=blocked.lifecycle_version,
            actor=LOCAL_ACTOR,
            reason="Reopened after review.",
        )
        frozen = self.repository.approve_and_freeze_normalization(
            self.project.project_id,
            published.run_id,
            expected_version=reopened.lifecycle_version,
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
        self.assertIn(
            evaluation.groups[0].decision_key,
            self.repository.get_normalization_dry_run(
                self.project.project_id,
                frozen.run_id,
            ).approved_groups,
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
            event_type="WORKSPACE_GOVERNANCE_UPDATED",
            event_detail="",
            actor=LOCAL_ACTOR,
        )
        self.assertIsNone(
            self.repository.get_current_normalization_summary(self.project.project_id)
        )
        database_path = (
            self.repository.workspace_directory(self.project.project_id)
            / "workspace-engine.duckdb"
        )
        with self.repository._connect(database_path) as connection:
            retired = connection.execute(
                "SELECT status, retired_reason FROM normalization_run WHERE run_id = ?",
                [frozen.run_id],
            ).fetchone()
        self.assertEqual(
            retired,
            ("INVALIDATED", "WORKSPACE_GOVERNANCE_CHANGED"),
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
        mapping = _mapping(
            ScalarFieldMapping(
                target_field="name",
                transform=ScalarTransformPolicy(trim=True),
            )
        )
        evaluation = evaluate_normalization(
            project=self.project,
            staging=staging_run,
            quality=quality_run,
            mappings={"contacts": mapping},
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
            self.repository.workspace_directory(self.project.project_id)
            / "workspace-engine.duckdb"
        )
        peak_mib = psutil.Process().memory_info().peak_wset / (1024 * 1024)
        database_mib = database_path.stat().st_size / (1024 * 1024)

        self.assertEqual(published.eligible_record_count, 25_000)
        self.assertEqual(published.changed_record_count, 25_000)
        print(
            "Slice 4 scale probe: "
            f"{elapsed:.3f}s, {peak_mib:.1f} MiB peak working set, "
            f"{database_mib:.1f} MiB DuckDB"
        )


if __name__ == "__main__":
    unittest.main()

