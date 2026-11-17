"""Preserve bounded quality routes when prepared readers expose separate facts."""

from __future__ import annotations

import unittest
from collections.abc import Sequence
from dataclasses import replace

from impodo.application.workspace.preparation.bounded_quality import (
    BoundedQualityUnsupported,
    build_bounded_quality_run,
)
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
)
from impodo.domain.preparation.quality import default_quality_ruleset, evaluate_quality
from impodo.domain.shared.models import LogicalReference, canonical_json_bytes
from tests.domain.preparation import test_quality as fixtures


class _CanonicalRows(Sequence):
    """Allow canonical reads only until the fixture explicitly forbids them."""

    def __init__(self, rows):
        self.rows = tuple(rows)
        self.forbid_canonical_reads = False
        self.canonical_reads = 0

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, index):
        self._canonical_read()
        return self.rows[index]

    def __iter__(self):
        self._canonical_read()
        return iter(self.rows)

    def _canonical_read(self):
        if self.forbid_canonical_reads:
            raise AssertionError("The indexed reader must not restore canonical rows")
        self.canonical_reads += 1


class _IndexOnlyRows(_CanonicalRows):
    """Expose quality and accounting facts without relationship or JSON readers."""

    def __init__(self, rows):
        super().__init__(rows)
        self.index_calls = 0
        self.index = fixtures._IndexedTestRows(self.rows)

    def bounded_quality_index(self, physical_rows, *, numeric_fields=()):
        self.index_calls += 1
        return self.index.bounded_quality_index(
            physical_rows, numeric_fields=numeric_fields
        )

    def iter_quality_index_batches(self, connection, batch_size):
        yield from self.index.iter_quality_index_batches(connection, batch_size)

    def iter_accounting_index_batches(self, connection, batch_size):
        yield from self.index.iter_accounting_index_batches(connection, batch_size)

    def contains_row_id(self, row_id):
        return self.index.contains_row_id(row_id)


class _DecliningIndexRows(_IndexOnlyRows):
    """Decline an otherwise advertised index through its supported result."""

    def bounded_quality_index(self, physical_rows, *, numeric_fields=()):
        self.index_calls += 1


class _EncodedOnlyRows(_CanonicalRows):
    """Supply encoded publication pages independently of narrow quality facts."""

    def __init__(self, rows):
        super().__init__(rows)
        self.publication_connections = []

    def iter_encoded_batches(self, connection, batch_size):
        self.publication_connections.append(connection)
        for start in range(0, len(self.rows), batch_size):
            yield tuple(
                (
                    ordinal,
                    canonical_json_bytes(row.to_portable_dict()).decode("utf-8"),
                )
                for ordinal, row in enumerate(
                    self.rows[start : start + batch_size], start
                )
            )


