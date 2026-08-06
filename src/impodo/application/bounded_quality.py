"""Bounded Stage-F construction for clean direct-table preparation runs.

The general quality evaluator remains authoritative for findings, guided
rules, advanced checks, identity collisions, and relationship propagation.
This module recognizes the common large-file case where those global findings
are absent and exposes the exact same evidence as lazy sequences backed by the
finalized preparation session.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import json
from typing import overload

from ..domain.staging.preparation_session import StoredCanonicalStagingRun
from ..projects import MigrationProject
from ..quality import (
    MANDATORY_QUALITY_FAMILIES,
    QualityError,
    QualityRuleFamily,
    QualityRuleSet,
    QualityRuleSource,
    QualityRowResult,
    SourceAccountingEntry,
    SourceAccountingState,
    StoredQualityRun,
    clean_quality_row_result,
    has_logical_references,
    quality_identity_key,
    retention_context_hash,
)
from ..staging_contracts import CanonicalRow, CanonicalStagingRun, StagingDisposition


class BoundedQualityUnsupported(ValueError):
    """Signal that the complete quality evaluator must handle this run."""


class _CleanQualityRows(Sequence[QualityRowResult]):
    """Translate bounded canonical rows without retaining prior batches."""

    def __init__(self, rows: Sequence[CanonicalRow]) -> None:
        self._rows = rows

    def __len__(self) -> int:
        return len(self._rows)

    @overload
    def __getitem__(self, index: int) -> QualityRowResult: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[QualityRowResult, ...]: ...

    def __getitem__(
        self,
        index: int | slice,
    ) -> QualityRowResult | tuple[QualityRowResult, ...]:
        if isinstance(index, slice):
            return tuple(clean_quality_row_result(row) for row in self._rows[index])
        return clean_quality_row_result(self._rows[index])

    def __iter__(self) -> Iterator[QualityRowResult]:
        for row in self._rows:
            yield clean_quality_row_result(row)

    def iter_batches(self, connection, batch_size: int):
        """Decode session rows through the publisher's existing connection."""

        encoded_batches = getattr(self._rows, "iter_encoded_batches", None)
        if not callable(encoded_batches):
            for start in range(0, len(self), batch_size):
                yield self[start : start + batch_size]
            return
        for batch in encoded_batches(connection, batch_size):
            yield tuple(
                clean_quality_row_result(
                    CanonicalRow.from_dict(json.loads(str(row_json)))
                )
                for *_metadata, row_json in batch
            )


class _DirectSourceAccounting(Sequence[SourceAccountingEntry]):
    """Expose the prevalidated one-to-one physical lineage lazily."""

    def __init__(self, rows: Sequence[CanonicalRow]) -> None:
        self._rows = rows

    def __len__(self) -> int:
        return len(self._rows)

    @overload
    def __getitem__(self, index: int) -> SourceAccountingEntry: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[SourceAccountingEntry, ...]: ...

    def __getitem__(
        self,
        index: int | slice,
    ) -> SourceAccountingEntry | tuple[SourceAccountingEntry, ...]:
        if isinstance(index, slice):
            return tuple(self._entry(row) for row in self._rows[index])
        return self._entry(self._rows[index])

    def __iter__(self) -> Iterator[SourceAccountingEntry]:
        for row in self._rows:
            yield self._entry(row)

    def iter_batches(self, connection, batch_size: int):
        """Decode session lineage through the publisher's open transaction."""

        encoded_batches = getattr(self._rows, "iter_encoded_batches", None)
        if not callable(encoded_batches):
            for start in range(0, len(self), batch_size):
                yield self[start : start + batch_size]
            return
        for batch in encoded_batches(connection, batch_size):
            yield tuple(
                self._entry(CanonicalRow.from_dict(json.loads(str(row_json))))
                for *_metadata, row_json in batch
            )

    @staticmethod
    def _entry(row: CanonicalRow) -> SourceAccountingEntry:
        physical_dataset_id, source_rows = next(
            iter(row.lineage.physical_sources.items())
        )
        return SourceAccountingEntry(
            physical_dataset_id=physical_dataset_id,
            source_row=source_rows[0],
            state=SourceAccountingState.REPRESENTED,
            canonical_row_ids=(row.row_id,),
        )


