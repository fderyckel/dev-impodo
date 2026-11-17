"""Keep named quality facts bounded, immutable, and faithful to prepared rows."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError, replace

from impodo.application.workspace.preparation.bounded_quality import (
    BoundedQualityUnsupported,
    build_bounded_quality_run,
)
from impodo.application.workspace.preparation.prepared_quality_projection import (
    PreparedQualityProjection,
    PreparedQualityProjectionError,
)
from impodo.domain.preparation.quality import default_quality_ruleset, evaluate_quality
from impodo.domain.preparation.quality_budget import QualityFindingBudget
from impodo.domain.preparation.staging_contracts import (
    CanonicalIssue,
    StagingDatasetRole,
    StagingDisposition,
)
from impodo.domain.shared.models import canonical_json_bytes
from tests.domain.preparation import test_quality as fixtures


def _counts(candidate_rows=1):
    return {
        disposition: candidate_rows
        if disposition is StagingDisposition.CANDIDATE
        else 0
        for disposition in StagingDisposition
    }


def _projection(**changes):
    return PreparedQualityProjection(
        **{
            "row_count": 1,
            "physical_row_count": 1,
            "direct_accounting": True,
            "disposition_counts": _counts(),
            **changes,
        }
    )


class _PacketRows(fixtures._IndexedTestRows):
    """Read one multi-source output using the same explicit projection contract."""

    def bounded_quality_index(self, physical_rows, *, numeric_fields=()):
        return self.projection

    def iter_accounting_index_batches(self, _connection, batch_size):
        facts = tuple(
            sorted(
                (dataset_id, source_row, (row.row_id,))
                for row in self
                for dataset_id, source_rows in row.lineage.physical_sources.items()
                for source_row in source_rows
            )
        )
        for start in range(0, len(facts), batch_size):
            yield facts[start : start + batch_size]


class PreparedQualityProjectionTests(unittest.TestCase):
    def test_malformed_counts_cannot_become_quality_or_accounting_totals(self):
        for name in ("row_count", "physical_row_count"):
            for value in (-1, True, 1.0, "1"):
                with (
                    self.subTest(name=name, value=value),
                    self.assertRaises(PreparedQualityProjectionError),
                ):
                    _projection(**{name: value})
        for invalid_count in (-1, True, 1.0, "1"):
            with self.subTest(disposition_count=invalid_count):
                counts = _counts()
                counts[StagingDisposition.CANDIDATE] = invalid_count
                with self.assertRaises(PreparedQualityProjectionError):
                    _projection(disposition_counts=counts)

    def test_incomplete_unknown_or_inconsistent_dispositions_are_refused(self):
        missing = _counts()
        del missing[StagingDisposition.EXCLUDED]
        unknown = {**_counts(), "UNKNOWN": 0}
        string_keys = {key.value: count for key, count in _counts().items()}
        for counts in (missing, unknown, string_keys, _counts(2)):
            with (
                self.subTest(counts=counts),
                self.assertRaises(PreparedQualityProjectionError),
            ):
                _projection(disposition_counts=counts)

    def test_direct_accounting_requires_a_boolean_and_one_physical_row_per_output(self):
        for mode in (1, "direct", None):
            with (
                self.subTest(mode=mode),
                self.assertRaises(PreparedQualityProjectionError),
            ):
                _projection(direct_accounting=mode)
        with self.assertRaises(PreparedQualityProjectionError):
            _projection(physical_row_count=2)

        multiple_sources = _projection(physical_row_count=2, direct_accounting=False)
        self.assertEqual(multiple_sources.physical_row_count, 2)
        self.assertFalse(multiple_sources.direct_accounting)

    def test_malformed_finding_pages_fail_before_quality_consumption(self):
        for name, width in (
            ("issue_rows", 8),
            ("collisions", 8),
            ("exception_rows", 7),
            ("precision_rows", 14),
        ):
            for facts in (
                object(),
                "not a fact page",
                ("x" * width,),
                ((None,) * (width - 1),),
                ((None,) * (width + 1),),
            ):
                with (
                    self.subTest(name=name, facts=facts),
                    self.assertRaises(PreparedQualityProjectionError),
                ):
                    _projection(**{name: facts})

    def test_membership_and_budget_are_validated_without_resetting_shared_charges(self):
        for membership in (("row-id",), {7}, "row-id"):
            with (
                self.subTest(membership=membership),
                self.assertRaises(PreparedQualityProjectionError),
            ):
                _projection(excluded_row_ids=membership)
        with self.assertRaises(PreparedQualityProjectionError):
            _projection(finding_budget=object())

        budget = QualityFindingBudget(limit_bytes=1024, used_bytes=256)
        packet = _projection(finding_budget=budget)
        self.assertIs(packet.finding_budget, budget)
        packet.finding_budget.consume(128)
        self.assertEqual(budget.used_bytes, 384)

        fresh = _projection()
        another = _projection()
        fresh.finding_budget.consume(128)
        self.assertEqual(another.finding_budget.used_bytes, 0)
        self.assertIsNot(fresh.finding_budget, another.finding_budget)

    def test_captured_counts_and_membership_do_not_change_with_caller_collections(self):
        counts = _counts()
        excluded = {"first"}
        packet = _projection(disposition_counts=counts, excluded_row_ids=excluded)
        counts[StagingDisposition.CANDIDATE] = 0
        excluded.add("later")

        self.assertEqual(packet.disposition_counts[StagingDisposition.CANDIDATE], 1)
        self.assertEqual(packet.excluded_row_ids, frozenset({"first"}))
        with self.assertRaises(TypeError):
            packet.disposition_counts[StagingDisposition.CANDIDATE] = 0
        with self.assertRaises(FrozenInstanceError):
            packet.row_count = 2

    def test_fact_payloads_keep_their_bytes_and_order_for_the_existing_decoder(self):
        # JSON meaning is checked by the quality consumer, after packet shape.
        payload = '{"message":"Réviser A",'
        rows = (
            (0, "first", "joined", 2, "CANDIDATE", payload, "left", 2),
            (0, "first", "joined", 2, "CANDIDATE", payload, "right", 7),
        )

        packet = _projection(issue_rows=rows)

        self.assertIs(packet.issue_rows, rows)
        self.assertEqual(packet.issue_rows[0][5], payload)
        self.assertEqual(packet.issue_rows[1][6:], ("right", 7))

    def test_unnamed_index_results_are_refused_instead_of_partially_consumed(self):
        workspace = fixtures._workspace_state()
        row = fixtures._canonical_row("a", 2)
        staging = fixtures._staging(workspace.workspace_id, (row,))
        reader = _PacketRows((row,))
        reader.projection = {"row_count": 1, "disposition_counts": _counts()}
        ruleset = default_quality_ruleset(
            workspace_id=workspace.workspace_id,
            mapping_hash=fixtures.MAPPING_HASH,
            schema_hash=fixtures.SCHEMA_HASH,
            datasets=("contacts",),
        )

        with self.assertRaises(BoundedQualityUnsupported):
            build_bounded_quality_run(
                staging=replace(fixtures._stored_staging(staging), rows=reader),
                workspace_state=workspace,
                physical_rows={"dataset:contacts": (2,)},
                ruleset=ruleset,
                published_staging_content_hash=staging.content_hash,
            )

    def test_multi_source_issue_payload_and_quality_hash_match_the_full_evaluator(self):
        workspace = fixtures._workspace_state()
        issue = CanonicalIssue(
            code="SOURCE_REQUIRED_VALUE_MISSING",
            message='Réviser le champ "Name".',
            severity="error",
            dataset="contacts",
            source_row=2,
            field="name",
        )
        row = fixtures._canonical_row("a", 2)
        physical_rows = {"dataset:contacts": (2,), "dataset:details": (7, 8)}
        row = replace(
            row,
            disposition=StagingDisposition.BLOCKED,
            issues=(issue,),
            lineage=replace(row.lineage, physical_sources=physical_rows),
        )
        staging = fixtures._staging(workspace.workspace_id, (row,))
        staging = replace(
            staging,
            datasets=(
                replace(
                    staging.datasets[0],
                    role=StagingDatasetRole.JOIN,
                    input_rows=3,
                    input_rows_used=3,
                    lineage_links=3,
                    combined_rows=2,
                ),
            ),
        )
        payload = canonical_json_bytes(issue.to_portable_dict()).decode("utf-8")
        facts = tuple(
            (
                0,
                row.row_id,
                row.dataset,
                row.source_row,
                row.disposition.value,
                payload,
                dataset_id,
                source_row,
            )
            for dataset_id, source_rows in physical_rows.items()
            for source_row in source_rows
        )
        budget = QualityFindingBudget(used_bytes=256)
        packet = _projection(
            physical_row_count=3,
            direct_accounting=False,
            disposition_counts={**_counts(0), StagingDisposition.BLOCKED: 1},
            issue_rows=facts,
            finding_budget=budget,
        )
        reader = _PacketRows((row,))
        reader.projection = packet
        stored = replace(fixtures._stored_staging(staging), rows=reader)
        arguments = {
            "workspace_state": workspace,
            "physical_rows": physical_rows,
            "ruleset": default_quality_ruleset(
                workspace_id=workspace.workspace_id,
                mapping_hash=fixtures.MAPPING_HASH,
                schema_hash=fixtures.SCHEMA_HASH,
                datasets=("contacts",),
            ),
            "published_staging_content_hash": staging.content_hash,
        }

        expected = evaluate_quality(staging=staging, **arguments)
        actual = fixtures._materialized_bounded_quality(
            build_bounded_quality_run(staging=stored, **arguments)
        )

        self.assertEqual(actual.to_json(), expected.to_json())
        self.assertEqual(actual.content_hash, expected.content_hash)
        self.assertEqual(len(actual.source_accounting), 3)
        self.assertEqual(len(actual.issues), 1)
        self.assertEqual(len(actual.quarantine[0].physical_sources), 3)
        self.assertEqual(packet.issue_rows, facts)
        self.assertIs(packet.finding_budget, budget)
        self.assertGreater(budget.used_bytes, 256)