class PreparedQualityReaderContractTests(unittest.TestCase):
    def _case(self, *, related=False, multiple_datasets=True, collision=False):
        workspace = fixtures._workspace_state()
        names = ("documents", "entries") if multiple_datasets else ("documents",)
        mappings = tuple(
            DatasetMapping(
                dataset_id=f"dataset:{name}",
                target_model=f"x_custom.{name}",
                source_identity_column_keys=("column:reference",),
                target_identity=(
                    IdentityComponentMapping(("column:reference",), ("name",)),
                ),
                target_scope=(
                    IdentityComponentMapping(
                        ("column:parent",),
                        ("x_parent",),
                        resolver=RelationshipResolver(
                            origin=ResolverOrigin.DATASET,
                            dataset_id="dataset:documents",
                        ),
                    ),
                )
                if related and name == "entries"
                else (),
                fields=(ScalarFieldMapping("name", "column:reference"),),
            )
            for name in names
        )
        definition = MappingDefinition(
            mapping_id="mapping:prepared-readers",
            schema_hash=fixtures.SCHEMA_HASH,
            source_selection_hash=fixtures.PHYSICAL_HASH,
            datasets=mappings,
        )
        rows = []
        for ordinal, name in enumerate(names):
            row = fixtures._canonical_row(
                "a" if ordinal == 0 else "b",
                ordinal + 2,
                dataset=name,
                source_identity=(f"KEY-{ordinal}",),
                target_identity=("COLLISION" if collision else f"KEY-{ordinal}",),
                target_scope=(
                    LogicalReference(
                        origin="incoming", dataset="documents", key=("KEY-0",)
                    ),
                )
                if related and name == "entries"
                else (),
                physical_dataset_id=f"dataset:{name}",
            )
            rows.append(
                replace(
                    row,
                    target_model=f"x_custom.{name}",
                )
            )
        if collision:
            rows.append(
                replace(
                    rows[0],
                    row_id="sha256:" + "c" * 64,
                    source_row=3,
                    source_identity=("OTHER",),
                    lineage=replace(
                        rows[0].lineage, source_row=3, physical_source_rows=(3,)
                    ),
                )
            )
        staging = replace(
            fixtures._staging(workspace.workspace_id, tuple(rows)),
            mapping_id=definition.mapping_id,
            mapping_hash=definition.content_hash,
            compiled_plan_hash=definition.content_hash,
            rows=tuple(
                replace(
                    row,
                    lineage=replace(row.lineage, mapping_hash=definition.content_hash),
                )
                for row in rows
            ),
        )
        physical_rows = {
            f"dataset:{name}": tuple(
                row.source_row for row in staging.rows if row.dataset == name
            )
            for name in names
        }
        arguments = {
            "workspace_state": workspace,
            "physical_rows": physical_rows,
            "ruleset": default_quality_ruleset(
                workspace_id=workspace.workspace_id,
                mapping_hash=definition.content_hash,
                schema_hash=fixtures.SCHEMA_HASH,
                datasets=names,
            ),
            "published_staging_content_hash": staging.content_hash,
        }
        stored = replace(
            fixtures._stored_staging(staging),
            validated_content_hash=staging.content_hash,
        )
        return staging, stored, definition, arguments

    def _assert_oracle(self, actual, staging, arguments):
        expected = evaluate_quality(staging=staging, **arguments)
        materialized = fixtures._materialized_bounded_quality(actual)
        self.assertEqual(materialized.to_json(), expected.to_json())
        self.assertEqual(materialized.content_hash, expected.content_hash)

    def test_index_only_reader_preserves_multiple_dataset_evidence_without_full_rows(
        self,
    ):
        staging, stored, definition, arguments = self._case()
        reader = _IndexOnlyRows(staging.rows)
        reader.forbid_canonical_reads = True

        actual = build_bounded_quality_run(
            staging=replace(stored, rows=reader), definition=definition, **arguments
        )

        self._assert_oracle(actual, staging, arguments)
        self.assertEqual(reader.index_calls, 1)
        self.assertEqual(reader.canonical_reads, 0)

    def test_incoming_group_without_its_reader_refuses_before_full_row_fallback(self):
        for noncallable_member in (False, True):
            with self.subTest(noncallable_member=noncallable_member):
                staging, stored, definition, arguments = self._case(related=True)
                reader = _IndexOnlyRows(staging.rows)
                reader.forbid_canonical_reads = True
                if noncallable_member:
                    reader.bounded_identity_group_findings = 7

                with self.assertRaises(BoundedQualityUnsupported):
                    build_bounded_quality_run(
                        staging=replace(stored, rows=reader),
                        definition=definition,
                        **arguments,
                    )

                self.assertEqual(reader.index_calls, 1)
                self.assertEqual(reader.canonical_reads, 0)

    def test_explicit_index_refusal_retains_authoritative_bounded_fallback(self):
        staging, stored, definition, arguments = self._case(multiple_datasets=False)
        reader = _DecliningIndexRows(staging.rows)

        actual = build_bounded_quality_run(
            staging=replace(stored, rows=reader), definition=definition, **arguments
        )

        self._assert_oracle(actual, staging, arguments)
        self.assertEqual(reader.index_calls, 1)
        self.assertGreater(reader.canonical_reads, 0)

    def test_noncallable_index_member_does_not_admit_an_indexed_route(self):
        staging, stored, definition, arguments = self._case(multiple_datasets=False)
        reader = _IndexOnlyRows(staging.rows)
        reader.bounded_quality_index = 7

        actual = build_bounded_quality_run(
            staging=replace(stored, rows=reader), definition=definition, **arguments
        )

        self._assert_oracle(actual, staging, arguments)
        self.assertEqual(reader.index_calls, 0)
        self.assertGreater(reader.canonical_reads, 0)

    def test_encoded_publication_remains_independent_for_clean_and_colliding_rows(self):
        for collision in (False, True):
            with self.subTest(collision=collision):
                staging, stored, definition, arguments = self._case(
                    multiple_datasets=False, collision=collision
                )
                reader = _EncodedOnlyRows(staging.rows)
                actual = build_bounded_quality_run(
                    staging=replace(stored, rows=reader),
                    definition=definition,
                    **arguments,
                )
                reader.forbid_canonical_reads = True
                publication_connection = object()

                row_results = tuple(
                    row
                    for batch in actual.row_results.iter_batches(
                        publication_connection, 1
                    )
                    for row in batch
                )
                accounting = tuple(
                    row
                    for batch in actual.source_accounting.iter_batches(
                        publication_connection, 1
                    )
                    for row in batch
                )

                self._assert_oracle(
                    replace(
                        actual,
                        row_results=row_results,
                        source_accounting=accounting,
                    ),
                    staging,
                    arguments,
                )
                self.assertEqual(
                    reader.publication_connections,
                    [publication_connection, publication_connection],
                )