def build_bounded_quality_run(
    *,
    project: MigrationProject,
    staging: StoredCanonicalStagingRun,
    physical_rows: Mapping[str, tuple[int, ...]],
    ruleset: QualityRuleSet,
    published_staging_content_hash: str,
) -> StoredQualityRun:
    """Return lazy evidence when the complete evaluator would find no issues.

    Capability is proven with one bounded validation pass. Any input requiring
    global or finding-producing semantics falls back to ``evaluate_quality``;
    it is never approximated by this fast path.
    """

    if staging.project_id != project.project_id or ruleset.project_id != project.project_id:
        raise QualityError("Quality evidence belongs to another project")
    if ruleset.mapping_hash != staging.mapping_hash or ruleset.schema_hash != staging.schema_hash:
        raise QualityError("Data checks no longer match the submitted field matches")
    if staging.issues or ruleset.reference_bundle_hash is not None:
        raise BoundedQualityUnsupported

    expected_rules = {
        (dataset.dataset, QualityRuleFamily(family))
        for dataset in staging.datasets
        for family in MANDATORY_QUALITY_FAMILIES
    }
    actual_rules = {(rule.dataset, rule.family) for rule in ruleset.rules}
    if (
        actual_rules != expected_rules
        or len(ruleset.rules) != len(expected_rules)
        or any(rule.source is not QualityRuleSource.MAPPING_DERIVED for rule in ruleset.rules)
    ):
        raise BoundedQualityUnsupported
    if len(physical_rows) != 1:
        raise BoundedQualityUnsupported

    physical_dataset_id, source_rows = next(iter(physical_rows.items()))
    remaining_physical_rows = set(source_rows)
    seen_identity_keys: set[bytes] = set()
    eligible_row_ids: set[str] = set()
    previous_row_key: tuple[str, int, str] | None = None
    previous_accounting_row = 0
    row_count = 0

    for row in staging.rows:
        row_count += 1
        row_key = (row.dataset, row.source_row, row.row_id)
        if previous_row_key is not None and row_key < previous_row_key:
            raise BoundedQualityUnsupported
        previous_row_key = row_key
        if (
            row.issues
            or row.disposition
            not in {StagingDisposition.CANDIDATE, StagingDisposition.REFERENCE}
            or has_logical_references(row)
            or row.row_id in eligible_row_ids
        ):
            raise BoundedQualityUnsupported

        identity_key = quality_identity_key(row)
        if identity_key is not None:
            if identity_key in seen_identity_keys:
                raise BoundedQualityUnsupported
            seen_identity_keys.add(identity_key)

        physical_sources = row.lineage.physical_sources
        if len(physical_sources) != 1 or physical_dataset_id not in physical_sources:
            raise BoundedQualityUnsupported
        linked_rows = physical_sources[physical_dataset_id]
        if len(linked_rows) != 1:
            raise BoundedQualityUnsupported
        physical_source_row = linked_rows[0]
        if (
            physical_source_row <= previous_accounting_row
            or physical_source_row not in remaining_physical_rows
        ):
            raise BoundedQualityUnsupported
        previous_accounting_row = physical_source_row
        remaining_physical_rows.remove(physical_source_row)
        eligible_row_ids.add(row.row_id)

    if (
        row_count != len(staging.rows)
        or remaining_physical_rows
        or row_count != len(source_rows)
    ):
        raise BoundedQualityUnsupported

    return StoredQualityRun(
        project_id=project.project_id,
        staging_content_hash=published_staging_content_hash,
        ruleset_hash=ruleset.content_hash,
        mapping_hash=staging.mapping_hash,
        schema_hash=staging.schema_hash,
        retention_context_hash=retention_context_hash(project),
        row_results=_CleanQualityRows(staging.rows),
        source_accounting=_DirectSourceAccounting(staging.rows),
        issues=(),
        quarantine=(),
        effective_dataset_hash=published_staging_content_hash,
        eligible_row_ids=frozenset(eligible_row_ids),
        summary_counts={
            "ready_count": row_count,
            "review_count": 0,
            "quarantined_count": 0,
            "excluded_count": 0,
            "blocked_count": 0,
        },
    )


def materialize_staging_run(
    staging: StoredCanonicalStagingRun,
) -> CanonicalStagingRun:
    """Restore the complete contract only for unsupported quality semantics."""

    return CanonicalStagingRun(
        project_id=staging.project_id,
        mapping_id=staging.mapping_id,
        physical_selection_hash=staging.physical_selection_hash,
        source_selection_hash=staging.source_selection_hash,
        mapping_hash=staging.mapping_hash,
        schema_hash=staging.schema_hash,
        derived_plan_hash=staging.derived_plan_hash,
        datasets=staging.datasets,
        rows=tuple(staging.rows),
        issues=staging.issues,
        reconciliation=staging.reconciliation,
        compiled_plan_hash=staging.compiled_plan_hash,
        control_totals=staging.control_totals,
        evaluator_version=staging.evaluator_version,
        contract_version=staging.contract_version,
    )
