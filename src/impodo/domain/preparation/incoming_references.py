"""Retain governed nested identities when source rows become business links.

Comparison and execution use this same representation. Ordinary flat target
references remain unchanged, and no numeric Odoo identity enters these values.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.mapping.source_record_identity import SOURCE_EXTERNAL_IDS_FIELD
from impodo.domain.recipe.profile import IdentityComponent
from impodo.domain.shared.models import BusinessReference, LogicalReference, ScalarValue


def incoming_business_reference(
    plan: CompiledMigrationPlan,
    dataset_name: str,
    identity: Iterable[ScalarValue | LogicalReference | BusinessReference],
    scope: Iterable[ScalarValue | LogicalReference | BusinessReference],
) -> BusinessReference:
    """Express an incoming row's key and nested scope using their governed fields."""
    dataset = plan.dataset(dataset_name)
    if dataset.source_record_identity is not None:
        values = tuple(identity)
        if len(values) != 1 or not isinstance(values[0], str):
            raise TypeError("A source record identity must contain one resolved External ID")
        return BusinessReference(
            model=dataset.target.model, key=(values[0],), scope=(),
            target_fields=(SOURCE_EXTERNAL_IDS_FIELD,),
        )

    def shaped(values, components: tuple[IdentityComponent, ...]):
        expanded = tuple(
            component for component in components for _ in component.target_fields
        )
        result: list[ScalarValue | BusinessReference] = []
        for value, component in zip(values, expanded, strict=True):
            if isinstance(value, LogicalReference):
                raise TypeError(
                    "A resolved dependency still contains a symbolic identity"
                )
            if isinstance(value, BusinessReference) and not value.target_fields:
                resolver = component.resolve
                if resolver is None:
                    raise TypeError(
                        "A nested identity reference has no governed resolver"
                    )
                if resolver.target_model is not None:
                    model, fields, scope_fields = (
                        resolver.target_model,
                        resolver.target_fields,
                        resolver.target_scope_fields,
                    )
                elif resolver.dataset is not None:
                    parent = plan.dataset(resolver.dataset)
                    model = parent.target.model
                    fields = tuple(
                        field
                        for item in parent.target_identity.components
                        for field in item.target_fields
                    )
                    scope_fields = tuple(
                        field
                        for item in parent.target_identity.scope
                        for field in item.target_fields
                    )
                else:
                    raise TypeError("A nested identity resolver is incomplete")
                if model != value.model:
                    raise ValueError(
                        "A nested identity reference has a different model"
                    )
                value = replace(
                    value, target_fields=fields, target_scope_fields=scope_fields
                )
            result.append(value)
        return tuple(result)

    return BusinessReference(
        model=dataset.target.model,
        key=shaped(identity, dataset.target_identity.components),
        scope=shaped(scope, dataset.target_identity.scope),
        target_fields=tuple(
            field
            for item in dataset.target_identity.components
            for field in item.target_fields
        ),
        target_scope_fields=tuple(
            field
            for item in dataset.target_identity.scope
            for field in item.target_fields
        ),
    )
