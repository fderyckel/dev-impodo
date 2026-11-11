"""Reconcile reviewed supporting meaning with portable and physical mappings.

These pure checks preserve resolver precedence. They report a contradiction
instead of rewriting a relationship, promoting write scope, or reading Odoo.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace

from impodo.domain.mapping.contracts import (
    DatasetMapping,
    MappingTargetMode,
    ResolverOrigin,
    TargetFieldHandling,
    relationship_target_fields,
)
from impodo.domain.serialization import content_hash
from impodo.domain.workspace.contracts import OdooSchemaCatalog
from impodo.domain.workspace.supporting_models import (
    SupportingModelRole,
    supporting_model_role,
)
from impodo.domain.workspace.supporting_plan import (
    SupportingDataPlan,
    SupportingDatasetIntent,
    SupportingRelationshipIntent,
    SupportingSourceDescriptor,
    plan_supporting_data,
)
from impodo.domain.workspace.supporting_review import (
    SupportingChoice,
    SupportingChoiceAction,
    SupportingMappingConflict,
    eligible_supporting_fields,
)


@dataclass(frozen=True, slots=True)
class ReviewedSupportingClosure:
    """Keep the bounded proposal, reviewed meaning and originating Recipe hash."""

    proposal: SupportingDataPlan
    choices: tuple[SupportingChoice, ...]
    recipe_hash: str = ""

    @property
    def content_hash(self) -> str:
        return content_hash(asdict(self))


def close_supporting_plan(
    *,
    schema: OdooSchemaCatalog,
    datasets: tuple[SupportingSourceDescriptor, ...],
    mappings: tuple[DatasetMapping, ...],
    choices: tuple[SupportingChoice, ...],
    source_hash: str,
    preparation_hash: str | None,
    recipe_hash: str = "",
) -> ReviewedSupportingClosure:
    """Merge exact incoming intent with required and explicitly used edges."""
    selected_models = {item.target_model for item in mappings}
    choices = tuple(item for item in choices if item.source_model in selected_models)
    proposal = plan_supporting_data(
        selected_models=tuple(sorted(selected_models)),
        models=schema.models,
        odoo_version=schema.odoo_version,
        datasets=datasets,
        source_selection_hash=source_hash,
        schema_hash=schema.content_hash,
        preparation_hash=preparation_hash,
        dataset_intents=tuple(
            SupportingDatasetIntent(item.dataset_id, item.target_model)
            for item in mappings
        ),
        relationship_intents=supporting_mapping_intents(mappings, schema),
        reviewed_choices=choices,
    )
    return ReviewedSupportingClosure(proposal, choices, recipe_hash)


def supporting_mapping_intents(
    mappings: tuple[DatasetMapping, ...], schema: OdooSchemaCatalog
) -> tuple[SupportingRelationshipIntent, ...]:
    """Retain actual providers and child ownership without inferring write scope."""
    return _supporting_mapping_usage(mappings, schema)[0]


def _supporting_mapping_usage(
    mappings: tuple[DatasetMapping, ...], schema: OdooSchemaCatalog
) -> tuple[tuple[SupportingRelationshipIntent, ...], frozenset[tuple[str, str, str]]]:
    """Keep the exact parent table for conflict checks across repeated models."""
    by_dataset = {item.dataset_id: item for item in mappings}
    eligible = eligible_supporting_fields(schema)
    inverses: dict[tuple[str, str, str], list[str]] = {}
    for (model_name, field_name), field in eligible.items():
        if field.type == "one2many" and field.relation and field.relation_field:
            inverses.setdefault(
                (model_name, field.relation, field.relation_field), []
            ).append(field_name)
    result = set()
    owners = set()
    for dataset in mappings:
        providers = [(item.target_field, item.resolver) for item in dataset.relationships]
        providers.extend(
            (item.target_fields[0], item.resolver)
            for item in (*dataset.target_identity, *dataset.target_scope)
            if len(item.target_fields) == 1 and item.resolver is not None
        )
        for field_name, resolver in providers:
            if (dataset.target_model, field_name) not in eligible:
                # Company-dependent and other excluded providers keep their
                # existing validator and company-policy owner.
                continue
            incoming = (
                resolver.dataset_id or ""
                if resolver.origin in {ResolverOrigin.DATASET, ResolverOrigin.TARGET_THEN_DATASET}
                else ""
            )
            result.add(SupportingRelationshipIntent(
                dataset.target_model, field_name, incoming,
                reuse_existing=resolver.origin is ResolverOrigin.TARGET_CATALOG,
            ))
            parent = by_dataset.get(incoming)
            if parent is not None and dataset.mode is not MappingTargetMode.REFERENCE:
                for inverse in inverses.get(
                    (parent.target_model, dataset.target_model, field_name), ()
                ):
                    result.add(SupportingRelationshipIntent(
                        parent.target_model, inverse, dataset.dataset_id
                    ))
                    owners.add((parent.dataset_id, inverse, dataset.dataset_id))
    return tuple(sorted(result)), frozenset(owners)


def supporting_mapping_conflicts(
    choices: tuple[SupportingChoice, ...],
    datasets: tuple[DatasetMapping, ...],
    schema: OdooSchemaCatalog,
) -> tuple[str, ...]:
    """Keep the established text contract over structured conflict identities."""
    return tuple(sorted({item.message for item in supporting_mapping_conflict_details(choices, datasets, schema)}))


def supporting_mapping_conflict_details(
    choices: tuple[SupportingChoice, ...],
    datasets: tuple[DatasetMapping, ...],
    schema: OdooSchemaCatalog,
) -> tuple[SupportingMappingConflict, ...]:
    """Check each reviewed edge against every mapping that supplies its parent."""
    fields = eligible_supporting_fields(schema)
    _intents, inverse_owners = _supporting_mapping_usage(datasets, schema)
    by_model: dict[str, list[DatasetMapping]] = {}
    by_dataset = {item.dataset_id: item for item in datasets}
    for dataset in datasets:
        by_model.setdefault(dataset.target_model, []).append(dataset)
    conflicts = set()
    for choice in choices:
        field = fields.get((choice.source_model, choice.field_name))
        if field is None:
            conflicts.add(
                SupportingMappingConflict(choice.source_model, choice.field_name, "", choice.action, True)
            )
            continue
        for dataset in by_model.get(choice.source_model, ()):
            providers = [
                (item.resolver, *relationship_target_fields(item), item.selected_matching_field)
                for item in dataset.relationships
                if item.target_field == choice.field_name
            ]
            providers.extend(
                (
                    item.resolver,
                    tuple(part.target_field for part in item.resolver.key_mappings),
                    tuple(part.target_field for part in item.resolver.scope_mappings),
                    item.selected_matching_field,
                )
                for item in (*dataset.target_identity, *dataset.target_scope)
                if item.target_fields == (choice.field_name,)
                and item.resolver is not None
            )
            scalar = any(
                item.target_field == choice.field_name for item in dataset.fields
            )
            inverse_used = field.type == "one2many" and any(
                parent_id == dataset.dataset_id and field_name == choice.field_name
                for parent_id, field_name, _child_id in inverse_owners
            )
            handling = next(
                (
                    item.handling
                    for item in dataset.target_field_dispositions
                    if item.target_field == choice.field_name
                ),
                None,
            )
            action = choice.action
            valid = not scalar
            if action is SupportingChoiceAction.REUSE:
                valid = (
                    valid
                    and handling is None
                    and bool(providers)
                    and all(
                        resolver.origin is ResolverOrigin.TARGET_CATALOG
                        and resolver.model == field.relation
                        and (
                            selected_field
                            or (key == choice.key_fields and scope == choice.scope_fields)
                        )
                        for resolver, key, scope, selected_field in providers
                    )
                )
            elif action is SupportingChoiceAction.INCOMING:
                related = by_dataset.get(choice.dataset_id)
                valid = (
                    valid
                    and handling is None
                    and related is not None
                    and (
                        related.target_model == field.relation
                        and related.mode is not MappingTargetMode.REFERENCE
                    )
                )
                if field.type == "one2many" and not providers and related is not None:
                    valid = (
                        valid
                        and bool(field.relation_field)
                        and (dataset.dataset_id, choice.field_name, choice.dataset_id)
                        in inverse_owners
                    )
                else:
                    valid = (
                        valid
                        and bool(providers)
                        and all(
                            resolver.dataset_id == choice.dataset_id
                            and resolver.model in {None, field.relation}
                            and resolver.origin
                            in {
                                ResolverOrigin.DATASET,
                                ResolverOrigin.TARGET_THEN_DATASET,
                            }
                            for resolver, _key, _scope, _selected_field in providers
                        )
                    )
            elif action is SupportingChoiceAction.CHECKED_DEFAULT:
                valid = (
                    valid
                    and not providers
                    and field.create_default_present
                    and (handling is TargetFieldHandling.ODOO_DEFAULT)
                )
            elif action is SupportingChoiceAction.ODOO_MANAGED:
                valid = (
                    valid
                    and supporting_model_role(
                        field,
                        source_model=choice.source_model,
                        odoo_version=schema.odoo_version,
                    ) is SupportingModelRole.ODOO_MANAGED
                    and not providers
                    and handling in {None, TargetFieldHandling.ODOO_MANAGED}
                )
            else:
                valid = (
                    valid
                    and not providers
                    and handling is None
                    and not field.required
                    and not inverse_used
                )
            if not valid:
                conflicts.add(
                    SupportingMappingConflict(
                        choice.source_model, choice.field_name, dataset.dataset_id, action,
                        matching_fields=(*choice.key_fields, *choice.scope_fields) if (
                            action is SupportingChoiceAction.REUSE and providers
                            and all(resolver.origin is ResolverOrigin.TARGET_CATALOG and resolver.model == field.relation
                                    for resolver, _key, _scope, _selected_field in providers)
                            and any(
                                not selected_field and (key != choice.key_fields or scope != choice.scope_fields)
                                for _resolver, key, scope, selected_field in providers
                            )
                        ) else (),
                    )
                )
    return tuple(sorted(conflicts))


def portable_supporting_choices(
    choices: tuple[SupportingChoice, ...], dataset_ids: Mapping[str, str]
) -> list[dict[str, object]]:
    """Replace physical incoming identities with existing logical Recipe IDs."""
    return [
        asdict(replace(item, dataset_id=dataset_ids[item.dataset_id]))
        if item.action is SupportingChoiceAction.INCOMING
        else asdict(item)
        for item in choices
    ]


def recipe_supporting_choices(
    contract: Mapping[str, object],
    bindings: Mapping[str, str] | None = None,
) -> tuple[SupportingChoice, ...]:
    """Read optional reviewed meaning; historical Recipes keep their old contract."""
    raw = contract.get("supporting_choices", ())
    if not isinstance(raw, (tuple, list)) or len(raw) > 1_000:
        raise ValueError("Recipe supporting choices exceed their bounded contract")
    choices = []
    for value in raw:
        if not isinstance(value, Mapping) or set(value) != {
            "source_model",
            "field_name",
            "action",
            "dataset_id",
            "key_fields",
            "scope_fields",
            "display_field",
        }:
            raise ValueError("Recipe supporting choice is invalid")
        for key in ("key_fields", "scope_fields"):
            if not isinstance(value[key], (tuple, list)) or any(
                not isinstance(item, str) for item in value[key]
            ):
                raise ValueError("Recipe supporting read scope is invalid")
        if any(
            not isinstance(value[key], str)
            for key in (
                "source_model",
                "field_name",
                "action",
                "dataset_id",
                "display_field",
            )
        ):
            raise ValueError("Recipe supporting choice identity is invalid")
        choice = SupportingChoice(
            str(value["source_model"]),
            str(value["field_name"]),
            SupportingChoiceAction(value["action"]),
            str(value["dataset_id"]),
            tuple(value["key_fields"]),
            tuple(value["scope_fields"]),
            str(value["display_field"]),
        )
        if choice.action is SupportingChoiceAction.INCOMING:
            if not choice.dataset_id.startswith("dataset:"):
                raise ValueError(
                    "Recipe incoming choice needs a logical table identity"
                )
            if bindings is not None:
                physical = bindings.get(choice.dataset_id)
                if not physical:
                    raise ValueError(
                        "Bind the Recipe's supporting incoming table first"
                    )
                choice = replace(choice, dataset_id=physical)
        choices.append(choice)
    if len({(item.source_model, item.field_name) for item in choices}) != len(choices):
        raise ValueError("Recipe supporting choices contain conflicting edges")
    return tuple(sorted(choices))
