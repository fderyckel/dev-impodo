"""Check physical incoming parent keys before confirming Match data.

Layer: application. Project included source keys in one narrow scan per table;
leave generated-table and Odoo resolution to their existing runtime owners.
See docs/developer/workflow/03-match-data.md and
tests/integration/web/test_same_table_hierarchy.py.
"""

from __future__ import annotations

from collections import Counter

from impodo.domain.mapping.contracts import (
    IdentityNullPolicy,
    MappingDefinition,
    ResolverOrigin,
)
from impodo.domain.mapping.validation.evidence import MappingValidationIssue
from impodo.domain.workspace.contracts import SourceSelection
from impodo.domain.workspace.errors import WorkspaceError

from .categorical_coverage import CategoricalCoverageService


def incoming_parent_issues(
    coverage: CategoricalCoverageService,
    workspace_id: str,
    definition: MappingDefinition,
    selection: SourceSelection,
) -> tuple[MappingValidationIssue, ...]:
    """Reject missing physical scope parents without guessing root markers."""

    physical = coverage.sources.get_source_selection(workspace_id)
    if physical is None:
        return ()
    physical_ids = {source.dataset_id for source in physical.datasets}
    mappings = {dataset.dataset_id: dataset for dataset in definition.datasets}
    names = {source.dataset_id: source.name for source in selection.datasets}
    checks = [
        (dataset_index, dataset, scope_index, component, component.resolver.dataset_id)
        for dataset_index, dataset in enumerate(definition.datasets)
        for scope_index, component in enumerate(dataset.target_scope)
        if component.resolver is not None
        and component.resolver.origin is ResolverOrigin.DATASET
        and dataset.dataset_id in physical_ids
        and component.resolver.dataset_id in physical_ids
        and component.resolver.dataset_id in mappings
        and mappings[component.resolver.dataset_id].source_identity_column_keys
    ]
    columns: dict[str, list[str]] = {}
    for _, dataset, _, component, parent_id in checks:
        if parent_id is None:
            raise WorkspaceError("Choose the incoming parent table")
        for dataset_id, keys in (
            (dataset.dataset_id, component.source_column_keys),
            (parent_id, mappings[parent_id].source_identity_column_keys),
        ):
            selected = columns.setdefault(dataset_id, [])
            selected.extend(key for key in keys if key not in selected)
    rows = {
        dataset_id: coverage.source_included_key_tuples(
            workspace_id,
            dataset_id,
            keys,
            mappings[dataset_id].row_inclusion,
        )
        for dataset_id, keys in sorted(columns.items())
    }

    def keys_for(dataset_id: str, selected: tuple[str, ...]):
        indexes = tuple(columns[dataset_id].index(key) for key in selected)
        return tuple(tuple(row[index] for index in indexes) for row in rows[dataset_id])

    issues: list[MappingValidationIssue] = []
    for dataset_index, dataset, scope_index, component, parent_id in checks:
        if parent_id is None:
            raise WorkspaceError("Choose the incoming parent table")
        parents = set(
            keys_for(parent_id, mappings[parent_id].source_identity_column_keys)
        )
        references = Counter(keys_for(dataset.dataset_id, component.source_column_keys))
        missing = {
            key: count
            for key, count in references.items()
            if key not in parents
            and not (
                component.null_policy is not IdentityNullPolicy.REJECT
                and all(value is None for value in key)
            )
            and not (
                component.null_policy is IdentityNullPolicy.ZERO_AS_SCOPE_NULL
                and key == ("0",)
            )
        }
        if not missing:
            continue
        zero_root = (
            dataset.dataset_id == parent_id
            and component.null_policy is IdentityNullPolicy.EXPLICIT_SCOPE_NULL
            and ("0",) in missing
        )
        issues.append(
            MappingValidationIssue(
                code="MAPPING_INCOMING_PARENT_MISSING",
                severity="error",
                path=f"/datasets/{dataset_index}/target_scope/{scope_index}/resolver/dataset_id",
                message=(
                    f"{sum(missing.values())} included record(s) refer to a parent "
                    f"missing from {names[parent_id]}."
                    + (" A parent value of 0 is not a blank root." if zero_root else "")
                ),
                remediation=(
                    "If 0 marks your root, select 'A parent value of 0 means no parent'. "
                    "Otherwise include the missing parent or correct its source reference."
                    if zero_root
                    else "Include the missing parent rows or correct the parent references, then check matches again."
                ),
                dataset_id=dataset.dataset_id,
                target_model=dataset.target_model,
                target_field=component.target_fields[0],
            )
        )
    return tuple(issues)
