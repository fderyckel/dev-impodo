"""Mapping form-to-domain translation helpers."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import Iterable

from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from ...domain.schema.governance import (
    BusinessKeyDefinition,
    BusinessKeyStatus,
)
from ...domain.mapping.contracts import (
    MAX_CONTROL_TOTALS_PER_DATASET,
    BusinessControlDefinition,
    CategoricalCoveragePolicy,
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    MappingControlExpectation,
    MappingTargetMode,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ScalarFieldMapping,
    ScalarValueSource,
    SelectionCondition,
    SelectionConditionOperator,
    SelectionRule,
    SelectionRuleJoin,
    SelectionRuleSet,
    TargetFieldDisposition,
    TargetFieldHandling,
)
from ...value_rules import (
    MAX_TEXT_TRANSFORM_STEPS,
    ScalarTransformPolicy,
    ScalarValidationPolicy,
    TextTransformStep,
)
from ...domain.source_binding import OdooSourceBinding, SourceOriginKind
from ...workspace_state import WorkspaceState, WorkspaceStatus
from ...reference_keys import standard_reference_key
from ...workspace_errors import WorkspaceError
from ..context import WebContext
from ..forms import (
    _checked,
    _optional_int,
    _text,
    _texts,
    _value_mappings_from_form,
)


def _text_steps_from_form(
    form,
    field_name: str,
) -> tuple[TextTransformStep, ...]:
    """Read one bounded ordered sequence from the current browser form."""

    if field_name not in form:
        return ()
    raw_value = _text(form, field_name)
    try:
        payload = json.loads(raw_value or "[]")
    except json.JSONDecodeError as error:
        raise ValueError("Text cleanup steps could not be read") from error
    if not isinstance(payload, list) or len(payload) > MAX_TEXT_TRANSFORM_STEPS:
        raise ValueError("Text cleanup steps are invalid")
    expected = {
        "kind",
        "search_value",
        "replacement_value",
        "search_mode",
        "replace_all",
        "characters",
    }
    steps = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != expected:
            raise ValueError("Text cleanup steps are invalid")
        if not all(
            isinstance(item[name], str)
            for name in expected.difference({"replace_all"})
        ) or not isinstance(item["replace_all"], bool):
            raise ValueError("Text cleanup steps are invalid")
        steps.append(TextTransformStep(**item))
    return tuple(steps)


def _selection_rules_from_form(form, field_name: str) -> SelectionRuleSet:
    """Read one strict, bounded conditional Selection provider."""

    raw_value = _text(form, field_name)
    if not raw_value or len(raw_value.encode("utf-8")) > 65_536:
        raise ValueError("Conditional choice rules are missing or too large")
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise ValueError("Conditional choice rules could not be read") from error
    if not isinstance(payload, dict) or set(payload) != {
        "rules",
        "otherwise_value",
    }:
        raise ValueError("Conditional choice rules are invalid")
    if not isinstance(payload["rules"], list):
        raise ValueError("Conditional choice rules are invalid")
    rules = []
    for rule_payload in payload["rules"]:
        if not isinstance(rule_payload, dict) or set(rule_payload) != {
            "rule_id",
            "conditions",
            "target_value",
            "join",
        }:
            raise ValueError("Conditional choice rules are invalid")
        if not isinstance(rule_payload["conditions"], list):
            raise ValueError("Conditional choice conditions are invalid")
        conditions = []
        for condition_payload in rule_payload["conditions"]:
            if not isinstance(condition_payload, dict) or set(condition_payload) != {
                "condition_id",
                "source_column_key",
                "operator",
                "comparison_value",
                "value_type",
            }:
                raise ValueError("Conditional choice conditions are invalid")
            if not all(
                isinstance(condition_payload[name], str)
                for name in {
                    "condition_id",
                    "source_column_key",
                    "operator",
                    "value_type",
                }
            ) or not (
                condition_payload["comparison_value"] is None
                or isinstance(condition_payload["comparison_value"], str)
            ):
                raise ValueError("Conditional choice conditions are invalid")
            conditions.append(
                SelectionCondition(
                    condition_id=condition_payload["condition_id"],
                    source_column_key=condition_payload["source_column_key"],
                    operator=SelectionConditionOperator(
                        condition_payload["operator"]
                    ),
                    comparison_value=condition_payload["comparison_value"],
                    value_type=condition_payload["value_type"],
                )
            )
        rules.append(
            SelectionRule(
                rule_id=str(rule_payload["rule_id"]),
                conditions=tuple(conditions),
                target_value=str(rule_payload["target_value"]),
                join=SelectionRuleJoin(rule_payload["join"]),
            )
        )
    otherwise = payload["otherwise_value"]
    if otherwise is not None and not isinstance(otherwise, str):
        raise ValueError("Conditional choice otherwise value is invalid")
    return SelectionRuleSet(tuple(rules), otherwise)


def _mapping_allowed_fields(form, selection, schema) -> set[str]:
    allowed = {
        "csrf_token",
        "action",
        "editable_dataset_id",
        "expected_parent_version",
        "expected_working_draft_version",
        "warning_acknowledgement",
    }
    model_names = {item.name for item in schema.models}
    models = {item.name: item for item in schema.models}
    for dataset_index, _dataset in enumerate(selection.datasets):
        allowed.update(
            {
                f"target_model_{dataset_index}",
                f"mode_{dataset_index}",
                f"on_existing_{dataset_index}",
                f"source_identity_{dataset_index}",
                f"business_key_{dataset_index}",
                f"visible_scalar_target_{dataset_index}",
                f"visible_relation_target_{dataset_index}",
                f"target_field_disposition_{dataset_index}",
                f"approved_write_field_{dataset_index}",
            }
        )
        target_model = _text(form, f"target_model_{dataset_index}")
        if target_model not in model_names:
            continue
        model = models[target_model]
        for identity_index in range(len(model.fields)):
            allowed.update(
                {
                    f"identity_source_{dataset_index}_{identity_index}",
                    f"identity_resolver_key_{dataset_index}_{identity_index}",
                }
            )
        scalar_fields = [
            item
            for item in model.fields
            if item.type not in {"many2one", "many2many", "one2many"}
        ]
        for field_index, _field in enumerate(scalar_fields):
            allowed.update(
                {
                    f"scalar_value_source_{dataset_index}_{field_index}",
                    f"scalar_source_{dataset_index}_{field_index}",
                    f"scalar_literal_{dataset_index}_{field_index}",
                    f"scalar_type_{dataset_index}_{field_index}",
                    f"scalar_trim_{dataset_index}_{field_index}",
                    f"scalar_collapse_{dataset_index}_{field_index}",
                    f"scalar_empty_null_{dataset_index}_{field_index}",
                    f"scalar_case_{dataset_index}_{field_index}",
                    f"scalar_decimal_locale_{dataset_index}_{field_index}",
                    f"scalar_date_format_{dataset_index}_{field_index}",
                    f"scalar_timezone_{dataset_index}_{field_index}",
                    f"scalar_text_steps_{dataset_index}_{field_index}",
                    f"scalar_round_places_{dataset_index}_{field_index}",
                    f"scalar_round_mode_{dataset_index}_{field_index}",
                    f"scalar_formula_{dataset_index}_{field_index}",
                    f"scalar_exact_length_{dataset_index}_{field_index}",
                    f"scalar_segment_location_{dataset_index}_{field_index}",
                    f"scalar_segment_length_{dataset_index}_{field_index}",
                    f"scalar_character_class_{dataset_index}_{field_index}",
                    f"scalar_pattern_{dataset_index}_{field_index}",
                    f"scalar_value_matches_{dataset_index}_{field_index}",
                    f"scalar_selection_rules_{dataset_index}_{field_index}",
                    f"scalar_categorical_policy_{dataset_index}_{field_index}",
                    f"scalar_compare_{dataset_index}_{field_index}",
                    f"scalar_validate_only_{dataset_index}_{field_index}",
                    f"scalar_required_{dataset_index}_{field_index}",
                    f"scalar_required_create_{dataset_index}_{field_index}",
                    f"scalar_null_{dataset_index}_{field_index}",
                }
            )
        for control_index in range(MAX_CONTROL_TOTALS_PER_DATASET):
            allowed.update(
                {
                    f"control_name_{dataset_index}_{control_index}",
                    f"control_id_{dataset_index}_{control_index}",
                    f"control_target_{dataset_index}_{control_index}",
                    f"control_expected_{dataset_index}_{control_index}",
                    f"control_unit_{dataset_index}_{control_index}",
                    f"control_tolerance_{dataset_index}_{control_index}",
                }
            )
        relation_fields = [
            item
            for item in model.fields
            if item.type in {"many2one", "many2many", "one2many"}
        ]
        for relation_index, _field in enumerate(relation_fields):
            allowed.update(
                {
                    f"relation_source_{dataset_index}_{relation_index}",
                    f"relation_origin_{dataset_index}_{relation_index}",
                    f"relation_dataset_{dataset_index}_{relation_index}",
                    f"relation_key_{dataset_index}_{relation_index}",
                    f"relation_operation_{dataset_index}_{relation_index}",
                    f"relation_compare_{dataset_index}_{relation_index}",
                    f"relation_validate_only_{dataset_index}_{relation_index}",
                    f"relation_required_{dataset_index}_{relation_index}",
                    f"relation_required_create_{dataset_index}_{relation_index}",
                    f"relation_missing_{dataset_index}_{relation_index}",
                    f"relation_ambiguous_{dataset_index}_{relation_index}",
                    f"relation_null_{dataset_index}_{relation_index}",
                    f"relation_separator_{dataset_index}_{relation_index}",
                    f"relation_value_matches_{dataset_index}_{relation_index}",
                    f"relation_categorical_policy_{dataset_index}_{relation_index}",
                }
            )
    return allowed


def _mapping_datasets_from_form(
    form,
    selection,
    schema,
    governance,
) -> tuple[DatasetMapping, ...]:
    models = {item.name: item for item in schema.models}
    keys = _available_mapping_business_keys(schema, governance)
    datasets: list[DatasetMapping] = []
    for dataset_index, source_dataset in enumerate(selection.datasets):
        pinned_update = source_dataset.origin is SourceOriginKind.ODOO
        target_model = _text(form, f"target_model_{dataset_index}")
        if pinned_update:
            binding = source_dataset.source
            if not isinstance(binding, OdooSourceBinding):
                raise WorkspaceError("The protected Odoo source is not current")
            if target_model != binding.model:
                raise WorkspaceError(
                    "Captured Odoo records must stay on their originating model"
                )
        model = models.get(target_model)
        if model is None:
            raise WorkspaceError("Choose a captured target model")
        selected_key = (
            None
            if pinned_update
            else keys.get(_text(form, f"business_key_{dataset_index}"))
        )
        if selected_key is not None and selected_key.model != target_model:
            raise WorkspaceError("Target business key does not match its model")
        field_by_name = {item.name: item for item in model.fields}
        source_columns = {
            item.stable_key for item in source_dataset.columns
        }
        identity_components: list[IdentityComponentMapping] = []
        scope_components: list[IdentityComponentMapping] = []
        identity_targets: set[str] = set()
        key_fields = (
            (*selected_key.key_fields, *selected_key.scope_fields)
            if selected_key
            else ()
        )
        for identity_index, target_field in enumerate(key_fields):
            selected_sources = tuple(
                item
                for item in _texts(
                    form,
                    f"identity_source_{dataset_index}_{identity_index}",
                )
                if item in source_columns
            )
            metadata = field_by_name.get(target_field)
            resolver = None
            if metadata is not None and metadata.type == "many2one":
                related_key = keys.get(
                    _text(
                        form,
                        (
                            f"identity_resolver_key_{dataset_index}_"
                            f"{identity_index}"
                        ),
                    )
                )
                resolver = _target_catalog_resolver(
                    metadata.relation,
                    related_key,
                    selected_sources,
                )
            component = IdentityComponentMapping(
                source_column_keys=selected_sources,
                target_fields=(target_field,),
                value_type=(
                    "string"
                    if resolver is not None
                    else _canonical_mapping_type(
                        metadata.type if metadata else "char"
                    )
                ),
                resolver=resolver,
            )
            target = (
                scope_components
                if selected_key
                and target_field in selected_key.scope_fields
                else identity_components
            )
            target.append(component)
            identity_targets.add(target_field)

        scalar_fields = [
            item
            for item in model.fields
            if item.type not in {"many2one", "many2many", "one2many"}
        ]
        scalar_mappings: list[ScalarFieldMapping] = []
        for field_index, metadata in enumerate(scalar_fields):
            value_source_text = _text(
                form,
                f"scalar_value_source_{dataset_index}_{field_index}",
            )
            if not value_source_text or metadata.name in identity_targets:
                continue
            value_source = ScalarValueSource(value_source_text)
            source_key = _text(
                form, f"scalar_source_{dataset_index}_{field_index}"
            )
            literal_value = _text(
                form, f"scalar_literal_{dataset_index}_{field_index}"
            )
            text_steps = _text_steps_from_form(
                form,
                f"scalar_text_steps_{dataset_index}_{field_index}",
            )
            value_mappings = _value_mappings_from_form(
                form,
                f"scalar_value_matches_{dataset_index}_{field_index}",
            )
            selection_rules = (
                _selection_rules_from_form(
                    form,
                    f"scalar_selection_rules_{dataset_index}_{field_index}",
                )
                if value_source is ScalarValueSource.CONDITIONAL_RULES
                else None
            )
            categorical_policy = None
            if (
                metadata.type == "selection"
                and value_source is not ScalarValueSource.ODOO_DEFAULT
            ):
                policy_text = _text(
                    form,
                    f"scalar_categorical_policy_{dataset_index}_{field_index}",
                )
                categorical_policy = (
                    CategoricalCoveragePolicy(policy_text)
                    if policy_text
                    else (
                        CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH
                        if value_mappings
                        else CategoricalCoveragePolicy.EXACT_TARGET_VALUE
                    )
                )
            scalar_mappings.append(
                ScalarFieldMapping(
                    target_field=metadata.name,
                    source_column_key=(
                        source_key
                        if value_source
                        in {
                            ScalarValueSource.SOURCE,
                            ScalarValueSource.SOURCE_WITH_FALLBACK,
                        }
                        else None
                    ),
                    value_source=value_source,
                    literal_value=(
                        literal_value
                        if value_source
                        in {
                            ScalarValueSource.CONSTANT,
                            ScalarValueSource.SOURCE_WITH_FALLBACK,
                        }
                        else None
                    ),
                    transform=(ScalarTransformPolicy() if selection_rules else ScalarTransformPolicy(
                        trim=_checked(
                            form,
                            f"scalar_trim_{dataset_index}_{field_index}",
                        ),
                        collapse_whitespace=_checked(
                            form,
                            f"scalar_collapse_{dataset_index}_{field_index}",
                        ),
                        empty_as_null=_checked(
                            form,
                            f"scalar_empty_null_{dataset_index}_{field_index}",
                        ),
                        case_mode=(
                            _text(
                                form,
                                f"scalar_case_{dataset_index}_{field_index}",
                            )
                            or "preserve"
                        ),
                        decimal_locale=(
                            _text(
                                form,
                                (
                                    f"scalar_decimal_locale_{dataset_index}_"
                                    f"{field_index}"
                                ),
                            )
                            or "invariant"
                        ),
                        date_format=(
                            _text(
                                form,
                                f"scalar_date_format_{dataset_index}_{field_index}",
                            )
                            or "iso"
                        ),
                        timezone=(
                            _text(
                                form,
                                f"scalar_timezone_{dataset_index}_{field_index}",
                            )
                            or "UTC"
                        ),
                        decimal_places=_optional_int(
                            _text(
                                form,
                                f"scalar_round_places_{dataset_index}_{field_index}",
                            )
                        ),
                        rounding_mode=(
                            _text(
                                form,
                                f"scalar_round_mode_{dataset_index}_{field_index}",
                            )
                            or "half_up"
                        ),
                        formula=_text(
                            form,
                            f"scalar_formula_{dataset_index}_{field_index}",
                        ),
                        text_steps=text_steps,
                    )),
                    validation=ScalarValidationPolicy(
                        exact_length=_optional_int(
                            _text(
                                form,
                                f"scalar_exact_length_{dataset_index}_{field_index}",
                            )
                        ),
                        segment_location=(
                            _text(
                                form,
                                f"scalar_segment_location_{dataset_index}_{field_index}",
                            )
                            or "none"
                        ),
                        segment_length=_optional_int(
                            _text(
                                form,
                                f"scalar_segment_length_{dataset_index}_{field_index}",
                            )
                        ),
                        character_class=(
                            _text(
                                form,
                                f"scalar_character_class_{dataset_index}_{field_index}",
                            )
                            or "none"
                        ),
                        pattern=_text(
                            form,
                            f"scalar_pattern_{dataset_index}_{field_index}",
                        ),
                    ),
                    value_mappings=(value_mappings if selection_rules is None else ()),
                    categorical_policy=categorical_policy,
                    value_type=(
                        _text(
                            form,
                            f"scalar_type_{dataset_index}_{field_index}",
                        )
                        or _canonical_mapping_type(metadata.type)
                    ),
                    compare=_checked(
                        form,
                        f"scalar_compare_{dataset_index}_{field_index}",
                    ),
                    validate_only=_checked(
                        form,
                        f"scalar_validate_only_{dataset_index}_{field_index}",
                    ),
                    required=_checked(
                        form,
                        f"scalar_required_{dataset_index}_{field_index}",
                    ),
                    required_on_create=_checked(
                        form,
                        (
                            f"scalar_required_create_{dataset_index}_"
                            f"{field_index}"
                        ),
                    ),
                    null_policy=(
                        _text(
                            form,
                            f"scalar_null_{dataset_index}_{field_index}",
                        )
                        or "distinct"
                    ),
                    selection_rules=selection_rules,
                )
            )

        relation_fields = [
            item
            for item in model.fields
            if item.type in {"many2one", "many2many", "one2many"}
        ]
        relationships: list[RelationshipMapping] = []
        for relation_index, metadata in enumerate(relation_fields):
            if metadata.name in identity_targets:
                continue
            selected_sources = tuple(
                item
                for item in _texts(
                    form,
                    f"relation_source_{dataset_index}_{relation_index}",
                )
                if item in source_columns
            )
            if not selected_sources:
                continue
            origin = ResolverOrigin(
                _text(
                    form,
                    f"relation_origin_{dataset_index}_{relation_index}",
                )
                or ResolverOrigin.TARGET_CATALOG.value
            )
            if origin is ResolverOrigin.DATASET:
                value_mappings = _value_mappings_from_form(
                    form,
                    f"relation_value_matches_{dataset_index}_{relation_index}",
                )
                if value_mappings:
                    raise ValueError(
                        "Existing-record matches cannot use an incoming dataset"
                    )
                resolver = RelationshipResolver(
                    origin=origin,
                    dataset_id=_text(
                        form,
                        f"relation_dataset_{dataset_index}_{relation_index}",
                    )
                    or None,
                )
            else:
                resolver = replace(
                    _target_catalog_resolver(
                        metadata.relation,
                        keys.get(
                            _text(
                                form,
                                f"relation_key_{dataset_index}_{relation_index}",
                            )
                        ),
                        selected_sources,
                    ),
                    value_mappings=_value_mappings_from_form(
                        form,
                        (
                            f"relation_value_matches_{dataset_index}_"
                            f"{relation_index}"
                        ),
                    ),
                )
            policy_text = _text(
                form,
                f"relation_categorical_policy_{dataset_index}_{relation_index}",
            )
            relationships.append(
                RelationshipMapping(
                    target_field=metadata.name,
                    kind=metadata.type,
                    source_column_keys=selected_sources,
                    resolver=resolver,
                    compare=_checked(
                        form,
                        f"relation_compare_{dataset_index}_{relation_index}",
                    ),
                    validate_only=_checked(
                        form,
                        (
                            f"relation_validate_only_{dataset_index}_"
                            f"{relation_index}"
                        ),
                    ),
                    required=_checked(
                        form,
                        f"relation_required_{dataset_index}_{relation_index}",
                    ),
                    required_on_create=_checked(
                        form,
                        (
                            f"relation_required_create_{dataset_index}_"
                            f"{relation_index}"
                        ),
                    ),
                    on_missing=(
                        _text(
                            form,
                            f"relation_missing_{dataset_index}_{relation_index}",
                        )
                        or "error"
                    ),
                    on_ambiguous=(
                        _text(
                            form,
                            (
                                f"relation_ambiguous_{dataset_index}_"
                                f"{relation_index}"
                            ),
                        )
                        or "error"
                    ),
                    operation=(
                        _text(
                            form,
                            (
                                f"relation_operation_{dataset_index}_"
                                f"{relation_index}"
                            ),
                        )
                        or "replace"
                    ),
                    separator=(
                        _text(
                            form,
                            (
                                f"relation_separator_{dataset_index}_"
                                f"{relation_index}"
                            ),
                        )
                        or ";"
                    ),
                    null_policy=(
                        _text(
                            form,
                            f"relation_null_{dataset_index}_{relation_index}",
                        )
                        or "distinct"
                    ),
                    categorical_policy=(
                        CategoricalCoveragePolicy(policy_text)
                        if policy_text
                        else (
                            CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH
                            if resolver.value_mappings
                            else CategoricalCoveragePolicy.EXACT_BUSINESS_KEY
                        )
                    ),
                )
            )
        mode = MappingTargetMode(
            _text(form, f"mode_{dataset_index}") or "upsert"
        )
        if pinned_update and mode is not MappingTargetMode.ODOO_PINNED_UPDATE:
            raise WorkspaceError(
                "Captured Odoo records can only use protected update mode"
            )
        dispositions: list[TargetFieldDisposition] = []
        disposition_targets: set[str] = set()
        for raw_disposition in _texts(
            form,
            f"target_field_disposition_{dataset_index}",
        ):
            target_field, separator, handling = raw_disposition.partition(":")
            if (
                separator != ":"
                or target_field not in field_by_name
                or target_field in disposition_targets
            ):
                raise WorkspaceError(
                    "An Odoo-required field decision is not current"
                )
            dispositions.append(
                TargetFieldDisposition(
                    target_field=target_field,
                    handling=TargetFieldHandling(handling),
                )
            )
            disposition_targets.add(target_field)
        if pinned_update and dispositions:
            raise WorkspaceError(
                "Create-time Odoo field decisions are not valid for captured records"
            )
        raw_approved_write_fields = _texts(
            form,
            f"approved_write_field_{dataset_index}",
        )
        if any(item not in field_by_name for item in raw_approved_write_fields):
            raise WorkspaceError("An approved Odoo update field is not current")
        approved_write_fields = tuple(sorted(set(raw_approved_write_fields)))
        if not pinned_update and approved_write_fields:
            raise WorkspaceError(
                "Odoo update approval is only valid for captured Odoo records"
            )
        control_definitions: list[BusinessControlDefinition] = []
        control_expectations: list[MappingControlExpectation] = []
        numeric_targets = {
            item.name
            for item in scalar_fields
            if item.type in {"integer", "float", "monetary"}
        }
        for control_index in range(MAX_CONTROL_TOTALS_PER_DATASET):
            name = _text(
                form, f"control_name_{dataset_index}_{control_index}"
            )
            target_field = _text(
                form, f"control_target_{dataset_index}_{control_index}"
            )
            expected = _text(
                form, f"control_expected_{dataset_index}_{control_index}"
            )
            unit = _text(
                form, f"control_unit_{dataset_index}_{control_index}"
            )
            tolerance = _text(
                form, f"control_tolerance_{dataset_index}_{control_index}"
            )
            control_id = (
                _text(form, f"control_id_{dataset_index}_{control_index}")
                or f"control:{target_field}"
            )
            if not any((name, target_field, expected, unit)):
                continue
            if not name or not target_field or not expected:
                raise WorkspaceError(
                    "Complete the name, numeric field and expected value for "
                    "each totals check"
                )
            if target_field not in numeric_targets:
                raise WorkspaceError(
                    "Choose a number, quantity or amount field for the totals check"
                )
            control_definitions.append(
                BusinessControlDefinition(
                    control_id=control_id,
                    name=name,
                    target_field=target_field,
                    unit=unit,
                    tolerance=tolerance or "0",
                )
            )
            control_expectations.append(
                MappingControlExpectation(
                    control_id=control_id,
                    expected_total=expected,
                )
            )
        datasets.append(
            DatasetMapping(
                dataset_id=source_dataset.dataset_id,
                target_model=target_model,
                mode=mode,
                on_existing=(
                    _text(form, f"on_existing_{dataset_index}") or "block"
                    if mode is MappingTargetMode.CREATE
                    else None
                ),
                source_identity_column_keys=tuple(
                    ()
                    if pinned_update
                    else (
                        item
                        for item in _texts(
                            form, f"source_identity_{dataset_index}"
                        )
                        if item in source_columns
                    )
                ),
                target_identity=tuple(identity_components),
                target_scope=tuple(scope_components),
                fields=tuple(
                    sorted(
                        scalar_mappings,
                        key=lambda item: item.target_field,
                    )
                ),
                relationships=tuple(
                    sorted(
                        relationships,
                        key=lambda item: item.target_field,
                    )
                ),
                target_field_dispositions=tuple(
                    sorted(
                        dispositions,
                        key=lambda item: item.target_field,
                    )
                ),
                approved_write_fields=approved_write_fields,
                control_definitions=tuple(control_definitions),
                control_expectations=tuple(control_expectations),
            )
        )
    return tuple(datasets)


def _active_mapping_definition(
    context: WebContext,
    workspace_id: str,
    selection,
    schema,
    governance,
) -> MappingDefinition | None:
    expected_schema_hash = (
        governance.content_hash if governance is not None else schema.content_hash
    )
    working_draft = context.queries.get_mapping_working_draft(workspace_id)
    if (
        working_draft is not None
        and working_draft.definition.source_selection_hash == selection.content_hash
        and working_draft.definition.schema_hash == expected_schema_hash
    ):
        return working_draft.definition
    revision = context.queries.get_mapping_revision(workspace_id)
    if (
        revision is not None
        and revision.definition.source_selection_hash == selection.content_hash
        and revision.definition.schema_hash == expected_schema_hash
    ):
        return revision.definition
    return None


def _merge_partial_mapping_datasets(
    parsed_datasets: tuple[DatasetMapping, ...],
    active_definition: MappingDefinition | None,
    form: FormData,
    selection,
    schema,
) -> tuple[DatasetMapping, ...]:
    editable_dataset_id = _text(form, "editable_dataset_id")
    if not editable_dataset_id:
        return parsed_datasets
    dataset_indexes = {
        item.dataset_id: index for index, item in enumerate(selection.datasets)
    }
    editable_index = dataset_indexes.get(editable_dataset_id)
    if editable_index is None:
        raise WorkspaceError("Editable mapping dataset is not current")

    parsed_by_id = {item.dataset_id: item for item in parsed_datasets}
    existing_by_id = {
        item.dataset_id: item
        for item in (active_definition.datasets if active_definition else ())
    }
    models = {item.name: item for item in schema.models}
    merged: list[DatasetMapping] = []
    for source_dataset in selection.datasets:
        parsed = parsed_by_id[source_dataset.dataset_id]
        existing = existing_by_id.get(source_dataset.dataset_id)
        compatible_existing = (
            existing
            if existing is not None
            and existing.target_model == parsed.target_model
            else None
        )
        if source_dataset.dataset_id != editable_dataset_id:
            if (
                parsed.fields
                or parsed.relationships
                or parsed.control_definitions
                or parsed.control_expectations
                or parsed.approved_write_fields
            ):
                raise WorkspaceError(
                    "Mapping request changed a dataset that is not being edited"
                )
            existing_dispositions = (
                compatible_existing.target_field_dispositions
                if compatible_existing
                else ()
            )
            if parsed.target_field_dispositions != existing_dispositions:
                raise WorkspaceError(
                    "Mapping request changed a hidden Odoo-field decision"
                )
            merged.append(
                replace(
                    parsed,
                    fields=(compatible_existing.fields if compatible_existing else ()),
                    relationships=(
                        compatible_existing.relationships
                        if compatible_existing
                        else ()
                    ),
                    control_definitions=(
                        compatible_existing.control_definitions
                        if compatible_existing
                        else ()
                    ),
                    control_expectations=(
                        compatible_existing.control_expectations
                        if compatible_existing
                        else ()
                    ),
                    target_field_dispositions=existing_dispositions,
                    approved_write_fields=(
                        compatible_existing.approved_write_fields
                        if compatible_existing
                        else ()
                    ),
                )
            )
            continue

        model = models.get(parsed.target_model)
        if model is None:
            raise WorkspaceError("Choose a captured target model")
        scalar_names = {
            item.name
            for item in model.fields
            if item.type not in {"many2one", "many2many", "one2many"}
        }
        relation_names = {
            item.name
            for item in model.fields
            if item.type in {"many2one", "many2many", "one2many"}
        }
        visible_scalars = set(
            _texts(form, f"visible_scalar_target_{editable_index}")
        )
        visible_relations = set(
            _texts(form, f"visible_relation_target_{editable_index}")
        )
        if not visible_scalars.issubset(scalar_names):
            raise WorkspaceError("Visible scalar mapping fields are not current")
        if not visible_relations.issubset(relation_names):
            raise WorkspaceError("Visible relationship fields are not current")
        if any(item.target_field not in visible_scalars for item in parsed.fields):
            raise WorkspaceError("Mapping request changed a hidden scalar field")
        if any(
            item not in visible_scalars
            for item in parsed.approved_write_fields
        ):
            raise WorkspaceError("Mapping request changed a hidden Odoo approval")
        if any(
            item.target_field not in visible_relations
            for item in parsed.relationships
        ):
            raise WorkspaceError("Mapping request changed a hidden relationship")

        field_by_target = {
            item.target_field: item
            for item in (compatible_existing.fields if compatible_existing else ())
            if item.target_field not in visible_scalars
        }
        field_by_target.update({item.target_field: item for item in parsed.fields})
        relation_by_target = {
            item.target_field: item
            for item in (
                compatible_existing.relationships if compatible_existing else ()
            )
            if item.target_field not in visible_relations
        }
        relation_by_target.update(
            {item.target_field: item for item in parsed.relationships}
        )
        approved_write_fields = {
            item
            for item in (
                compatible_existing.approved_write_fields
                if compatible_existing
                else ()
            )
            if item not in visible_scalars
        }
        approved_write_fields.update(parsed.approved_write_fields)
        identity_targets = {
            target
            for component in (*parsed.target_identity, *parsed.target_scope)
            for target in component.target_fields
        }
        merged.append(
            replace(
                parsed,
                fields=tuple(
                    item
                    for target, item in sorted(field_by_target.items())
                    if target not in identity_targets
                ),
                relationships=tuple(
                    item
                    for target, item in sorted(relation_by_target.items())
                    if target not in identity_targets
                ),
                approved_write_fields=tuple(sorted(approved_write_fields)),
            )
        )
    return tuple(merged)


def _standard_reference_business_key(
    model: str | None,
) -> BusinessKeyDefinition | None:
    rule = standard_reference_key(model or "")
    if rule is None:
        return None
    return BusinessKeyDefinition(
        key_id=(
            f"odoo-standard:{rule.model}:"
            f"{'+'.join((*rule.key_fields, *rule.scope_fields))}"
        ),
        model=rule.model,
        key_fields=rule.key_fields,
        scope_fields=rule.scope_fields,
        description=rule.description,
        status=BusinessKeyStatus.CONFIRMED,
    )


def _related_business_keys(
    definitions: Iterable[BusinessKeyDefinition],
    model: str | None,
) -> tuple[BusinessKeyDefinition, ...]:
    confirmed = tuple(
        item
        for item in definitions
        if item.model == model and item.status is BusinessKeyStatus.CONFIRMED
    )
    standard = _standard_reference_business_key(model)
    if standard is None or any(
        item.key_fields == standard.key_fields
        and item.scope_fields == standard.scope_fields
        for item in confirmed
    ):
        return confirmed
    return (*confirmed, standard)


def _available_mapping_business_keys(
    schema,
    governance,
) -> dict[str, BusinessKeyDefinition]:
    confirmed = tuple(
        item
        for item in (
            governance.business_keys if governance is not None else ()
        )
        if item.status is BusinessKeyStatus.CONFIRMED
    )
    keys = {item.key_id: item for item in confirmed}
    related_models = {
        field.relation
        for model in schema.models
        for field in model.fields
        if field.relation
    }
    for related_model in related_models:
        for key in _related_business_keys(confirmed, related_model):
            keys.setdefault(key.key_id, key)
    return keys


def _target_catalog_resolver(
    related_model: str | None,
    business_key,
    selected_sources: tuple[str, ...],
) -> RelationshipResolver:
    key_count = len(business_key.key_fields) if business_key else 0
    return RelationshipResolver(
        origin=ResolverOrigin.TARGET_CATALOG,
        model=related_model,
        key_mappings=tuple(
            ReferenceKeyMapping(source, target)
            for source, target in zip(
                selected_sources[:key_count],
                business_key.key_fields if business_key else (),
                strict=False,
            )
        ),
        scope_mappings=tuple(
            ReferenceKeyMapping(source, target)
            for source, target in zip(
                selected_sources[key_count:],
                business_key.scope_fields if business_key else (),
                strict=False,
            )
        ),
    )


def _resolver_business_key(resolver, candidates):
    if resolver is None or resolver.origin is not ResolverOrigin.TARGET_CATALOG:
        return candidates[0] if candidates else None
    key_fields = tuple(item.target_field for item in resolver.key_mappings)
    scope_fields = tuple(item.target_field for item in resolver.scope_mappings)
    return next(
        (
            item
            for item in candidates
            if item.key_fields == key_fields
            and item.scope_fields == scope_fields
        ),
        candidates[0] if candidates else None,
    )


def _canonical_mapping_type(odoo_type: str) -> str:
    return {
        "boolean": "boolean",
        "integer": "integer",
        "float": "decimal",
        "monetary": "decimal",
        "date": "date",
        "datetime": "datetime",
    }.get(odoo_type, "string")


def _comma_values(value: str) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in value.split(",")
        if item.strip()
    )


def _business_key_id(
    model: str,
    key_fields: tuple[str, ...],
    scope_fields: tuple[str, ...],
) -> str:
    payload = "\0".join((model, *key_fields, "\0", *scope_fields))
    return f"key:{model}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _draft_or_redirect(
    context: WebContext,
    workspace_id: str,
) -> WorkspaceState | RedirectResponse:
    workspace_state = context.queries.get(workspace_id)
    if workspace_state.status is not WorkspaceStatus.DRAFT:
        return RedirectResponse(
            f"/workspaces/{workspace_state.workspace_id}/summary",
            status_code=303,
        )
    return workspace_state
