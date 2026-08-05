"""Extracted control totals domain behavior."""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256

from ..mapping.contracts import MappingDefinition
from ...models import canonical_json_bytes
from ...source import PreparedBundle
from ...staging_contracts import CanonicalControlTotal
from ...workspace_contracts import SourceSelection
from ..errors import ReadinessError




def _evaluate_control_totals(
    definition: MappingDefinition,
    selection: SourceSelection,
    prepared: PreparedBundle,
) -> tuple[CanonicalControlTotal, ...]:
    """Evaluate only explicitly declared sums over canonical numeric values."""

    dataset_name_by_id = {
        item.dataset_id: item.name for item in selection.datasets
    }
    records_by_dataset = prepared.by_dataset()
    results: list[CanonicalControlTotal] = []
    for dataset in definition.datasets:
        dataset_name = dataset_name_by_id[dataset.dataset_id]
        records = records_by_dataset.get(dataset_name, ())
        controls = dataset.control_totals
        controls_by_field = {}
        for control in controls:
            controls_by_field.setdefault(control.target_field, control)
        actual_by_field = {
            target_field: Decimal("0") for target_field in controls_by_field
        }
        included_by_field = {target_field: 0 for target_field in controls_by_field}
        empty_by_field = {target_field: 0 for target_field in controls_by_field}
        for record in records:
            for target_field, first_control in controls_by_field.items():
                value = record.scalar_values.get(target_field)
                if value is None:
                    empty_by_field[target_field] += 1
                    continue
                if isinstance(value, bool) or not isinstance(
                    value, (int, Decimal)
                ):
                    raise ReadinessError(
                        f"The named total {first_control.name!r} did not produce "
                        "numeric prepared values"
                    )
                actual_by_field[target_field] += Decimal(value)
                included_by_field[target_field] += 1
        for control in controls:
            control_id = "sha256:" + sha256(
                canonical_json_bytes(
                    {
                        "mapping_hash": definition.content_hash,
                        "dataset": dataset_name,
                        "name": control.name,
                        "target_field": control.target_field,
                        "expected_total": control.expected_total,
                        "unit": control.unit,
                        "tolerance": control.tolerance,
                    }
                )
            ).hexdigest()
            results.append(
                CanonicalControlTotal(
                    control_id=control_id,
                    name=control.name,
                    dataset=dataset_name,
                    target_field=control.target_field,
                    expected_total=control.expected_total,
                    actual_total=format(actual_by_field[control.target_field], "f"),
                    tolerance=control.tolerance,
                    unit=control.unit,
                    included_rows=included_by_field[control.target_field],
                    empty_rows=empty_by_field[control.target_field],
                )
            )
    return tuple(sorted(results, key=lambda item: item.control_id))


evaluate_control_totals = _evaluate_control_totals
