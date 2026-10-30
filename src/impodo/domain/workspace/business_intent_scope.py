"""Derive a bounded intent ceiling from existing checked mapping semantics.

Compilation reuses the runtime field/default and resolver rules. An expectation
may narrow this ceiling, but it cannot select a new mode, field, or operation.
No source values, Odoo records, connection, assessment, or approval is retained.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from impodo.domain.compiler.browser_mapping_compiler import compile_browser_mapping
from impodo.domain.mapping.contracts import MappingDefinition, MappingTargetMode
from impodo.domain.mapping.create_field_policy import (
    CREATE_DEFAULT_DECISION_POLICY_VERSION,
)
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation
from impodo.domain.odoo_source_policy import (
    ProductionWriteDisposition,
    is_pinned_write_field,
    odoo_source_policy,
    odoo_source_policy_hash,
)
from impodo.domain.serialization import content_hash
from impodo.domain.source_binding import SourceOriginKind
from impodo.domain.workspace.business_intent import (
    MAX_INTENT_MODELS,
    BusinessIntent,
    BusinessIntentError,
    BusinessRelationshipOperation,
    BusinessRelationshipRule,
)
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SourceSelection
from impodo.domain.workspace.workbench import SourceMode


@dataclass(frozen=True, slots=True)
class SupportedModelWrites:
    """Describe only explicit fields and authored relationship effects in scope."""

    model: str
    creates: bool
    updates: bool
    create_fields: tuple[str, ...]
    update_fields: tuple[str, ...]
    create_relationships: tuple[BusinessRelationshipRule, ...]
    update_relationships: tuple[BusinessRelationshipRule, ...]


@dataclass(frozen=True, slots=True)
class SupportedBusinessScope:
    """Keep the qualified operation and exact technical ceiling reviewable."""

    source_mode: str
    odoo_version: str
    operation: str
    models: tuple[SupportedModelWrites, ...]
    source_policy_hash: str | None
    contract_version: int = 1
    create_field_policy_version: int = CREATE_DEFAULT_DECISION_POLICY_VERSION

    @property
    def content_hash(self) -> str:
        return content_hash(asdict(self))

    def require_narrowing(self, intent: BusinessIntent) -> None:
        """Reject requested authority outside the current checked write path."""
        models = {item.model: item for item in self.models}
        for rule in intent.models:
            ceiling = models.get(rule.model)
            if ceiling is None:
                raise BusinessIntentError(
                    f"{rule.model} is outside the checked business scope"
                )
            if (rule.mode.creates and not ceiling.creates) or (
                rule.mode.updates and not ceiling.updates
            ):
                raise BusinessIntentError(
                    f"The requested write mode is unavailable for {rule.model}"
                )
            for fields, allowed, relationships, allowed_relationships in (
                (
                    rule.create_fields,
                    ceiling.create_fields,
                    rule.create_relationships,
                    ceiling.create_relationships,
                ),
                (
                    rule.update_fields,
                    ceiling.update_fields,
                    rule.update_relationships,
                    ceiling.update_relationships,
                ),
            ):
                if fields is not None and not set(fields).issubset(allowed):
                    raise BusinessIntentError(
                        f"Business field limits exceed the checked write scope for {rule.model}"
                    )
                by_field = {
                    item.field: set(item.operations) for item in allowed_relationships
                }
                if relationships is not None and any(
                    item.field not in by_field
                    or not set(item.operations).issubset(by_field[item.field])
                    for item in relationships
                ):
                    raise BusinessIntentError(
                        f"Business relationship operations exceed the checked write scope for {rule.model}"
                    )


def derive_business_intent_scope(
    definition: MappingDefinition,
    selection: SourceSelection,
    schema: OdooSchemaCatalog,
    *,
    source_mode: SourceMode,
    operation: OdooOperation,
) -> SupportedBusinessScope:
    """Compile metadata once, then aggregate each model without per-field reads."""
    if operation not in {OdooOperation.WRITE, OdooOperation.PRODUCTION}:
        raise BusinessIntentError(
            "Business intent requires a qualified write operation"
        )
    if len({item.target_model for item in definition.datasets}) > MAX_INTENT_MODELS:
        raise BusinessIntentError("The business scope exceeds its model limit")
    origins = {item.dataset_id: item.origin for item in selection.datasets}
    pinned = source_mode is SourceMode.ODOO
    for item in definition.datasets:
        if (
            item.dataset_id not in origins
            or (origins[item.dataset_id] is SourceOriginKind.ODOO) != pinned
        ):
            raise BusinessIntentError(
                "The checked mapping does not match the current source mode"
            )
        if (item.mode is MappingTargetMode.ODOO_PINNED_UPDATE) != pinned:
            raise BusinessIntentError(
                "The mapping mode cannot enable this source write path"
            )
    decision = assess_odoo_operation(schema.odoo_version, operation)
    policy = odoo_source_policy(decision.version.major or -1) if pinned else None
    qualified = decision.allowed and not (
        pinned
        and operation is OdooOperation.PRODUCTION
        and (
            policy is None
            or policy.production_write_disposition
            is ProductionWriteDisposition.PRODUCTION_WRITE_UNSUPPORTED
        )
    )
    plan = compile_browser_mapping(definition, selection)
    models = {
        item.name: {field.name: field for field in item.fields}
        for item in schema.models
    }
    scalars: dict[tuple[str, str], set[str]] = {}
    relations: dict[tuple[str, str], dict[str, set[BusinessRelationshipOperation]]] = {}
    modes: dict[str, list[bool]] = {}
    compiled = {item.name: item for item in plan.datasets}
    source_names = {item.dataset_id: item.name for item in selection.datasets}
    for mapping in definition.datasets:
        dataset = compiled[source_names[mapping.dataset_id]]
        create = qualified and mapping.mode in {
            MappingTargetMode.CREATE,
            MappingTargetMode.UPSERT,
        }
        update = qualified and mapping.mode in {
            MappingTargetMode.UPSERT,
            MappingTargetMode.ODOO_PINNED_UPDATE,
        }
        allowed = modes.setdefault(mapping.target_model, [False, False])
        allowed[0] |= create
        allowed[1] |= update
        fields = models.get(mapping.target_model)
        if fields is None:
            raise BusinessIntentError(
                "Capture the checked record type before reviewing business intent"
            )
        for phase, enabled in (("create", create), ("update", update)):
            if not enabled:
                continue
            identity = (
                {
                    name
                    for component in (
                        *dataset.target_identity.components,
                        *dataset.target_identity.scope,
                    )
                    for name in component.target_fields
                }
                if phase == "create"
                else set()
            )
            scalar_names = {
                name
                for name, field in dataset.fields.items()
                if not field.validate_only and (phase == "create" or field.compare)
            }
            relationship_ops = {
                name: {BusinessRelationshipOperation(field.operation)}
                for name, field in dataset.relations.items()
                if not field.validate_only and (phase == "create" or field.compare)
            }
            for name in identity:
                metadata = fields.get(name)
                if metadata is not None and metadata.type in {
                    "many2one",
                    "many2many",
                    "one2many",
                }:
                    relationship_ops.setdefault(name, set()).add(
                        BusinessRelationshipOperation.REPLACE
                    )
                else:
                    scalar_names.add(name)
            for name in (*scalar_names, *relationship_ops):
                metadata = fields.get(name)
                if (
                    metadata is None
                    or metadata.readonly
                    or (
                        pinned
                        and (
                            name not in mapping.approved_write_fields
                            or not is_pinned_write_field(metadata)
                        )
                    )
                ):
                    raise BusinessIntentError(
                        "The checked field is outside the captured writable scope"
                    )
            scalars.setdefault((mapping.target_model, phase), set()).update(
                scalar_names
            )
            merged = relations.setdefault((mapping.target_model, phase), {})
            for name, operations in relationship_ops.items():
                merged.setdefault(name, set()).update(operations)

    def relationship_rules(
        model: str, phase: str
    ) -> tuple[BusinessRelationshipRule, ...]:
        return tuple(
            BusinessRelationshipRule(name, tuple(sorted(operations)))
            for name, operations in sorted(relations.get((model, phase), {}).items())
        )

    return SupportedBusinessScope(
        source_mode.value,
        schema.odoo_version,
        operation.value,
        tuple(
            SupportedModelWrites(
                model,
                modes[model][0],
                modes[model][1],
                tuple(sorted(scalars.get((model, "create"), ()))),
                tuple(sorted(scalars.get((model, "update"), ()))),
                relationship_rules(model, "create"),
                relationship_rules(model, "update"),
            )
            for model in sorted(modes)
        ),
        odoo_source_policy_hash(decision.version.major or -1) if pinned else None,
    )
