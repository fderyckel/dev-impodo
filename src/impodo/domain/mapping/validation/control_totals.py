"""Validation of declared business control totals."""

from __future__ import annotations

from ..contracts import BusinessControlTotal, DatasetMapping
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
    definitions_by_id = {
        item.control_id: item for item in dataset.control_definitions
    }
    expectations_by_id = {
        item.control_id: item for item in dataset.control_expectations
    }
    if len(definitions_by_id) != len(dataset.control_definitions):
        issues.append(
            _issue(
                "MAPPING_CONTROL_DEFINITION_DUPLICATE",
                f"{base}/control_definitions",
                "A control identifier is declared more than once.",
                "Keep one reusable definition per control identifier.",
                dataset=dataset,
            )
        )
    if len(expectations_by_id) != len(dataset.control_expectations):
        issues.append(
            _issue(
                "MAPPING_CONTROL_EXPECTATION_DUPLICATE",
                f"{base}/control_expectations",
                "A control expectation is declared more than once.",
                "Keep one edition expectation per control identifier.",
                dataset=dataset,
            )
        )
    for definition in dataset.control_definitions:
        if definition.control_id not in expectations_by_id:
            issues.append(
                _issue(
                    "MAPPING_CONTROL_EXPECTATION_REQUIRED",
                    f"{base}/control_expectations",
                    f"Control {definition.name!r} needs this edition's expected value.",
                    "Enter or confirm the expected value for this data edition.",
                    dataset=dataset,
                    target_field=definition.target_field,
                )
            )
    for expectation in dataset.control_expectations:
        if expectation.control_id not in definitions_by_id:
            issues.append(
                _issue(
                    "MAPPING_CONTROL_EXPECTATION_ORPHANED",
                    f"{base}/control_expectations",
                    "A control expectation has no reusable definition.",
                    "Remove it or restore the matching control definition.",
                    dataset=dataset,
                )
            )
    controls = (
        dataset.control_totals
        if dataset.control_totals
        else tuple(
            BusinessControlTotal(
                name=item.name,
                target_field=item.target_field,
                expected_total="0",
                unit=item.unit,
                tolerance=item.tolerance,
            )
            for item in dataset.control_definitions
        )
    )
    control_path = (
        "control_definitions" if dataset.control_definitions else "control_totals"
    )
    for control_index, control in enumerate(controls):
        path = f"{base}/{control_path}/{control_index}"
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
