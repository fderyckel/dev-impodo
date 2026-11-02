"""Bind reviewed selection choices to exact schema and mapping evidence.

Refresh candidates do not change current evidence. Accepting one changes only
selection codes and labels; field behavior and the verified reader stay fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SchemaOrigin
from impodo.domain.workspace.errors import WorkspaceError


@dataclass(frozen=True, slots=True)
class SelectionChoiceCandidate:
    """One protected, target-bound choice list awaiting an explicit save."""

    workspace_id: str
    schema_hash: str
    governance_hash: str
    access_binding: tuple[str, ...]
    source_hash: str
    working_version: int | None
    workspace_revision: int
    model: str
    field: str
    choices: tuple[tuple[str, str], ...]
    checked_at: datetime


@dataclass(frozen=True, slots=True)
class SelectionSchemaRefresh:
    """An exact schema transition published in the mapping transaction."""

    previous_schema: OdooSchemaCatalog
    previous_governance: SchemaGovernance | None
    schema: OdooSchemaCatalog
    governance: SchemaGovernance | None
    workspace_revision: int
    working_version: int | None

    @property
    def changed(self) -> bool:
        return self.schema.content_hash != self.previous_schema.content_hash


def selection_schema_refresh(
    schema: OdooSchemaCatalog,
    governance: SchemaGovernance | None,
    candidates: tuple[SelectionChoiceCandidate, ...],
    *,
    source_hash: str,
    working_version: int | None,
    workspace_revision: int,
    actor_name: str,
) -> SelectionSchemaRefresh:
    """Revalidate narrow choice changes and rebind unchanged governed keys."""

    if (
        not candidates
        or schema.origin is not SchemaOrigin.LIVE_API
        or schema.pending_refresh
    ):
        raise WorkspaceError("Review the current Odoo fields before refreshing choices")
    fields = {
        (model.name, field.name): field
        for model in schema.models
        for field in model.fields
    }
    updates: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}
    for candidate in candidates:
        key = candidate.model, candidate.field
        field = fields.get(key)
        if (
            candidate.workspace_id != schema.workspace_id
            or candidate.schema_hash != schema.content_hash
            or candidate.governance_hash
            != (governance.content_hash if governance else "")
            or candidate.access_binding != selection_access_binding(schema)
            or candidate.source_hash != source_hash
            or candidate.working_version != working_version
            or candidate.workspace_revision != workspace_revision
            or field is None
            or field.type != "selection"
            or key in updates
        ):
            raise WorkspaceError(
                "The matching inputs changed. Refresh the Odoo choices again."
            )
        codes = [code for code, _label in candidate.choices]
        if (
            len(codes) > 2000
            or len(codes) != len(set(codes))
            or any(not code for code in codes)
        ):
            raise WorkspaceError("Odoo returned an invalid or oversized choice list")
        updates[key] = candidate.choices
    changed_fields = [
        (model, name, sorted(code for code, _label in choices))
        for (model, name), choices in sorted(updates.items())
        if {code for code, _label in fields[model, name].selection}
        != {code for code, _label in choices}
    ]
    checked_at = max(candidate.checked_at for candidate in candidates)
    updated = replace(
        schema,
        models=tuple(
            replace(
                model,
                fields=tuple(
                    replace(
                        field,
                        selection=updates.get(
                            (model.name, field.name), field.selection
                        ),
                    )
                    for field in model.fields
                ),
            )
            for model in schema.models
        ),
        content_hash=content_hash(
            {
                "previous_schema": schema.content_hash,
                "selection_choices": changed_fields,
            }
        )
        if changed_fields
        else schema.content_hash,
        last_checked_at=checked_at,
        last_checked_by=actor_name,
    )
    rebound = governance
    if governance is not None:
        if governance.catalog_hash != schema.content_hash or set(
            governance.permitted_models
        ) != {model.name for model in schema.models}:
            raise WorkspaceError("Review the current Odoo matching rules first")
        for key in governance.business_keys:
            if any(
                (key.model, name) not in fields
                for name in (*key.key_fields, *key.scope_fields)
            ):
                raise WorkspaceError("An Odoo matching rule is no longer available")
        if changed_fields:
            rebound = replace(
                governance,
                version=governance.version + 1,
                catalog_hash=updated.content_hash,
                recorded_at=checked_at,
                recorded_by=actor_name,
            )
    return SelectionSchemaRefresh(
        schema, governance, updated, rebound, workspace_revision, working_version
    )


def selection_access_binding(schema: OdooSchemaCatalog) -> tuple[str, ...]:
    """Identify the exact target, reader generation and governed read context."""
    return (
        schema.connection_target_hash,
        schema.read_credential_binding_hash,
        schema.read_principal_hash,
        schema.read_permission_hash,
        schema.read_context_hash,
    )
