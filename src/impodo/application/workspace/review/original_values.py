"""Plan exact file-cell evidence for saved field omissions without reading values.

Only a direct scalar provider proves one physical source cell. Derived and
composed providers keep unavailable coverage until their lineage can prove the
same relationship. Artifact adapters hydrate these bounded requests later.
"""

from __future__ import annotations

from dataclasses import replace

from impodo.application.data_version.source_snapshots import (
    validate_snapshot_for_dataset,
)
from impodo.application.workspace.preparation.readiness_ports import (
    PreflightSourceRepository,
)
from impodo.domain.errors import ReadinessError
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    ScalarFieldMapping,
    ScalarValueSource,
)
from impodo.domain.preflight.fields_set_aside import FieldSetAsideEvidence
from impodo.domain.preflight.original_values import (
    OriginalSourceRequest,
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
    """Bind eligible omission coordinates to immutable physical source manifests.

    Restricted exports return before any source repository access. Historical
    missing evidence stays explicitly unavailable; contradictory bindings fail
    closed. This planner never materializes an artifact or reads source values.
    """

    if fields is None:
        return None, ()
    if restricted:
        return replace(
            fields,
            findings=tuple(
                replace(finding, source_value=OriginalSourceValue.withheld())
                if finding.source_row is not None
                else finding
                for finding in fields.findings
            ),
            source_values_restricted=True,
        ), ()
    if not fields.findings:
        return fields, ()
    if selection is None:
        return _with_gap(fields, _MISSING_SELECTION), ()
    candidates = _candidate_cells(fields, selection, mappings)
    if not candidates:
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
    snapshots = sources.get_current_source_snapshots(workspace_id)
    requests, missing = _bound_requests(selection, physical, snapshots, candidates)
    return (_with_gap(fields, _MISSING_SNAPSHOTS) if missing else fields), requests


def _candidate_cells(
    fields: FieldSetAsideEvidence,
    selection: SourceSelection,
    mappings: tuple[DatasetMapping, ...],
) -> dict[str, set[tuple[int, str, str]]]:
    datasets = {dataset.dataset_id: dataset for dataset in selection.datasets}
    keys_by_dataset = {
        dataset.dataset_id: frozenset(column.stable_key for column in dataset.columns)
        for dataset in selection.datasets
    }
    if len(datasets) != len(selection.datasets):
        raise ReadinessError(_STALE)
    providers: dict[tuple[str, str], list[tuple[SourceDataset, str]]] = {}
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
                    (dataset, field.source_column_key)
                )
    candidates: dict[str, set[tuple[int, str, str]]] = {}
    for finding in fields.findings:
        matches = providers.get((finding.dataset, finding.target_field), ())
        if finding.source_row is None or len(matches) != 1:
            continue
        dataset, key = matches[0]
        if finding.source_keys != (key,):
            continue
        if (
            type(finding.source_row) is not int
            or finding.source_row < 1
            or key not in keys_by_dataset[dataset.dataset_id]
        ):
            raise ReadinessError(_STALE)
        candidates.setdefault(dataset.dataset_id, set()).add(
            (finding.source_row, key, finding.target_field)
        )
    return candidates


def _is_direct_scalar(field: ScalarFieldMapping) -> bool:
    return (
        field.value_source
        in {ScalarValueSource.SOURCE, ScalarValueSource.SOURCE_WITH_FALLBACK}
        and field.concatenation is None
        and field.selection_rules is None
        and field.reference_lookup is None
        and not field.transform.formula.strip()
    )


def _bound_requests(
    effective: SourceSelection,
    physical: SourceSelection,
    snapshots: tuple[SourceSnapshot, ...],
    candidates: dict[str, set[tuple[int, str, str]]],
) -> tuple[tuple[OriginalSourceRequest, ...], bool]:
    effective_datasets = {dataset.dataset_id: dataset for dataset in effective.datasets}
    physical_datasets = {dataset.dataset_id: dataset for dataset in physical.datasets}
    snapshot_by_dataset = {snapshot.dataset_id: snapshot for snapshot in snapshots}
    if len(physical_datasets) != len(physical.datasets) or len(
        snapshot_by_dataset
    ) != len(snapshots):
        raise ReadinessError(_STALE)
    requests: list[OriginalSourceRequest] = []
    missing = False
    for dataset_id, targets in sorted(candidates.items()):
        dataset = physical_datasets.get(dataset_id)
        if dataset is None or dataset != effective_datasets[dataset_id]:
            raise ReadinessError(_STALE)
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
                cells=tuple(sorted({(row, key) for row, key, _ in targets})),
                targets=tuple(sorted(targets)),
            )
        )
    return tuple(requests), missing


def _with_gap(fields: FieldSetAsideEvidence, gap: str) -> FieldSetAsideEvidence:
    return replace(
        fields, coverage_gaps=tuple(dict.fromkeys((*fields.coverage_gaps, gap)))
    )
