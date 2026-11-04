"""Carry portable linked-record fallbacks into comparison and create intents."""

from impodo.domain.recipe.profile import RelationSpec
from impodo.domain.shared.models import LogicalReference


def blank_create_reference(spec: RelationSpec) -> LogicalReference | None:
    """Build the existing-record lookup for a blank create rule."""

    values = spec.blank_create_values
    if not values:
        return None
    resolve = spec.resolve
    width = len(resolve.target_fields)
    return LogicalReference(
        origin="target",
        model=resolve.target_model,
        key=values[:width],
        scope=values[width:],
        target_fields=resolve.target_fields,
        target_scope_fields=resolve.target_scope_fields,
    )
