"""Stored preparation checks every duplicate member with bounded scratch counts."""

from dataclasses import replace
import unittest

from impodo.adapters.duckdb.preparation_quality_spill import quality_finding_store
from impodo.adapters.duckdb.request_timing import collect_duckdb_request_timings
from impodo.adapters.duckdb.unit_of_work import retain_databases_for_read
from impodo.application.workspace.preparation.bounded_quality import (
    build_bounded_quality_run,
    materialize_staging_run,
)
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ScalarFieldMapping,
)
from impodo.domain.preparation.field_uniqueness import mapped_unique_fields
from impodo.domain.preparation.quality import default_quality_ruleset, evaluate_quality
from impodo.domain.preparation.staging_contracts import StagingDatasetRole
from impodo.domain.shared.access import LOCAL_ACTOR
from impodo.domain.staging.canonical_projection import canonical_prepared_session_row
from impodo.domain.staging.transformation_impact import TransformationImpactReport
from tests.domain.preparation.test_quality import _materialized_bounded_quality
from tests.integration.duckdb.test_preparation_session import (
    SCHEMA_HASH,
    SELECTION_HASH,
    SOURCE_HASH,
)
from tests.integration.duckdb import test_preparation_session as fixtures
from tests.support.database_probe import count_duckdb_statements


class StoredFieldUniquenessTests(unittest.TestCase):
    setUp = fixtures.PreparationSessionRepositoryTests.setUp
    tearDown = fixtures.PreparationSessionRepositoryTests.tearDown

    def test_multiple_storage_pages_retain_all_findings_and_match_the_domain_evaluator(
        self,
    ):
        definition = MappingDefinition(
            mapping_id=self.bindings.mapping_id,
            source_selection_hash=SELECTION_HASH,
            schema_hash=SCHEMA_HASH,
            datasets=(
                DatasetMapping(
                    dataset_id="dataset:contacts",
                    target_model="res.partner",
                    source_identity_column_keys=("column:ref",),
                    target_identity=(
                        IdentityComponentMapping(("column:ref",), ("ref",)),
                    ),
                    fields=(
                        ScalarFieldMapping("vat", "column:vat", must_be_unique=True),
                    ),
                ),
            ),
        )
        bindings = replace(self.bindings, mapping_hash=definition.content_hash)
        session = self.repository.begin_direct_session(
            self.workspace_state.workspace_id, bindings, actor=LOCAL_ACTOR
        )
        row_count = 520
        for start in range(0, row_count, 100):
            self.repository.append_direct_rows(
                self.workspace_state.workspace_id,
                session.session_id,
                tuple(
                    canonical_prepared_session_row(
                        dataset="contacts",
                        source_row=index + 2,
                        target_model="res.partner",
                        source_identity=(f"C{index}",),
                        target_identity=(f"C{index}",),
                        target_scope=(),
                        scalar_values={"vat": f"VAT-{index % 260}"},
                        references={},
                        issues=(),
                        ordinal=index,
                        mode="upsert",
                        source_hash=SOURCE_HASH,
                        source_selection_hash=SELECTION_HASH,
                        mapping_hash=definition.content_hash,
                        schema_hash=SCHEMA_HASH,
                        field_sources={"vat": ("column:vat",)},
                        physical_dataset_id="dataset:contacts",
                    )
                    for index in range(start, min(start + 100, row_count))
                ),
            )
        stored = self.repository.finalize_direct_session(
            self.workspace_state.workspace_id,
            session.session_id,
            dataset_evidence={
                "contacts": (
                    "dataset:contacts",
                    StagingDatasetRole.DIRECT,
                    row_count,
                    "res.partner",
                )
            },
            run_issues=(),
            control_totals=(),
            impact_report=TransformationImpactReport(
                mapping_content_hash=definition.content_hash,
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
        physical = {"dataset:contacts": tuple(range(2, row_count + 2))}
        ruleset = default_quality_ruleset(
            workspace_id=self.workspace_state.workspace_id,
            mapping_hash=definition.content_hash,
            schema_hash=SCHEMA_HASH,
            datasets=("contacts",),
        )
        expected = evaluate_quality(
            workspace_state=self.workspace_state,
            staging=materialize_staging_run(stored),
            physical_rows=physical,
            ruleset=ruleset,
            published_staging_content_hash=stored.validated_content_hash,
            unique_fields=mapped_unique_fields(definition),
        )
        with quality_finding_store() as scratch:
            with (
                collect_duckdb_request_timings() as timing,
                count_duckdb_statements() as statements,
                retain_databases_for_read(),
            ):
                actual = build_bounded_quality_run(
                    workspace_state=self.workspace_state,
                    staging=stored,
                    physical_rows=physical,
                    ruleset=ruleset,
                    definition=definition,
                    published_staging_content_hash=stored.validated_content_hash,
                    finding_store=scratch,
                )
                actual_json = _materialized_bounded_quality(actual).to_json()
            self.assertEqual(actual.summary_counts["blocked_count"], row_count)
            self.assertEqual(len(actual.issues), row_count)
            self.assertEqual(
                {issue.affected_fields for issue in actual.issues}, {("vat",)}
            )
            self.assertEqual(actual_json, expected.to_json())
            # One session owner for normal JSON reads and one indexed owner;
            # owner count must not grow with the number of duplicate members.
            self.assertLessEqual(timing.connection_count, 2)
            self.assertLessEqual(timing.schema_check_count, 2)
            self.assertLess(len(statements), 180)
