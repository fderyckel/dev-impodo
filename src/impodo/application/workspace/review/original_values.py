"""Plan exact file-cell evidence for saved field omissions without reading values.

Direct scalars and relationship key/scope components can prove physical source
inputs. Synthetic, derived, projected, and prepared reference providers keep
unavailable coverage. Artifact adapters hydrate complete proven input groups.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from impodo.application.data_version.source_snapshots import (
    validate_snapshot_for_dataset,
)
from impodo.application.workspace.preparation.readiness_ports import (
    PreflightSourceRepository,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    RelationshipMapping,
    RelationshipValueSource,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarValueSource,
    relationship_input_columns,
)
from impodo.domain.preflight.fields_set_aside import FieldSetAsideEvidence
from impodo.domain.preflight.original_values import (
    OriginalSourceInput,
    OriginalSourceRequest,
    OriginalSourceTarget,
    OriginalSourceValue,
)
from impodo.domain.preparation.source import SourceLoadError
from impodo.domain.source_binding import SourceOriginKind
from impodo.domain.source_snapshot import SourceSnapshot
from impodo.domain.workspace.contracts import SourceDataset, SourceSelection

_MISSING_SELECTION = (
    "Original source values are unavailable because the frozen physical source "
    "selection was not recorded."
)
_MISSING_SNAPSHOTS = (
    "Original source values are unavailable for some direct file cells because "
    "their frozen source snapshots are missing."
)
_STALE = (
    "The original source evidence does not match this review. Prepare the current "
    "data and reopen Final review before exporting."
)


@dataclass(frozen=True, slots=True)
class _InputGroup:
    finding_index: int
    dataset_id: str
    source_row: int
    target_field: str
    input_keys: tuple[str, ...]


def plan_workbook_original_values(
    *,
    fields: FieldSetAsideEvidence | None,
    selection: SourceSelection | None,
    physical_selection_hash: str,
    mappings: tuple[DatasetMapping, ...],
    sources: PreflightSourceRepository,
    workspace_id: str,
    restricted: bool,
) -> tuple[FieldSetAsideEvidence | None, tuple[OriginalSourceRequest, ...]]:
    """Bind complete eligible input groups to immutable physical source manifests.

    Replanning discards all earlier original values and geometry. Restricted
    exports return before source repository access. Missing historical evidence
    stays explicitly unavailable; contradictory bindings fail closed. No source
    value is read or artifact materialized by this planner.
    """

    if fields is None:
        return None, ()
    fields = _clear_inputs(fields, restricted=restricted)
    if restricted or not fields.findings:
        return fields, ()
    if selection is None:
        return _with_gap(fields, _MISSING_SELECTION), ()
    groups = _candidate_groups(fields, selection, mappings)
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
    physical_groups = _physical_groups(selection, physical, groups)
    if not physical_groups:
        return fields, ()
    fields = _with_proven_inputs(fields, physical_groups)
    snapshots = sources.get_current_source_snapshots(workspace_id)
    requests, missing = _bound_requests(physical, snapshots, physical_groups)
    return (_with_gap(fields, _MISSING_SNAPSHOTS) if missing else fields), requests


def _clear_inputs(
    fields: FieldSetAsideEvidence, *, restricted: bool
) -> FieldSetAsideEvidence:
    findings = tuple(
        replace(finding, source_inputs=()) if finding.source_inputs else finding
        for finding in fields.findings
    )
    if findings == fields.findings and fields.source_values_restricted == restricted:
        return fields
    return replace(fields, findings=findings, source_values_restricted=restricted)


def _candidate_groups(
    fields: FieldSetAsideEvidence,
    selection: SourceSelection,
    mappings: tuple[DatasetMapping, ...],
) -> tuple[_InputGroup, ...]:
    datasets = {dataset.dataset_id: dataset for dataset in selection.datasets}
    keys_by_dataset = {
        dataset.dataset_id: frozenset(column.stable_key for column in dataset.columns)
        for dataset in selection.datasets
    }
    if len(datasets) != len(selection.datasets):
        raise ReadinessError(_STALE)
    providers: dict[
        tuple[str, str], list[tuple[SourceDataset, tuple[str, ...], bool]]
    ] = {}
    for mapping in mappings:
        dataset = datasets.get(mapping.dataset_id)
        if (
            dataset is None
            or dataset.origin is not SourceOriginKind.FILE
            or fields.target_models.get(dataset.name) != mapping.target_model
        ):
            continue
        for field in mapping.fields:
            if _is_direct_scalar(field) and field.source_column_key is not None:
                providers.setdefault((dataset.name, field.target_field), []).append(
                    (dataset, (field.source_column_key,), False)
                )
        for relationship in mapping.relationships:
            input_keys = _relationship_input_keys(relationship)
            if input_keys is not None:
                providers.setdefault(
                    (dataset.name, relationship.target_field), []
                ).append((dataset, input_keys, True))
    groups: list[_InputGroup] = []
    for finding_index, finding in enumerate(fields.findings):
        matches = providers.get((finding.dataset, finding.target_field), ())
        if finding.source_row is None or len(matches) != 1:
            continue
        dataset, input_keys, relationship_provider = matches[0]
        if finding.comparison_issue and not relationship_provider:
            continue
        if finding.source_keys != input_keys:
            continue
        if (
            type(finding.source_row) is not int
            or finding.source_row < 1
            or not set(input_keys) <= keys_by_dataset[dataset.dataset_id]
        ):
            raise ReadinessError(_STALE)
        groups.append(
            _InputGroup(
                finding_index,
                dataset.dataset_id,
                finding.source_row,
                finding.target_field,
                input_keys,
            )
        )
    return tuple(groups)


def _is_direct_scalar(field: ScalarFieldMapping) -> bool:
    return (
        field.value_source
        in {ScalarValueSource.SOURCE, ScalarValueSource.SOURCE_WITH_FALLBACK}
        and field.concatenation is None
        and field.selection_rules is None
        and field.reference_lookup is None
        and not field.transform.formula.strip()
    )


def _relationship_input_keys(
    relationship: RelationshipMapping,
) -> tuple[str, ...] | None:
    """Prove direct single inputs or distinct ordered key/scope components."""

    resolver = relationship.resolver
    keys = relationship.source_column_keys
    if (
        relationship.value_source is not RelationshipValueSource.SOURCE
        or relationship.kind not in {"many2one", "many2many"}
        or not keys
        or relationship.constant_reference is not None
        or resolver.dataset_projection_field is not None
        or relationship_input_columns(relationship) != keys
    ):
        return None
    components = (*resolver.key_mappings, *resolver.scope_mappings)
    if any(component.preparation is not None for component in components):
        return None
    if not resolver.scope_mappings:
        return (
            keys
            if len(keys) == 1
            and all(component.source_column_key == keys[0] for component in components)
            else None
        )
    if (
        relationship.kind != "many2one"
        or not resolver.key_mappings
        or resolver.origin
        not in {ResolverOrigin.TARGET_CATALOG, ResolverOrigin.TARGET_THEN_DATASET}
        or resolver.value_mappings
        or tuple(component.source_column_key for component in components) != keys
        or len(set(keys)) != len(keys)
        or len({component.target_field for component in components}) != len(components)
    ):
        return None
    return keys


def _physical_groups(
    effective: SourceSelection,
    physical: SourceSelection,
    groups: tuple[_InputGroup, ...],
) -> tuple[_InputGroup, ...]:
    """Preserve whole component groups while excluding generated source inputs."""

    effective_datasets = {dataset.dataset_id: dataset for dataset in effective.datasets}
    physical_datasets = {dataset.dataset_id: dataset for dataset in physical.datasets}
    if len(physical_datasets) != len(physical.datasets):
        raise ReadinessError(_STALE)
    physical_keys_by_dataset: dict[str, frozenset[str]] = {}
    for dataset_id in {group.dataset_id for group in groups}:
        dataset = physical_datasets.get(dataset_id)
        effective_dataset = effective_datasets[dataset_id]
        if (
            dataset is None
            or replace(effective_dataset, columns=dataset.columns) != dataset
            or effective_dataset.columns[: len(dataset.columns)] != dataset.columns
            or len({column.stable_key for column in effective_dataset.columns})
            != len(effective_dataset.columns)
        ):
            raise ReadinessError(_STALE)
        physical_keys_by_dataset[dataset_id] = frozenset(
            column.stable_key for column in dataset.columns
        )
    return tuple(
        group
        for group in groups
        if set(group.input_keys) <= physical_keys_by_dataset[group.dataset_id]
    )


def _with_proven_inputs(
    fields: FieldSetAsideEvidence,
    groups: tuple[_InputGroup, ...],
) -> FieldSetAsideEvidence:
    inputs_by_finding = {
        group.finding_index: tuple(
            OriginalSourceInput(key, OriginalSourceValue.unavailable())
            for key in group.input_keys
        )
        for group in groups
    }
    return replace(
        fields,
        findings=tuple(
            replace(finding, source_inputs=inputs_by_finding[index])
            if index in inputs_by_finding
            else finding
            for index, finding in enumerate(fields.findings)
        ),
    )


def _bound_requests(
    physical: SourceSelection,
    snapshots: tuple[SourceSnapshot, ...],
    groups: tuple[_InputGroup, ...],
) -> tuple[tuple[OriginalSourceRequest, ...], bool]:
    physical_datasets = {dataset.dataset_id: dataset for dataset in physical.datasets}
    snapshot_by_dataset = {snapshot.dataset_id: snapshot for snapshot in snapshots}
    if len(snapshot_by_dataset) != len(snapshots):
        raise ReadinessError(_STALE)
    targets_by_dataset: dict[str, set[OriginalSourceTarget]] = {}
    for group in groups:
        targets_by_dataset.setdefault(group.dataset_id, set()).update(
            OriginalSourceTarget(
                group.source_row, key, group.target_field, group.input_keys
            )
            for key in group.input_keys
        )
    requests: list[OriginalSourceRequest] = []
    missing = False
    for dataset_id, targets in sorted(targets_by_dataset.items()):
        dataset = physical_datasets[dataset_id]
        snapshot = snapshot_by_dataset.get(dataset_id)
        if snapshot is None:
            missing = True
            continue
        try:
            validate_snapshot_for_dataset(physical, dataset, snapshot)
        except SourceLoadError as error:
            raise ReadinessError(_STALE) from error
        requests.append(
            OriginalSourceRequest(
                snapshot=snapshot,
                dataset=dataset.name,
                cells=tuple(
                    sorted(
                        {(target.source_row, target.source_key) for target in targets}
                    )
                ),
                targets=tuple(
                    sorted(
                        targets,
                        key=lambda target: (
                            target.source_row,
                            target.source_key,
                            target.target_field,
                            target.input_keys,
                        ),
                    )
                ),
            )
        )
    return tuple(requests), missing


def _with_gap(fields: FieldSetAsideEvidence, gap: str) -> FieldSetAsideEvidence:
    return replace(
        fields, coverage_gaps=tuple(dict.fromkeys((*fields.coverage_gaps, gap)))
    )
