"""Validation of declared business control totals."""

from __future__ import annotations

from ..contracts import DatasetMapping
from .common import _issue
from .context import ValidationContext
from .evidence import MappingValidationIssue


def _validate_control_totals(
    context: ValidationContext,
    dataset: DatasetMapping,
    base: str,
    issues: list[MappingValidationIssue],
) -> None:
    fields = context.fields_by_model[dataset.target_model]
    scalar_by_target = {
        item.target_field: item for item in dataset.fields
    }
    seen_control_fields: set[str] = set()
    for control_index, control in enumerate(dataset.control_totals):
        path = f"{base}/control_totals/{control_index}"
        scalar = scalar_by_target.get(control.target_field)
        metadata = fields.get(control.target_field)
        if scalar is None:
            issues.append(
                _issue(
                    "MAPPING_CONTROL_TOTAL_FIELD_UNMAPPED",
                    f"{path}/target_field",
                    "The totals check uses an Odoo field that is not mapped.",
                    "Map that numeric field or remove the totals check.",
                    dataset=dataset,
                    target_field=control.target_field,
                )
            )
        elif scalar.value_type not in {"integer", "decimal"}:
            issues.append(
                _issue(
                    "MAPPING_CONTROL_TOTAL_FIELD_NOT_NUMERIC",
                    f"{path}/target_field",
                    "The totals check requires a mapped number or amount field.",
                    "Choose a mapped numeric Odoo field.",
                    dataset=dataset,
                    target_field=control.target_field,
                )
            )
        elif metadata is None or metadata.type not in {
            "integer",
            "float",
            "monetary",
        }:
            issues.append(
                _issue(
                    "MAPPING_CONTROL_TOTAL_TARGET_NOT_NUMERIC",
                    f"{path}/target_field",
                    "The Odoo field selected for this total is not numeric.",
                    "Choose a number, quantity, or amount field.",
                    dataset=dataset,
                    target_field=control.target_field,
                )
            )
        if control.target_field in seen_control_fields:
            issues.append(
                _issue(
                    "MAPPING_CONTROL_TOTAL_DUPLICATE",
                    f"{path}/target_field",
                    "The same field has more than one totals check.",
                    "Keep one named expected total for this field.",
                    dataset=dataset,
                    target_field=control.target_field,
                )
            )
        seen_control_fields.add(control.target_field)

