"""Derive direct supporting-model roles from captured Odoo fields.

The Stage 2 planner starts from the business models that the data manager
selected.  It examines only their captured relationship fields and never
traverses the wider Odoo model graph.  A suggestion therefore explains a
direct dependency without adding that related model to the migration write
scope.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from impodo.domain.mapping.create_field_policy import is_odoo_create_hook_field
from impodo.domain.workspace.contracts import SchemaField, SchemaModel


class SupportingModelRole(StrEnum):
    """Describe how one direct relationship can be handled safely."""

    REUSE_EXISTING = "REUSE_EXISTING"
    CHECKED_DEFAULT = "CHECKED_DEFAULT"
    ODOO_MANAGED = "ODOO_MANAGED"
    REVIEW_INCOMING = "REVIEW_INCOMING"


@dataclass(frozen=True, slots=True)
class SupportingModelDependency:
    """One direct relationship from selected business data to another model."""

    source_model: str
    source_label: str
    field_name: str
    field_label: str
    field_type: str
    relation_model: str
    required: bool
    role: SupportingModelRole


def derive_supporting_model_dependencies(
    models: Iterable[SchemaModel],
    *,
    odoo_version: str | None = None,
) -> tuple[SupportingModelDependency, ...]:
    """Return deterministic direct dependencies outside the write scope.

    Required relationships are retained because they affect create readiness.
    An inverse-owned one-to-many is retained because it exposes related child
    business data. Other optional relationships need accepted-source or Recipe
    intent, which belongs to a later planning slice; field names alone are not
    enough to infer that intent.
    """

    captured = tuple(models)
    selected_models = {model.name for model in captured}
    dependencies = (
        SupportingModelDependency(
            source_model=model.name,
            source_label=model.label,
            field_name=field.name,
            field_label=field.label,
            field_type=field.type,
            relation_model=field.relation,
            required=field.required,
            role=supporting_model_role(
                field, source_model=model.name, odoo_version=odoo_version
            ),
        )
        for model in captured
        for field in model.fields
        if _is_direct_dependency(field, selected_models)
        and field.relation is not None
    )
    return tuple(
        sorted(
            dependencies,
            key=lambda item: (
                item.role.value,
                item.relation_model,
                item.source_label.casefold(),
                item.field_label.casefold(),
                item.source_model,
                item.field_name,
            ),
        )
    )


def unavailable_required_supporting_dependencies(
    models: Iterable[SchemaModel],
    available_model_names: Iterable[str],
    *,
    odoo_version: str | None = None,
) -> tuple[SupportingModelDependency, ...]:
    """Return required reuse dependencies absent from model discovery.

    A checked default or an Odoo-managed field does not require another
    incoming record type.  A required relationship that must reuse an
    existing record does require the related model to be visible so later
    matching can prove the intended parent or lookup record.
    """

    available = frozenset(available_model_names)
    return tuple(
        dependency
        for dependency in derive_supporting_model_dependencies(
            models, odoo_version=odoo_version
        )
        if dependency.required
        and dependency.role is SupportingModelRole.REUSE_EXISTING
        and dependency.relation_model not in available
    )


def _is_direct_dependency(
    field: SchemaField,
    selected_models: set[str],
) -> bool:
    return bool(
        field.type in {"many2one", "many2many", "one2many"}
        and field.relation
        and field.relation not in selected_models
        and field.exportable is not False
        and field.company_dependent is not True
        and (
            field.required
            or (field.type == "one2many" and bool(field.relation_field))
        )
    )


def supporting_model_role(
    field: SchemaField,
    *,
    source_model: str | None = None,
    odoo_version: str | None = None,
) -> SupportingModelRole:
    """Classify captured behavior and qualified, shared Odoo create hooks."""

    if is_odoo_create_hook_field(
        field, target_model=source_model, odoo_version=odoo_version
    ):
        return SupportingModelRole.ODOO_MANAGED
    if field.related is True:
        return SupportingModelRole.ODOO_MANAGED
    if field.computed is True and field.has_inverse is not True:
        return SupportingModelRole.ODOO_MANAGED
    if field.type == "one2many":
        return (
            SupportingModelRole.REVIEW_INCOMING
            if field.relation_field
            else SupportingModelRole.ODOO_MANAGED
        )
    if field.readonly and field.has_inverse is not True:
        return SupportingModelRole.ODOO_MANAGED
    if field.required and field.create_default_present:
        return SupportingModelRole.CHECKED_DEFAULT
    return SupportingModelRole.REUSE_EXISTING


__all__ = [
    "SupportingModelDependency",
    "SupportingModelRole",
    "derive_supporting_model_dependencies",
    "supporting_model_role",
    "unavailable_required_supporting_dependencies",
]
