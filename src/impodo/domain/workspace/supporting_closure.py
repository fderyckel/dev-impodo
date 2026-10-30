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
    SupportingPlanRole,
    SupportingRelationshipIntent,
    SupportingSourceDescriptor,
    plan_supporting_data,
)
from impodo.domain.workspace.supporting_review import (
    SupportingChoice,
    SupportingChoiceAction,
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
        datasets=datasets,
        source_selection_hash=source_hash,
        schema_hash=schema.content_hash,
        preparation_hash=preparation_hash,
        dataset_intents=tuple(
            SupportingDatasetIntent(item.dataset_id, item.target_model)
            for item in mappings
        ),
        relationship_intents=tuple(
            SupportingRelationshipIntent(
                item.source_model, item.field_name, item.dataset_id
            )
            for item in choices
            if item.action is not SupportingChoiceAction.SKIP_OPTIONAL
        ),
    )
    by_edge = {(item.source_model, item.field_name): item for item in choices}
    role = {
        SupportingChoiceAction.INCOMING: SupportingPlanRole.INCOMING_CANDIDATE,
        SupportingChoiceAction.REUSE: SupportingPlanRole.REUSE_EXISTING,
        SupportingChoiceAction.CHECKED_DEFAULT: SupportingPlanRole.CHECKED_DEFAULT,
        SupportingChoiceAction.ODOO_MANAGED: SupportingPlanRole.ODOO_MANAGED,
    }
    by_id = {item.dataset_id: item for item in datasets}
    edges = []
    for edge in proposal.edges:
        choice = by_edge.get((edge.source_model, edge.field_name))
        if choice and choice.action in role:
            candidates = (
                (by_id[choice.dataset_id],) if choice.dataset_id in by_id else ()
            )
            edge = replace(edge, role=role[choice.action], candidates=candidates)
        edges.append(edge)
    return ReviewedSupportingClosure(
        replace(proposal, edges=tuple(edges)), choices, recipe_hash
    )


def supporting_mapping_conflicts(
    choices: tuple[SupportingChoice, ...],
    datasets: tuple[DatasetMapping, ...],
    schema: OdooSchemaCatalog,
) -> tuple[str, ...]:
    """Check each reviewed edge against every mapping that supplies its parent."""
    fields = eligible_supporting_fields(schema)
    by_model: dict[str, list[DatasetMapping]] = {}
    by_dataset = {item.dataset_id: item for item in datasets}
    for dataset in datasets:
        by_model.setdefault(dataset.target_model, []).append(dataset)
    conflicts = set()
    for choice in choices:
        field = fields.get((choice.source_model, choice.field_name))
        if field is None:
            conflicts.add(
                f"{choice.source_model}.{choice.field_name}: Odoo details changed"
            )
            continue
        for dataset in by_model.get(choice.source_model, ()):
            providers = [
                (item.resolver, *relationship_target_fields(item))
                for item in dataset.relationships
                if item.target_field == choice.field_name
            ]
            providers.extend(
                (
                    item.resolver,
                    tuple(part.target_field for part in item.resolver.key_mappings),
                    tuple(part.target_field for part in item.resolver.scope_mappings),
                )
                for item in (*dataset.target_identity, *dataset.target_scope)
                if item.target_fields == (choice.field_name,)
                and item.resolver is not None
            )
            scalar = any(
                item.target_field == choice.field_name for item in dataset.fields
            )
            inverse_used = field.type == "one2many" and any(
                item.target_field == field.relation_field
                and item.resolver.dataset_id == dataset.dataset_id
                for related in by_model.get(field.relation or "", ())
                for item in related.relationships
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
                        and key == choice.key_fields
                        and scope == choice.scope_fields
                        for resolver, key, scope in providers
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
                        and any(
                            item.target_field == field.relation_field
                            and item.resolver.dataset_id == dataset.dataset_id
                            and item.resolver.origin
                            in {
                                ResolverOrigin.DATASET,
                                ResolverOrigin.TARGET_THEN_DATASET,
                            }
                            for item in related.relationships
                        )
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
                            for resolver, _key, _scope in providers
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
                    and supporting_model_role(field) is SupportingModelRole.ODOO_MANAGED
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
                    f"{choice.source_model}.{choice.field_name}: saved {action.value.lower()} choice conflicts with Match data"
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
