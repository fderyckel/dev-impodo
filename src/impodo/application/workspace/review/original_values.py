"""Bind saved field occurrences to complete physical original-cell cohorts.

Preparation records actual scalar dependencies. Static mapping proofs remain
available for current direct and relationship evidence. Neither path evaluates
values during export; a metadata index maps saved lineage to physical cells.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from impodo.application.data_version.source_snapshots import (
    validate_snapshot_for_dataset,
)
from impodo.application.workspace.preparation.readiness_ports import (
    PreflightSourceRepository,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import DatasetMapping
from impodo.domain.preflight.fields_set_aside import (
    FieldSetAside,
    FieldSetAsideEvidence,
)
from impodo.domain.preflight.original_values import (
    OriginalSourceCoordinate,
    OriginalSourceInput,
    OriginalSourceOccurrence,
    OriginalSourceRequest,
    OriginalSourceTarget,
    OriginalSourceValue,
)
from impodo.domain.preflight.physical_lineage import PhysicalColumnIndex
from impodo.domain.preflight.relationship_inputs import relationship_original_inputs
from impodo.domain.preflight.scalar_inputs import (
    scalar_formula_aliases,
    scalar_original_inputs,
)
from impodo.domain.preparation.source import SourceLoadError
from impodo.domain.preparation.staging_contracts import CanonicalLineage
from impodo.domain.source_binding import SourceOriginKind
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.workspace.contracts import SourceDataset, SourceSelection
from impodo.domain.workspace.derived_entities import DerivedEntityPlan

_MISSING_SELECTION = (
    "Original source values are unavailable because the frozen physical source "
    "selection was not recorded."
)
_MISSING_SNAPSHOTS = (
    "Original source values are unavailable for some file cells because "
    "their frozen source snapshots are missing."
)
_STALE = (
    "The original source evidence does not match this review. Prepare the current "
    "data and reopen Final review before exporting."
)


@dataclass(frozen=True, slots=True)
class WorkbookFieldProvider:
    """Bind a mapped field to the inputs that can be proven for one row."""

    dataset: SourceDataset
    saved_keys: tuple[str, ...] | None
    input_keys: tuple[str, ...] | None
    relationship: bool
    source_keys: frozenset[str]

    def inputs_for(
        self, field: str, saved: tuple[str, ...], lineage: CanonicalLineage | None
    ) -> tuple[str, ...] | None:
        """Prefer the actual scalar read set, including a recorded empty set."""
        if lineage is not None and not self.relationship and field in lineage.field_input_keys:
            if lineage.field_sources.get(field) != saved:
                return None
            inputs = lineage.field_input_keys[field]
        elif self.saved_keys == saved:
            inputs = self.input_keys
        else:
            return None
        if inputs is not None and (
            not {*saved, *inputs} <= self.source_keys or len(set(inputs)) != len(inputs)
        ):
            raise ReadinessError(_STALE)
        return inputs


@dataclass(frozen=True, slots=True)
class _InputGroup:
    finding_index: int
    dataset_id: str
    occurrence: OriginalSourceOccurrence
    input_keys: tuple[str, ...]
    lineage: CanonicalLineage | None


@dataclass(frozen=True, slots=True)
class _PhysicalGroup:
    finding_index: int
    occurrence: OriginalSourceOccurrence
    coordinates: tuple[OriginalSourceCoordinate, ...]


def plan_workbook_original_values(
    *,
    fields: FieldSetAsideEvidence | None,
    selection: SourceSelection | None,
    physical_selection_hash: str,
    mappings: tuple[DatasetMapping, ...],
    sources: PreflightSourceRepository,
    workspace_id: str,
    restricted: bool,
    workbook_lineage: Mapping[str, CanonicalLineage] | None = None,
    derived_plan: DerivedEntityPlan | None = None,
) -> tuple[FieldSetAsideEvidence | None, tuple[OriginalSourceRequest, ...]]:
    """Prove complete original groups with at most two bulk source metadata reads.

    Restricted exports return before source access. Recorded empty dependencies
    are known outcomes without a source cell. Missing historical evidence stays
    unavailable; contradictory bindings fail closed. Physical artifact values
    are read later by the bounded adapter.
    """

    if fields is None:
        return None, ()
    fields = _clear_inputs(fields, restricted=restricted)
    if restricted or not fields.findings:
        return fields, ()
    if selection is None:
        return _with_gap(fields, _MISSING_SELECTION), ()
    groups = _candidate_groups(fields, selection, mappings, workbook_lineage)
    known_empty = tuple(
        _PhysicalGroup(group.finding_index, group.occurrence, ())
        for group in groups
        if not group.input_keys
    )
    fields = _with_proven_inputs(fields, known_empty)
    groups = tuple(group for group in groups if group.input_keys)
    if not groups:
        return fields, ()
    if not physical_selection_hash:
        return _with_gap(fields, _MISSING_SELECTION), ()
    physical = sources.get_source_selection(workspace_id)
    if physical is None:
        return _with_gap(fields, _MISSING_SELECTION), ()
    if (
        physical.content_hash != physical_selection_hash
        or physical.data_version_id != selection.data_version_id
    ):
        raise ReadinessError(_STALE)
    try:
        index = PhysicalColumnIndex(selection, physical, derived_plan)
        resolved = tuple(_resolve_group(index, group) for group in groups)
    except (KeyError, TypeError, ValueError) as error:
        raise ReadinessError(_STALE) from error
    physical_groups = tuple(group for group in resolved if group is not None)
    fields = _with_physical_labels(
        _with_proven_inputs(fields, physical_groups), physical
    )
    requested_groups = tuple(group for group in physical_groups if group.coordinates)
    if not requested_groups:
        return fields, ()
    snapshots = sources.get_current_source_snapshots(workspace_id)
    requests, missing = _bound_requests(physical, snapshots, requested_groups)
    return (_with_gap(fields, _MISSING_SNAPSHOTS) if missing else fields), requests


def _clear_inputs(
    fields: FieldSetAsideEvidence, *, restricted: bool
) -> FieldSetAsideEvidence:
    findings = tuple(
        replace(finding, source_inputs=(), source_inputs_known=False)
        if finding.source_inputs or finding.source_inputs_known
        else finding
        for finding in fields.findings
    )
    if findings == fields.findings and fields.source_values_restricted == restricted:
        return fields
    return replace(fields, findings=findings, source_values_restricted=restricted)


def workbook_field_providers(
    selection: SourceSelection,
    mappings: tuple[DatasetMapping, ...],
    target_models: Mapping[str, str],
    *,
    include_identity: bool = False,
) -> dict[tuple[str, str], list[WorkbookFieldProvider]]:
    """Index the reviewed mapping once for workbook source-cell resolution."""

    datasets = {dataset.dataset_id: dataset for dataset in selection.datasets}
    if len(datasets) != len(selection.datasets):
        raise ReadinessError(_STALE)
    aliases: dict[str, Mapping[str, str] | None] = {}
    dataset_keys: dict[str, frozenset[str]] = {}
    result: dict[tuple[str, str], list[WorkbookFieldProvider]] = {}
    for mapping in mappings:
        dataset = datasets.get(mapping.dataset_id)
        if (
            dataset is None
            or dataset.origin is SourceOriginKind.ODOO
            or target_models.get(dataset.name) != mapping.target_model
        ):
            continue
        if dataset.dataset_id not in dataset_keys:
            dataset_keys[dataset.dataset_id] = frozenset(
                column.stable_key for column in dataset.columns
            )
        keys = dataset_keys[dataset.dataset_id]
        if len(keys) != len(dataset.columns):
            raise ReadinessError(_STALE)
        for field in mapping.fields:
            if field.transform.formula.strip() and dataset.dataset_id not in aliases:
                aliases[dataset.dataset_id] = scalar_formula_aliases(dataset.columns)
            inputs = scalar_original_inputs(
                field, source_keys=keys, formula_aliases=aliases.get(dataset.dataset_id)
            )
            result.setdefault((dataset.name, field.target_field), []).append(
                WorkbookFieldProvider(dataset, inputs, inputs, False, keys)
            )
        for relationship in mapping.relationships:
            inputs = relationship_original_inputs(relationship)
            result.setdefault((dataset.name, relationship.target_field), []).append(
                WorkbookFieldProvider(
                    dataset,
                    inputs.saved_keys if inputs is not None else None,
                    inputs.input_keys if inputs is not None else None,
                    True,
                    keys,
                )
            )
        if include_identity:
            # A write can also supply a field from its direct identity or scope.
            # An explicit field mapping takes precedence in canonical lineage.
            for component in (*mapping.target_identity, *mapping.target_scope):
                if component.resolver is not None or len(component.target_fields) != len(component.source_column_keys):
                    continue
                for target, source in zip(component.target_fields, component.source_column_keys, strict=True):
                    result.setdefault(
                        (dataset.name, target),
                        [WorkbookFieldProvider(dataset, component.source_column_keys, (source,), False, keys)],
                    )
    return result


def _candidate_groups(
    fields: FieldSetAsideEvidence,
    selection: SourceSelection,
    mappings: tuple[DatasetMapping, ...],
    workbook_lineage: Mapping[str, CanonicalLineage] | None,
) -> tuple[_InputGroup, ...]:
    providers = workbook_field_providers(selection, mappings, fields.target_models)
    result: list[_InputGroup] = []
    for finding_index, finding in enumerate(fields.findings):
        matches = providers.get((finding.dataset, finding.target_field), ())
        if finding.source_row is None or len(matches) != 1:
            continue
        provider = matches[0]
        if finding.comparison_issue and not provider.relationship:
            continue
        lineage = _saved_lineage(finding, workbook_lineage)
        if (
            lineage is not None
            and lineage.source_selection_hash != selection.content_hash
        ):
            raise ReadinessError(_STALE)
        if provider.dataset.origin is SourceOriginKind.DERIVED and lineage is None:
            continue
        input_keys = provider.inputs_for(finding.target_field, finding.source_keys, lineage)
        if input_keys is None:
            continue
        if type(finding.source_row) is not int or finding.source_row < 1:
            raise ReadinessError(_STALE)
        occurrence = OriginalSourceOccurrence(
            finding.dataset,
            finding.source_row,
            finding.target_field,
            finding.source_keys,
            finding.source_trace_id,
        )
        result.append(
            _InputGroup(
                finding_index,
                provider.dataset.dataset_id,
                occurrence,
                input_keys,
                lineage,
            )
        )
    return tuple(result)


def _saved_lineage(
    finding: FieldSetAside,
    workbook_lineage: Mapping[str, CanonicalLineage] | None,
) -> CanonicalLineage | None:
    if not finding.source_trace_id or workbook_lineage is None:
        return None
    lineage = workbook_lineage.get(finding.source_trace_id)
    if (
        lineage is None
        or lineage.dataset != finding.dataset
        or lineage.source_row != finding.source_row
    ):
        raise ReadinessError(_STALE)
    return lineage


def _resolve_group(
    index: PhysicalColumnIndex,
    group: _InputGroup,
) -> _PhysicalGroup | None:
    coordinates = (
        index.resolve(group.dataset_id, group.input_keys, group.lineage)
        if group.lineage is not None
        else index.resolve_direct(
            group.dataset_id, group.input_keys, group.occurrence.source_row
        )
    )
    return (
        _PhysicalGroup(group.finding_index, group.occurrence, coordinates)
        if coordinates is not None
        else None
    )


def _with_proven_inputs(
    fields: FieldSetAsideEvidence,
    groups: tuple[_PhysicalGroup, ...],
) -> FieldSetAsideEvidence:
    if not groups:
        return fields
    by_finding = {group.finding_index: group.coordinates for group in groups}
    return replace(
        fields,
        findings=tuple(
            replace(
                finding,
                source_inputs=tuple(
                    OriginalSourceInput(coordinate, OriginalSourceValue.unavailable())
                    for coordinate in by_finding[index]
                ),
                source_inputs_known=True,
            )
            if index in by_finding
            else finding
            for index, finding in enumerate(fields.findings)
        ),
    )


def _with_physical_labels(
    fields: FieldSetAsideEvidence, physical: SourceSelection
) -> FieldSetAsideEvidence:
    coordinates = {
        (item.coordinate.dataset_id, item.coordinate.source_key)
        for finding in fields.findings
        for item in finding.source_inputs
    }
    if not coordinates:
        return fields
    labels = dict(fields.source_labels)
    labels.update(
        ((dataset.name, column.stable_key), column.source_name)
        for dataset in physical.datasets
        if dataset.origin is SourceOriginKind.FILE
        for column in dataset.columns
        if (dataset.dataset_id, column.stable_key) in coordinates
    )
    return replace(fields, source_labels=labels)


def _bound_requests(
    physical: SourceSelection,
    snapshots: tuple[SourceSnapshot, ...],
    groups: tuple[_PhysicalGroup, ...],
) -> tuple[tuple[OriginalSourceRequest, ...], bool]:
    datasets = {dataset.dataset_id: dataset for dataset in physical.datasets}
    by_dataset = {snapshot.dataset_id: snapshot for snapshot in snapshots}
    if len(by_dataset) != len(snapshots):
        raise ReadinessError(_STALE)
    needed = {
        coordinate.dataset_id for group in groups for coordinate in group.coordinates
    }
    missing = bool(needed - by_dataset.keys())
    for dataset_id in sorted(needed & by_dataset.keys()):
        try:
            validate_snapshot_for_dataset(
                physical, datasets[dataset_id], by_dataset[dataset_id]
            )
        except (KeyError, SourceLoadError) as error:
            raise ReadinessError(_STALE) from error
    targets: dict[str, set[OriginalSourceTarget]] = {}
    for group in groups:
        if any(
            coordinate.dataset_id not in by_dataset for coordinate in group.coordinates
        ):
            continue
        for coordinate in group.coordinates:
            targets.setdefault(coordinate.dataset_id, set()).add(
                OriginalSourceTarget(group.occurrence, coordinate, group.coordinates)
            )
    requests = tuple(
        OriginalSourceRequest(
            snapshot=by_dataset[dataset_id],
            cells=tuple(sorted({target.coordinate for target in group_targets})),
            targets=tuple(sorted(group_targets)),
        )
        for dataset_id, group_targets in sorted(targets.items())
    )
    return requests, missing


def _with_gap(fields: FieldSetAsideEvidence, gap: str) -> FieldSetAsideEvidence:
    return replace(
        fields, coverage_gaps=tuple(dict.fromkeys((*fields.coverage_gaps, gap)))
    )
