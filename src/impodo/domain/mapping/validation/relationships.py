"""Relationship and business-key resolver validation."""

from __future__ import annotations

from typing import Mapping

from impodo.domain.workspace.reference_keys import (
    GovernedReferenceRequest,
    ReferencePolicyDenial,
    ReferenceReadPurpose,
    authorize_governed_reference,
    captured_reference_field_contracts,
)
from ..contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    RelationshipMapping,
    RelationshipResolver,
    RelationshipValueSource,
    ResolverOrigin,
    relationship_target_fields,
)
from .common import (
    _NULL_POLICIES,
    _check_column,
    _issue,
    _target_unknown,
)
from .context import SourceColumnView, ValidationContext
from .evidence import MappingValidationIssue


def _validate_relationship(
    context: ValidationContext,
    dataset: DatasetMapping,
    relation: RelationshipMapping,
    path: str,
    columns: Mapping[str, SourceColumnView],
    issues: list[MappingValidationIssue],
) -> None:
    fields = context.fields_by_model[dataset.target_model]
    source_provider = relation.value_source is RelationshipValueSource.SOURCE
    if source_provider and not relation.source_column_keys:
        issues.append(
            _issue(
                "MAPPING_REFERENCE_KEY_INVALID",
                path,
                "A relationship requires source reference data.",
                "Choose the source key column or list column.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    if source_provider:
        for column in relation.source_column_keys:
            _check_column(dataset, column, path, columns, issues)
    metadata = fields.get(relation.target_field)
    if metadata is None:
        issues.append(_target_unknown(dataset, path, relation.target_field))
        return
    if metadata.type == "one2many":
        inverse = (
            f" through inverse {metadata.relation_field}"
            if getattr(metadata, "relation_field", None)
            else ""
        )
        issues.append(
            _issue(
                "MAPPING_ONE2MANY_OWNER_INVALID",
                path,
                f"{relation.target_field} is one2many{inverse}.",
                "Map a child dataset to the inverse many2one field.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    if metadata.type != relation.kind:
        issues.append(
            _issue(
                "MAPPING_RELATION_KIND_INCORRECT",
                path,
                (
                    f"{relation.target_field} is {metadata.type}, "
                    f"not {relation.kind}."
                ),
                "Use the relation kind captured from Odoo.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    if metadata.readonly and not relation.validate_only:
        issues.append(
            _issue(
                "MAPPING_TARGET_FIELD_READONLY",
                path,
                f"{relation.target_field} is readonly.",
                "Remove it or mark it validate-only.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    if relation.validate_only and relation.compare:
        issues.append(
            _issue(
                "MAPPING_RELATION_POLICY_UNSAFE",
                path,
                "A validate-only relation cannot also be compared.",
                "Disable comparison or validate-only.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    if (
        relation.compare or relation.required or relation.required_on_create
    ) and relation.on_missing != "error":
        issues.append(
            _issue(
                "MAPPING_RELATION_POLICY_UNSAFE",
                path,
                "A compared or required relation must fail when missing.",
                "Use on_missing: error.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    if relation.compare and relation.on_ambiguous != "error":
        issues.append(
            _issue(
                "MAPPING_RELATION_POLICY_UNSAFE",
                path,
                "A compared relation must fail when ambiguous.",
                "Use on_ambiguous: error.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    if relation.kind == "many2one" and relation.operation != "replace":
        issues.append(
            _issue(
                "MAPPING_RELATION_POLICY_UNSAFE",
                path,
                "Many2one supports replace only.",
                "Choose replace.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    if relation.kind == "many2many":
        if len(relation.source_column_keys) != 1:
            issues.append(
                _issue(
                    "MAPPING_REFERENCE_KEY_INVALID",
                    path,
                    "Many2many requires one list-valued source column.",
                    "Choose exactly one source column.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        if relation.operation not in {"replace", "add", "remove"}:
            issues.append(
                _issue(
                    "MAPPING_RELATION_POLICY_UNSAFE",
                    path,
                    "The many2many operation is unsupported.",
                    "Choose replace, add, or remove.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
        if not relation.separator or len(relation.separator) != 1:
            issues.append(
                _issue(
                    "MAPPING_RELATION_POLICY_UNSAFE",
                    path,
                    "The many2many separator must be one character.",
                    "Choose one explicit separator.",
                    dataset=dataset,
                    target_field=relation.target_field,
                )
            )
    if relation.null_policy not in _NULL_POLICIES:
        issues.append(
            _issue(
                "MAPPING_RELATION_POLICY_UNSAFE",
                path,
                "The relation null policy is unsupported.",
                "Choose a supported null policy.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    if relation.on_missing not in {"error", "warning"}:
        issues.append(
            _issue(
                "MAPPING_RELATION_POLICY_UNSAFE",
                path,
                "The missing-reference policy is unsupported.",
                "Choose error or warning.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    if relation.on_ambiguous not in {"error", "warning"}:
        issues.append(
            _issue(
                "MAPPING_RELATION_POLICY_UNSAFE",
                path,
                "The ambiguous-reference policy is unsupported.",
                "Choose error or warning.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    policy = relation.categorical_policy
    if policy is None:
        issues.append(
            _issue(
                "MAPPING_CATEGORICAL_POLICY_REQUIRED",
                f"{path}/categorical_policy",
                "This relationship has no categorical business-key policy.",
                "Confirm exact business keys or require an explicit match for every source choice.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    elif policy not in {
        CategoricalCoveragePolicy.EXACT_BUSINESS_KEY,
        CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
    }:
        issues.append(
            _issue(
                "MAPPING_CATEGORICAL_POLICY_INVALID",
                f"{path}/categorical_policy",
                "A target-value policy cannot govern a relationship key.",
                "Choose exact business key or explicit key match.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    elif (
        policy is CategoricalCoveragePolicy.EXACT_BUSINESS_KEY
        and relation.resolver.value_mappings
    ):
        issues.append(
            _issue(
                "MAPPING_CATEGORICAL_POLICY_CONFLICT",
                f"{path}/categorical_policy",
                "Exact business-key coverage cannot also translate source choices.",
                "Remove the value matches or choose explicit key match.",
                dataset=dataset,
                target_field=relation.target_field,
            )
        )
    target_key_fields, target_scope_fields = relationship_target_fields(relation)
    _validate_resolver(
        context,
        dataset,
        relation.resolver,
        path,
        relation.source_column_keys,
        metadata.relation,
        metadata,
        issues,
        require_governed_key=True,
        target_key_fields=target_key_fields,
        target_scope_fields=target_scope_fields,
        validate_source_mappings=source_provider,
    )


def _validate_resolver(
    context: ValidationContext,
    dataset: DatasetMapping,
    resolver: RelationshipResolver,
    path: str,
    source_columns: tuple[str, ...],
    expected_model: str | None,
    relationship_metadata,
    issues: list[MappingValidationIssue],
    *,
    require_governed_key: bool,
    target_key_fields: tuple[str, ...] | None = None,
    target_scope_fields: tuple[str, ...] | None = None,
    validate_source_mappings: bool = True,
) -> None:
    uses_incoming = resolver.origin in {
        ResolverOrigin.DATASET,
        ResolverOrigin.TARGET_THEN_DATASET,
    }
    if uses_incoming:
        if not resolver.dataset_id:
            issues.append(
                _issue(
                    "MAPPING_REFERENCE_KEY_INVALID",
                    path,
                    "Incoming resolution requires a referenced dataset.",
                    "Choose the parent/reference dataset.",
                    dataset=dataset,
                )
            )
            return
        referenced_model = context.dataset_targets.get(resolver.dataset_id)
        projection_valid = False
        projection_field = resolver.dataset_projection_field
        if projection_field is not None:
            referenced_schema = context.schema_models.get(referenced_model or "")
            projected = next(
                (
                    field
                    for field in (referenced_schema.fields if referenced_schema else ())
                    if field.name == projection_field
                ),
                None,
            )
            projection_valid = bool(
                resolver.origin is ResolverOrigin.TARGET_THEN_DATASET
                and referenced_model is not None
                and expected_model is not None
                and referenced_model != expected_model
                and projected is not None
                and projected.type == "many2one"
                and projected.relation == expected_model
                and projected.readonly
            )
            if not projection_valid:
                issues.append(
                    _issue(
                        "MAPPING_GENERATED_TARGET_INVALID",
                        path,
                        (
                            "The selected generated-record link is not one captured "
                            "read-only many-to-one field from the incoming table's "
                            "Odoo model to this relationship model."
                        ),
                        "Choose a captured generated-record link or use matching Odoo models.",
                        dataset=dataset,
                    )
                )
        if (
            referenced_model is not None
            and expected_model is not None
            and referenced_model != expected_model
            and not projection_valid
        ):
            issues.append(
                _issue(
                    "MAPPING_RELATED_MODEL_INCORRECT",
                    path,
                    (
                        f"Referenced dataset targets {referenced_model}, "
                        f"but the relation expects {expected_model}."
                    ),
                    "Choose a dataset mapped to the captured related model.",
                    dataset=dataset,
                )
            )
        if resolver.origin is ResolverOrigin.DATASET and (
            resolver.model
            or resolver.key_mappings
            or resolver.scope_mappings
            or resolver.value_mappings
            or resolver.dataset_projection_field
        ):
            issues.append(
                _issue(
                    "MAPPING_REFERENCE_KEY_INVALID",
                    path,
                    "Incoming resolution must derive keys from its dataset.",
                    "Remove target-catalog key settings.",
                    dataset=dataset,
                )
            )
        if resolver.origin is ResolverOrigin.DATASET:
            return

    if (
        resolver.origin is ResolverOrigin.TARGET_CATALOG
        and (
            resolver.dataset_id is not None
            or resolver.dataset_projection_field is not None
        )
    ):
        issues.append(
            _issue(
                "MAPPING_REFERENCE_KEY_INVALID",
                path,
                (
                    "Target-catalog resolution cannot name an incoming dataset "
                    "or generated-record link."
                ),
                "Remove the incoming dataset settings.",
                dataset=dataset,
            )
        )
    if not resolver.model or resolver.model != expected_model:
        issues.append(
            _issue(
                "MAPPING_RELATED_MODEL_INCORRECT",
                path,
                (
                    f"Resolver model {resolver.model!r} does not match "
                    f"{expected_model!r}."
                ),
                "Use the related model captured from Odoo.",
                dataset=dataset,
            )
        )
        return
    model = context.schema_models.get(resolver.model)
    resolver_key_fields = (
        target_key_fields
        if target_key_fields is not None
        else tuple(item.target_field for item in resolver.key_mappings)
    )
    resolver_scope_fields = (
        target_scope_fields
        if target_scope_fields is not None
        else tuple(item.target_field for item in resolver.scope_mappings)
    )
    supporting_contracts = context.supporting_reference_contracts.get(
        (
            resolver.model,
            resolver_key_fields,
            resolver_scope_fields,
        )
    )
    try:
        odoo_major_version = int(
            str(context.schema_catalog.odoo_version).split(".", 1)[0]
        )
    except ValueError:
        odoo_major_version = -1
    reference_decision = authorize_governed_reference(
        GovernedReferenceRequest(
            parent_model=dataset.target_model,
            relationship_field=relationship_metadata.name,
            relationship_type=relationship_metadata.type,
            relationship_model=relationship_metadata.relation,
            related_model=resolver.model,
            key_fields=resolver_key_fields,
            scope_fields=resolver_scope_fields,
            requested_fields=(*resolver_key_fields, *resolver_scope_fields),
            purpose=ReferenceReadPurpose.MATCH_VALIDATION,
            odoo_major_version=odoo_major_version,
            governed_key=context.has_governed_key(
                resolver.model,
                resolver_key_fields,
                resolver_scope_fields,
            ),
        ),
        captured_fields=(
            captured_reference_field_contracts(model.fields)
            if model is not None
            else supporting_contracts
        ),
    )
    if (
        model is None
        and not reference_decision.accepted
        and reference_decision.denial
        in {
            ReferencePolicyDenial.MODEL_NOT_REVIEWED,
            ReferencePolicyDenial.ODOO_VERSION_MISMATCH,
        }
    ):
        issues.append(
            _issue(
                "MAPPING_TARGET_MODEL_UNKNOWN",
                path,
                "The resolver model is absent from the permitted schema.",
                "Refresh the linked Odoo values and check matches again.",
                dataset=dataset,
                target_field=relationship_metadata.name,
            )
        )
        return
    model_fields = set(context.fields_by_model.get(resolver.model, ()))
    if reference_decision.contract is not None:
        model_fields.update(reference_decision.contract.key_fields)
        model_fields.update(reference_decision.contract.scope_fields)
        model_fields.add(reference_decision.contract.display_field)
    if supporting_contracts is not None:
        model_fields.update(item.name for item in supporting_contracts)
    if not resolver_key_fields:
        issues.append(
            _issue(
                "MAPPING_REFERENCE_KEY_INVALID",
                path,
                "Target-catalog resolution requires a business key.",
                "Choose a confirmed business-key definition.",
                dataset=dataset,
            )
        )
    for target_field in (*resolver_key_fields, *resolver_scope_fields):
        if target_field not in model_fields:
            issues.append(
                _issue(
                    "MAPPING_TARGET_FIELD_UNKNOWN",
                    path,
                    f"Resolver field {resolver.model}.{target_field} is unavailable.",
                    "Choose a captured field.",
                    dataset=dataset,
                    target_field=target_field,
                )
            )
    for mapping in (
        (*resolver.key_mappings, *resolver.scope_mappings)
        if validate_source_mappings
        else ()
    ):
        if mapping.source_column_key not in source_columns:
            issues.append(
                _issue(
                    "MAPPING_REFERENCE_KEY_INVALID",
                    path,
                    "Resolver source keys must be declared relation columns.",
                    "Add the source key to the relation mapping.",
                    dataset=dataset,
                    source_column=mapping.source_column_key,
                )
            )
    mapped_sources = tuple(
        item.source_column_key
        for item in (*resolver.key_mappings, *resolver.scope_mappings)
    )
    if validate_source_mappings and mapped_sources != source_columns:
        issues.append(
            _issue(
                "MAPPING_REFERENCE_KEY_INVALID",
                path,
                (
                    "Resolver keys must consume every declared source "
                    "column once and in business-key order."
                ),
                "Align source columns with the selected key and scope.",
                dataset=dataset,
            )
        )
    if resolver.value_mappings and (
        len(source_columns) != 1
        or len(resolver.key_mappings) != 1
        or bool(resolver.scope_mappings)
    ):
        issues.append(
            _issue(
                "MAPPING_VALUE_MATCH_INVALID",
                path,
                (
                    "Value matching currently supports one source choice "
                    "and one Odoo business-key field."
                ),
                "Choose a single-column, single-key relationship.",
                dataset=dataset,
            )
        )
    elif resolver.value_mappings:
        key_field = resolver.key_mappings[0].target_field
        key_metadata = context.fields_by_model.get(
            resolver.model,
            {},
        ).get(key_field)
        supporting_key_metadata = next(
            (
                item
                for item in (supporting_contracts or ())
                if item.name == key_field
            ),
            None,
        )
        key_type = (
            key_metadata.type
            if key_metadata is not None
            else (
                supporting_key_metadata.field_type
                if supporting_key_metadata is not None
                else None
            )
        )
        if key_type is not None and key_type not in {
            "char",
            "text",
            "selection",
        }:
            issues.append(
                _issue(
                    "MAPPING_VALUE_MATCH_INVALID",
                    path,
                    "Quick matching requires a text-based Odoo key.",
                    "Use the normal governed mapping for this key type.",
                    dataset=dataset,
                )
            )
    if require_governed_key and not reference_decision.accepted:
        issues.append(
            _issue(
                "MAPPING_BUSINESS_KEY_NOT_GOVERNED",
                path,
                "Resolver key and scope are not a confirmed business key.",
                "Select one confirmed definition for the related model.",
                dataset=dataset,
            )
        )
