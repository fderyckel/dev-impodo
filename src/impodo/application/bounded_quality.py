"""Bounded Stage-F construction for direct-table preparation runs.

The general quality evaluator remains authoritative for advanced statistics,
guided rules, and resolved datasets. This module keeps ordinary mapping
findings, identity collisions, and relationship propagation bounded while it
exposes the exact same evidence through lazy finalized-session sequences.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
import json
from typing import overload

from ..domain.staging.preparation_session import StoredCanonicalStagingRun
from ..projects import MigrationProject
from ..quality import (
    MANDATORY_QUALITY_FAMILIES,
    QualityError,
    QualityDisposition,
    QualityIssue,
    QualityOutcomePolicy,
    QualityRuleFamily,
    QualityRuleSet,
    QualityRuleSource,
    QualityRowResult,
    QuarantineEntry,
    SourceAccountingEntry,
    SourceAccountingState,
    StoredQualityRun,
    clean_quality_row_result,
    _correction_route,
    _effective_disposition,
    _family_for_issue,
    _hash,
    _logical_references,
    _quality_issue,
    _record_label,
    _setup_issue,
    quality_identity_key,
    retention_context_hash,
)
from ..models import canonical_json_bytes, portable_value
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


@dataclass(frozen=True, slots=True)
class _IssueRow:
    row_id: str
    dataset: str
    source_row: int
    disposition: StagingDisposition


class _BoundedQualityRows(Sequence[QualityRowResult]):
    """Replay row decisions from compact finding links."""

    def __init__(
        self,
        rows: Sequence[CanonicalRow],
        issues: Mapping[str, QualityIssue],
        row_issue_ids: Mapping[str, set[str]],
    ) -> None:
        self._rows = rows
        self._issues = issues
        self._row_issue_ids = row_issue_ids

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
            return tuple(self._result(row) for row in self._rows[index])
        return self._result(self._rows[index])

    def __iter__(self) -> Iterator[QualityRowResult]:
        for row in self._rows:
            yield self._result(row)

    def iter_batches(self, connection, batch_size: int):
        encoded_batches = getattr(self._rows, "iter_encoded_batches", None)
        if not callable(encoded_batches):
            for start in range(0, len(self), batch_size):
                yield self[start : start + batch_size]
            return
        for batch in encoded_batches(connection, batch_size):
            yield tuple(
                self._result(CanonicalRow.from_dict(json.loads(str(row_json))))
                for *_metadata, row_json in batch
            )

    def _result(self, row: CanonicalRow) -> QualityRowResult:
        issue_ids = tuple(sorted(self._row_issue_ids.get(row.row_id, ())))
        issues = tuple(self._issues[item] for item in issue_ids)
        return QualityRowResult(
            row_id=row.row_id,
            dataset=row.dataset,
            source_row=row.source_row,
            record_label=_record_label(row),
            base_disposition=QualityDisposition(row.disposition.value),
            effective_disposition=_effective_disposition(row, issues),
            issue_ids=issue_ids,
            requires_review=any(
                issue.policy is QualityOutcomePolicy.WARNING for issue in issues
            ),
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
    """Return lazy evidence for direct rows and compact mapping findings."""

    if staging.project_id != project.project_id or ruleset.project_id != project.project_id:
        raise QualityError("Quality evidence belongs to another project")
    if ruleset.mapping_hash != staging.mapping_hash or ruleset.schema_hash != staging.schema_hash:
        raise QualityError("Data checks no longer match the submitted field matches")
    if ruleset.reference_bundle_hash is not None:
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
    collision_counts: dict[bytes, int] = {}
    all_row_ids: set[str] = set()
    issue_map: dict[str, QualityIssue] = {}
    row_issue_ids: dict[str, set[str]] = {}
    rules_by_family = {
        (rule.dataset, rule.family): rule for rule in ruleset.rules
    }
    previous_row_key: tuple[str, int, str] | None = None
    previous_accounting_row = 0
    has_relationships = False
    dirty = bool(staging.issues)
    row_count = 0

    for row in staging.rows:
        row_count += 1
        row_key = (row.dataset, row.source_row, row.row_id)
        if previous_row_key is not None and row_key < previous_row_key:
            raise BoundedQualityUnsupported
        previous_row_key = row_key
        if row.row_id in all_row_ids:
            raise BoundedQualityUnsupported
        all_row_ids.add(row.row_id)
        if row.issues or row.disposition not in {
            StagingDisposition.CANDIDATE,
            StagingDisposition.REFERENCE,
        }:
            dirty = True
        references = _logical_references(row)
        has_relationships = has_relationships or bool(references)
        dirty = dirty or bool(references)

        for item in row.issues:
            family = _family_for_issue(item)
            rule = rules_by_family.get((row.dataset, family))
            if rule is None:
                continue
            policy = (
                QualityOutcomePolicy.WARNING
                if item.severity == "warning"
                else rule.outcome
            )
            issue = _quality_issue(
                project,
                rule,
                row,
                item.code,
                item.message,
                (item.field,) if item.field else (),
                policy=policy,
            )
            issue_map[issue.issue_id] = issue
            row_issue_ids.setdefault(row.row_id, set()).add(issue.issue_id)

        identity_key = quality_identity_key(row)
        if identity_key is not None:
            if identity_key in seen_identity_keys:
                collision_counts[identity_key] = (
                    collision_counts.get(identity_key, 1) + 1
                )
                dirty = True
            else:
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

    if (
        row_count != len(staging.rows)
        or remaining_physical_rows
        or row_count != len(source_rows)
    ):
        raise BoundedQualityUnsupported

    if not dirty:
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
            eligible_row_ids=all_row_ids,
            summary_counts={
                "ready_count": row_count,
                "review_count": 0,
                "quarantined_count": 0,
                "excluded_count": 0,
                "blocked_count": 0,
            },
        )

    # The first pass needs complete uniqueness and reconciliation indexes, but
    # the dirty evidence pass does not. Release them before decoding rows again
    # so collision and relationship handling can reuse that memory.
    del remaining_physical_rows
    del seen_identity_keys
    del all_row_ids

    setup_staging_issues = [
        item
        for item in staging.issues
        if item.dataset and item.source_row is None
    ]
    for item in setup_staging_issues:
        issue = _setup_issue(
            project,
            item.dataset or "",
            _family_for_issue(item),
            item.code,
            item.message,
        )
        issue_map[issue.issue_id] = issue
    staging_issues_by_coordinate: dict[
        tuple[str, int],
        list[object],
    ] = {}
    for item in staging.issues:
        if item.dataset and item.source_row is not None:
            staging_issues_by_coordinate.setdefault(
                (item.dataset, item.source_row),
                [],
            ).append(item)

    source_index: dict[tuple[str, bytes], str | tuple[str, ...]] = {}
    relation_rows: dict[str, _IssueRow] = {}
    relation_coordinates: set[tuple[str, int]] = set()
    duplicate_relation_coordinates: set[tuple[str, int]] = set()
    for row in staging.rows:
        identity_key = quality_identity_key(row)
        if identity_key in collision_counts:
            rule = rules_by_family.get(
                (row.dataset, QualityRuleFamily.IDENTITY_COLLISION)
            )
            if rule is not None:
                issue = _quality_issue(
                    project,
                    rule,
                    row,
                    "POST_TRANSFORM_IDENTITY_COLLISION",
                    f"{collision_counts[identity_key]} prepared records would use the same Odoo match. All were set aside for review.",
                    (),
                    policy=rule.outcome,
                )
                issue_map[issue.issue_id] = issue
                row_issue_ids.setdefault(row.row_id, set()).add(issue.issue_id)
        for item in staging_issues_by_coordinate.get(
            (row.dataset, row.source_row),
            (),
        ):
            family = _family_for_issue(item)
            rule = rules_by_family.get((row.dataset, family))
            if rule is None:
                continue
            policy = (
                QualityOutcomePolicy.WARNING
                if item.severity == "warning"
                else rule.outcome
            )
            issue = _quality_issue(
                project,
                rule,
                row,
                item.code,
                item.message,
                (item.field,) if item.field else (),
                policy=policy,
            )
            issue_map[issue.issue_id] = issue
            row_issue_ids.setdefault(row.row_id, set()).add(issue.issue_id)
        if has_relationships:
            source_key = (
                row.dataset,
                canonical_json_bytes(portable_value(row.source_identity)),
            )
            existing = source_index.get(source_key)
            if existing is None:
                source_index[source_key] = row.row_id
            elif isinstance(existing, tuple):
                source_index[source_key] = (*existing, row.row_id)
            else:
                source_index[source_key] = (existing, row.row_id)
            relation_rows[row.row_id] = _IssueRow(
                row.row_id,
                row.dataset,
                row.source_row,
                row.disposition,
            )
            coordinate = (row.dataset, row.source_row)
            if coordinate in relation_coordinates:
                duplicate_relation_coordinates.add(coordinate)
            relation_coordinates.add(coordinate)

    if has_relationships:
        _attach_relationship_findings(
            project=project,
            staging=staging,
            rules_by_family=rules_by_family,
            issue_map=issue_map,
            row_issue_ids=row_issue_ids,
            source_index=source_index,
            relation_rows=relation_rows,
            duplicate_coordinates=duplicate_relation_coordinates,
        )

    row_results = _BoundedQualityRows(
        staging.rows,
        issue_map,
        row_issue_ids,
    )
    summary_counts = {
        "ready_count": 0,
        "review_count": 0,
        "quarantined_count": 0,
        "excluded_count": 0,
        "blocked_count": 0,
    }
    eligible_row_ids: set[str] = set()
    quarantine: list[QuarantineEntry] = []
    for row in staging.rows:
        result = row_results._result(row)
        if result.effective_disposition in {
            QualityDisposition.CANDIDATE,
            QualityDisposition.REFERENCE,
        }:
            eligible_row_ids.add(row.row_id)
            if not result.requires_review:
                summary_counts["ready_count"] += 1
        if result.requires_review:
            summary_counts["review_count"] += 1
        if result.effective_disposition is QualityDisposition.QUARANTINED:
            summary_counts["quarantined_count"] += 1
        elif result.effective_disposition is QualityDisposition.EXCLUDED:
            summary_counts["excluded_count"] += 1
        elif result.effective_disposition is QualityDisposition.BLOCKED:
            summary_counts["blocked_count"] += 1
        for issue_id in result.issue_ids:
            issue = issue_map[issue_id]
            if (
                result.effective_disposition is not QualityDisposition.QUARANTINED
                or issue.policy is not QualityOutcomePolicy.QUARANTINE
            ):
                continue
            physical_sources = tuple(
                sorted(
                    f"{dataset_id}:{source_row}"
                    for dataset_id, source_numbers
                    in row.lineage.physical_sources.items()
                    for source_row in source_numbers
                )
            )
            quarantine.append(
                QuarantineEntry(
                    entry_id=_hash(
                        {
                            "effective_dataset": published_staging_content_hash,
                            "row_id": row.row_id,
                            "issue_id": issue.issue_id,
                        }
                    ),
                    row_id=row.row_id,
                    dataset=row.dataset,
                    source_row=row.source_row,
                    physical_sources=physical_sources,
                    issue_id=issue.issue_id,
                    rule_id=issue.rule_id,
                    reason_code=issue.reason_code,
                    explanation=issue.message,
                    affected_fields=issue.affected_fields,
                    owner_role=issue.owner_role,
                    owner_label=issue.owner_label,
                    review_by=None,
                    correction_route=_correction_route(issue.family),
                )
            )
    summary_counts["blocked_count"] += sum(
        issue.row_id is None and issue.policy is QualityOutcomePolicy.BLOCK
        for issue in issue_map.values()
    )
    summary_counts["review_count"] += sum(
        issue.row_id is None and issue.policy is QualityOutcomePolicy.WARNING
        for issue in issue_map.values()
    )
    return StoredQualityRun(
        project_id=project.project_id,
        staging_content_hash=published_staging_content_hash,
        ruleset_hash=ruleset.content_hash,
        mapping_hash=staging.mapping_hash,
        schema_hash=staging.schema_hash,
        retention_context_hash=retention_context_hash(project),
        row_results=row_results,
        source_accounting=_DirectSourceAccounting(staging.rows),
        issues=tuple(sorted(issue_map.values(), key=lambda item: item.issue_id)),
        quarantine=tuple(sorted(quarantine, key=lambda item: item.entry_id)),
        effective_dataset_hash=published_staging_content_hash,
        eligible_row_ids=eligible_row_ids,
        summary_counts=summary_counts,
    )


def _attach_relationship_findings(
    *,
    project: MigrationProject,
    staging: StoredCanonicalStagingRun,
    rules_by_family: Mapping[tuple[str, QualityRuleFamily], object],
    issue_map: dict[str, QualityIssue],
    row_issue_ids: dict[str, set[str]],
    source_index: Mapping[tuple[str, bytes], str | tuple[str, ...]],
    relation_rows: Mapping[str, _IssueRow],
    duplicate_coordinates: set[tuple[str, int]],
) -> None:
    """Propagate unsafe parent state through a compact relationship graph."""

    unsafe_dispositions = {
        QualityDisposition.BLOCKED,
        QualityDisposition.QUARANTINED,
        QualityDisposition.EXCLUDED,
    }
    dispositions = {
        row_id: _effective_disposition(
            row,
            (
                issue_map[issue_id]
                for issue_id in row_issue_ids.get(row_id, ())
            ),
        )
        for row_id, row in relation_rows.items()
    }
    dependents_by_parent: dict[str, str | list[str]] = {}
    unresolved_dependents: set[str] = set()
    relationship_rule_by_row: dict[str, object] = {}
    for row in staging.rows:
        if (row.dataset, row.source_row) in duplicate_coordinates:
            continue
        rule = rules_by_family.get(
            (row.dataset, QualityRuleFamily.RELATIONSHIP_READINESS)
        )
        if rule is None:
            continue
        relationship_rule_by_row[row.row_id] = rule
        seen_parent_ids: set[str] = set()
        for reference in _logical_references(row):
            if not reference.dataset:
                continue
            matches = source_index.get(
                (
                    reference.dataset,
                    canonical_json_bytes(portable_value(reference.key)),
                ),
                (),
            )
            if not isinstance(matches, str):
                unresolved_dependents.add(row.row_id)
                continue
            if matches in seen_parent_ids:
                continue
            seen_parent_ids.add(matches)
            existing = dependents_by_parent.get(matches)
            if existing is None:
                dependents_by_parent[matches] = row.row_id
            elif isinstance(existing, list):
                existing.append(row.row_id)
            else:
                dependents_by_parent[matches] = [existing, row.row_id]

    relationship_message = (
        "The linked incoming record is missing, ambiguous or set aside. "
        "This dependent record was also set aside."
    )

    def attach(row_id: str) -> bool:
        rule = relationship_rule_by_row.get(row_id)
        if rule is None:
            return False
        row = relation_rows[row_id]
        issue = _quality_issue(
            project,
            rule,
            row,
            "INCOMING_RELATIONSHIP_NOT_READY",
            relationship_message,
            (),
            policy=rule.outcome,
        )
        issue_ids = row_issue_ids.setdefault(row_id, set())
        if issue.issue_id in issue_ids:
            return False
        was_unsafe = dispositions[row_id] in unsafe_dispositions
        issue_map[issue.issue_id] = issue
        issue_ids.add(issue.issue_id)
        dispositions[row_id] = _effective_disposition(
            row,
            (issue_map[item] for item in issue_ids),
        )
        return (
            not was_unsafe
            and dispositions[row_id] in unsafe_dispositions
        )

    for row_id in unresolved_dependents:
        attach(row_id)
    queue = deque(
        row_id
        for row_id, disposition in dispositions.items()
        if disposition in unsafe_dispositions
    )
    while queue:
        parent_id = queue.popleft()
        dependents = dependents_by_parent.get(parent_id)
        dependent_ids = (
            ()
            if dependents is None
            else dependents
            if isinstance(dependents, list)
            else (dependents,)
        )
        for dependent_id in dependent_ids:
            if attach(dependent_id):
                queue.append(dependent_id)


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
