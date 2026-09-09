"""Project mapped Odoo defaults through the shared target-adaptation policy."""

from __future__ import annotations

from dataclasses import dataclass

from impodo.domain.mapping.contracts import ScalarValueSource, TargetFieldHandling
from impodo.domain.mapping.create_field_policy import (
    VerifiedCreateDefaultAction,
    VerifiedCreateDefaultDecision,
    decide_verified_create_default,
)
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SchemaField


@dataclass(frozen=True, slots=True)
class MappedTargetDefault:
    """One mapping-owned omission backed by exact target default evidence."""

    model: str
    field: SchemaField
    decision: VerifiedCreateDefaultDecision

    @property
    def key(self) -> tuple[str, str]:
        return self.model, self.field.name


def mapped_target_defaults(
    definition,
    schema: OdooSchemaCatalog,
    *,
    action: VerifiedCreateDefaultAction | None = None,
) -> tuple[MappedTargetDefault, ...]:
    """Return verified mapped defaults, optionally restricted by policy action."""

    fields_by_model = {
        model.name: {field.name: field for field in model.fields}
        for model in schema.models
    }
    keys = {
        (dataset.target_model, field.target_field)
        for dataset in definition.datasets
        for field in dataset.fields
        if field.value_source is ScalarValueSource.ODOO_DEFAULT
    }
    keys.update(
        (dataset.target_model, disposition.target_field)
        for dataset in definition.datasets
        for disposition in dataset.target_field_dispositions
        if disposition.handling is TargetFieldHandling.ODOO_DEFAULT
    )
    result = []
    for model_name, field_name in sorted(keys):
        field = fields_by_model.get(model_name, {}).get(field_name)
        if field is None or not field.required or not field.create_default_present:
            continue
        try:
            decision = decide_verified_create_default(field)
        except ValueError:
            continue
        if action is None or decision.action is action:
            result.append(MappedTargetDefault(model_name, field, decision))
    return tuple(result)
