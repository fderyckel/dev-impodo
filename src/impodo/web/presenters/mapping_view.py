"""Mapping page view-model helpers."""

from __future__ import annotations

import json

from fastapi import Request

from ...derived_entities import (
    DerivedEntityRule,
    derived_dataset_links,
    derived_mapping_samples,
    related_dataset_links,
)
from ...mapping_semantics import (
    MAX_CONTROL_TOTALS_PER_DATASET,
    BusinessKeyStatus,
    ScalarFieldMapping,
    ScalarValueError,
    ScalarValueSource,
    evaluate_scalar_mapping_value,
    mapping_issue_fingerprint,
)
from ...quality import (
    MAX_MANAGER_RULES_PER_DATASET,
    QualityOutcomePolicy,
    QualityOwnerRole,
    QualityRuleFamily,
    manager_quality_rule,
)
from ...workspace import WorkspaceError
from ..constants import DEFAULT_MAPPING_FIELDS_PER_PAGE, MAPPING_FIELD_PAGE_SIZES
from ..context import WebContext
from ..forms import (
    _positive_query_int,
    _text,
)
from .common import _render
from .mapping_forms import (
    _canonical_mapping_type,
    _related_business_keys,
    _resolver_business_key,
    _standard_reference_business_key,
)
from .mapping_impact import _mapping_field_page_size, _mapping_return_url


def _render_mapping(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    session_error = request.session.pop("mapping_error", None)
    if error is None and isinstance(session_error, str):
        error = session_error
    if error is None and request.query_params.get("save_error") == "request_rejected":
        error = (
            "The mapping request exceeded a safety limit or could not be read. "
            "No mapping change was saved; the last working draft is loaded."
        )
    selection = context.queries.get_mapping_source_selection(project_id)
    preparation_plan = context.queries.get_derived_entity_plan(project_id)
    schema = context.queries.get_odoo_schema_catalog(project_id)
    governance = context.queries.get_schema_governance(project_id)
    revision = context.queries.get_mapping_revision(project_id)
    stored_validation = (
        context.queries.get_mapping_validation(
            project_id, revision.version
        )
        if revision
        else None
    )
    stored_submission = (
        context.queries.get_mapping_submission(
            project_id, revision.version
        )
        if revision
        else None
    )
    working_draft = context.queries.get_mapping_working_draft(project_id)
    expected_schema_hash = None
    if governance is not None:
        expected_schema_hash = governance.content_hash
    elif schema is not None:
        expected_schema_hash = schema.content_hash
    working_draft_is_current = bool(
        working_draft is not None
        and selection is not None
        and expected_schema_hash is not None
        and working_draft.definition.source_selection_hash
        == selection.content_hash
        and working_draft.definition.schema_hash == expected_schema_hash
    )
    active_definition = None
    if working_draft_is_current and working_draft is not None:
        active_definition = working_draft.definition
    elif revision is not None:
        active_definition = revision.definition
    has_unvalidated_changes = bool(
        working_draft_is_current
        and working_draft is not None
        and (
            revision is None
            or working_draft.content_hash != revision.definition.content_hash
        )
    )
    validation = None if has_unvalidated_changes else stored_validation
    submission = None if has_unvalidated_changes else stored_submission
    previous_mapping_draft = context.queries.get_mapping_draft(project_id)
    source_catalogs = (
        context.queries.get_source_catalogs(project_id)
        if selection is not None
        else ()
    )
    dataset_count = len(selection.datasets) if selection is not None else 0
    active_dataset_index = min(
        _positive_query_int(
            request.query_params.get("mapping_dataset"),
            default=0,
        ),
        max(dataset_count - 1, 0),
    )
    scalar_page = _positive_query_int(
        request.query_params.get("scalar_page"),
        default=1,
    )
    scalar_page_size = _mapping_field_page_size(
        request.query_params.get("scalar_page_size")
    )
    relation_page = _positive_query_int(
        request.query_params.get("relation_page"),
        default=1,
    )
    relation_page_size = _mapping_field_page_size(
        request.query_params.get("relation_page_size")
    )
    relation_query = request.query_params.get("relation_query", "").strip()[:128]
    field_query = request.query_params.get("field_query", "").strip()[:128]
    mapped_only = request.query_params.get("mapped_only") == "1"
    lookup_links = derived_dataset_links(preparation_plan)
    lookup_samples: dict[str, dict[str, tuple[str | None, ...]]] = {}
    if preparation_plan is not None:
        lookup_rules = tuple(
            rule
            for rule in preparation_plan.rules
            if isinstance(rule, DerivedEntityRule)
        )
        for link, rule in zip(lookup_links, lookup_rules, strict=True):
            lookup_samples[link.derived_dataset_id] = derived_mapping_samples(
                link,
                context.derived_entities.preview(project_id, rule),
            )
    dataset_views = (
        _mapping_dataset_views(
            selection,
            schema,
            governance,
            active_definition.datasets if active_definition else (),
            source_catalogs,
            {
                index: request.query_params.get(f"target_model_{index}", "")
                for index, _item in enumerate(selection.datasets)
            },
            related_dataset_links(preparation_plan),
            lookup_links,
            lookup_samples,
            active_dataset_index=active_dataset_index,
            scalar_page=scalar_page,
            scalar_page_size=scalar_page_size,
            relation_page=relation_page,
            relation_page_size=relation_page_size,
            relation_query=relation_query,
            field_query=field_query,
            mapped_only=mapped_only,
        )
        if selection and schema
        else ()
    )
    for view in dataset_views:
        view["edit_url"] = _mapping_return_url(
            request,
            project_id,
            mapping_dataset=view["index"],
            scalar_page=1,
            relation_page=1,
            save_error=None,
        )
        if view["active"]:
            view["scalar_page_size_options"] = tuple(
                {
                    "size": size,
                    "url": _mapping_return_url(
                        request,
                        project_id,
                        scalar_page=1,
                        scalar_page_size=(
                            None
                            if size == DEFAULT_MAPPING_FIELDS_PER_PAGE
                            else size
                        ),
                        save_error=None,
                    ),
                }
                for size in MAPPING_FIELD_PAGE_SIZES
            )
            view["relation_page_size_options"] = tuple(
                {
                    "size": size,
                    "url": _mapping_return_url(
                        request,
                        project_id,
                        relation_page=1,
                        relation_page_size=(
                            None
                            if size == DEFAULT_MAPPING_FIELDS_PER_PAGE
                            else size
                        ),
                        save_error=None,
                    ),
                }
                for size in MAPPING_FIELD_PAGE_SIZES
            )
            view["scalar_previous_url"] = (
                _mapping_return_url(
                    request,
                    project_id,
                    scalar_page=int(view["scalar_page"]) - 1,
                    save_error=None,
                )
                if int(view["scalar_page"]) > 1
                else None
            )
            view["scalar_next_url"] = (
                _mapping_return_url(
                    request,
                    project_id,
                    scalar_page=int(view["scalar_page"]) + 1,
                    save_error=None,
                )
                if int(view["scalar_page"]) < int(view["scalar_page_count"])
                else None
            )
            view["relation_previous_url"] = (
                _mapping_return_url(
                    request,
                    project_id,
                    relation_page=int(view["relation_page"]) - 1,
                    save_error=None,
                )
                if int(view["relation_page"]) > 1
                else None
            )
            view["relation_next_url"] = (
                _mapping_return_url(
                    request,
                    project_id,
                    relation_page=int(view["relation_page"]) + 1,
                    save_error=None,
                )
                if int(view["relation_page"]) < int(view["relation_page_count"])
                else None
            )
    warning_issues = tuple(
        {
            "issue": item,
            "fingerprint": mapping_issue_fingerprint(item),
        }
        for item in (validation.issues if validation else ())
        if item.severity == "warning"
    )
    quality_view = None
    if (
        revision is not None
        and not has_unvalidated_changes
        and selection is not None
        and schema is not None
        and selection.datasets
    ):
        quality_view = _quality_check_view(
            revision.definition,
            selection,
            schema,
            active_dataset_index,
            context.queries.get_current_quality_ruleset(project_id),
        )
    return _render(
        request,
        "project_mapping.html",
        project=context.queries.get(project_id),
        selection=selection,
        schema=schema,
        governance=governance,
        revision=revision,
        validation=validation,
        submission=submission,
        working_draft=working_draft,
        working_draft_is_current=working_draft_is_current,
        working_draft_is_stale=(
            working_draft is not None and not working_draft_is_current
        ),
        has_unvalidated_changes=has_unvalidated_changes,
        has_previous_mapping_draft=previous_mapping_draft is not None,
        dataset_views=dataset_views,
        warning_issues=warning_issues,
        quality_view=quality_view,
        error=error,
        status_code=status_code,
    )


def _mapping_dataset_views(
    selection,
    schema,
    governance,
    existing_datasets,
    source_catalogs=(),
    selected_models=None,
    related_links=(),
    derived_links=(),
    prepared_source_samples=None,
    *,
    active_dataset_index=None,
    scalar_page=1,
    scalar_page_size=DEFAULT_MAPPING_FIELDS_PER_PAGE,
    relation_page=1,
    relation_page_size=DEFAULT_MAPPING_FIELDS_PER_PAGE,
    relation_query="",
    field_query="",
    mapped_only=False,
) -> tuple[dict[str, object], ...]:
    existing_by_id = {item.dataset_id: item for item in existing_datasets}
    models = {item.name: item for item in schema.models}
    keys = tuple(governance.business_keys) if governance else ()
    confirmed = tuple(
        item
        for item in keys
        if item.status is BusinessKeyStatus.CONFIRMED
    )
    link_by_child = {item.child_dataset_id: item for item in related_links}
    link_by_parent = {item.parent_dataset_id: item for item in related_links}
    parent_ids = {item.parent_dataset_id for item in related_links}
    derived_by_dataset = {
        item.derived_dataset_id: item for item in derived_links
    }
    derived_by_consumer: dict[str, list[object]] = {}
    for item in derived_links:
        derived_by_consumer.setdefault(item.consumer_dataset_id, []).append(item)
    prepared_source_samples = prepared_source_samples or {}

    def selected_mapping_model_name(dataset_index, source_dataset) -> str:
        existing = existing_by_id.get(source_dataset.dataset_id)
        derived_link = derived_by_dataset.get(source_dataset.dataset_id)
        selected_override = (
            selected_models.get(dataset_index, "")
            if selected_models is not None
            else ""
        )
        if selected_override in models:
            return selected_override
        if existing and existing.target_model in models:
            return existing.target_model
        if (
            derived_link is not None
            and derived_link.target_model in models
        ):
            return derived_link.target_model
        return next(
            (
                item.model
                for item in confirmed
                if item.model in models
            ),
            schema.models[0].name if schema.models else "",
        )

    selected_model_by_dataset = {
        source_dataset.dataset_id: selected_mapping_model_name(
            dataset_index,
            source_dataset,
        )
        for dataset_index, source_dataset in enumerate(selection.datasets)
    }
    result: list[dict[str, object]] = []
    for dataset_index, source_dataset in enumerate(selection.datasets):
        active = (
            active_dataset_index is None
            or dataset_index == active_dataset_index
        )
        source_samples = prepared_source_samples.get(
            source_dataset.dataset_id,
            _mapping_source_samples(source_dataset, source_catalogs),
        )
        existing = existing_by_id.get(source_dataset.dataset_id)
        derived_link = derived_by_dataset.get(source_dataset.dataset_id)
        selected_model_name = selected_model_by_dataset[
            source_dataset.dataset_id
        ]
        model = models.get(selected_model_name)
        model_keys = tuple(
            item for item in confirmed if item.model == selected_model_name
        )
        existing_identity_fields = tuple(
            target
            for component in (
                (*existing.target_identity, *existing.target_scope)
                if existing
                else ()
            )
            for target in component.target_fields
        )
        selected_key = next(
            (
                item
                for item in model_keys
                if (*item.key_fields, *item.scope_fields)
                == existing_identity_fields
            ),
            model_keys[0] if model_keys else None,
        )
        field_by_name = (
            {item.name: item for item in model.fields} if model else {}
        )
        existing_components = {
            item.target_fields[0]: item
            for item in (
                (*existing.target_identity, *existing.target_scope)
                if existing
                else ()
            )
            if len(item.target_fields) == 1
        }
        identity_rows: list[dict[str, object]] = []
        if selected_key is not None:
            for target_field in (
                *selected_key.key_fields,
                *selected_key.scope_fields,
            ):
                metadata = field_by_name.get(target_field)
                component = existing_components.get(target_field)
                related_keys = _related_business_keys(
                    confirmed,
                    metadata.relation if metadata is not None else None,
                )
                standard_related_key = _standard_reference_business_key(
                    metadata.relation if metadata is not None else None
                )
                selected_related_key = _resolver_business_key(
                    component.resolver if component else None,
                    related_keys,
                )
                identity_rows.append(
                    {
                        "target_field": target_field,
                        "scope": target_field in selected_key.scope_fields,
                        "metadata": metadata,
                        "relational": (
                            metadata is not None
                            and metadata.type == "many2one"
                        ),
                        "selected_sources": (
                            component.source_column_keys
                            if component
                            else (
                                (derived_link.name_column_key,)
                                if derived_link is not None
                                and target_field
                                == derived_link.target_name_field
                                else ()
                            )
                        ),
                        "related_keys": related_keys,
                        "selected_related_key": selected_related_key,
                        "recommended_related_key_id": (
                            standard_related_key.key_id
                            if standard_related_key is not None
                            and standard_related_key in related_keys
                            else ""
                        ),
                    }
                )
        identity_targets = {
            row["target_field"] for row in identity_rows
        }
        scalar_by_target = (
            {item.target_field: item for item in existing.fields}
            if existing
            else {}
        )
        all_scalar_fields = tuple(
            field
            for field in (model.fields if model else ())
            if field.type not in {"many2one", "many2many", "one2many"}
        )
        numeric_fields = tuple(
            field
            for field in all_scalar_fields
            if field.type in {"integer", "float", "monetary"}
        )
        existing_controls = existing.control_totals if existing else ()
        control_total_slots = tuple(
            {
                "index": index,
                "control": (
                    existing_controls[index]
                    if index < len(existing_controls)
                    else None
                ),
            }
            for index in range(MAX_CONTROL_TOTALS_PER_DATASET)
        )
        scalar_candidates = tuple(
            (field_index, field)
            for field_index, field in enumerate(all_scalar_fields)
            if field.name not in identity_targets
        )
        normalized_query = field_query.casefold()
        matching_scalars = tuple(
            (field_index, field)
            for field_index, field in scalar_candidates
            if (
                not normalized_query
                or normalized_query
                in f"{field.label} {field.name} {field.type}".casefold()
            )
            and (not mapped_only or field.name in scalar_by_target)
        )
        scalar_page_count = max(
            1,
            (len(matching_scalars) + scalar_page_size - 1)
            // scalar_page_size,
        )
        current_scalar_page = min(max(scalar_page, 1), scalar_page_count)
        scalar_start = (current_scalar_page - 1) * scalar_page_size
        visible_scalars = (
            matching_scalars[
                scalar_start : scalar_start + scalar_page_size
            ]
            if active
            else ()
        )
        scalar_rows = tuple(
            {
                "index": field_index,
                "metadata": field,
                "mapping": scalar_by_target.get(field.name),
                "value_mappings_json": _value_mappings_json(
                    scalar_by_target[field.name].value_mappings
                    if field.name in scalar_by_target
                    else ()
                ),
                "matched_choice_count": len(
                    scalar_by_target[field.name].value_mappings
                    if field.name in scalar_by_target
                    else ()
                ),
                "canonical_type": _canonical_mapping_type(field.type),
                "source_samples": source_samples,
                "recommended_source_column": (
                    derived_link.name_column_key
                    if derived_link is not None
                    and field.name == derived_link.target_name_field
                    and field.name not in scalar_by_target
                    else None
                ),
                "preview": _scalar_mapping_preview(
                    scalar_by_target.get(field.name),
                    source_samples,
                    source_dataset.columns,
                ),
            }
            for field_index, field in visible_scalars
        )
        relation_by_target = (
            {item.target_field: item for item in existing.relationships}
            if existing
            else {}
        )
        all_relation_fields = tuple(
            field
            for field in (model.fields if model else ())
            if field.type in {"many2one", "many2many", "one2many"}
        )
        relation_recommendations: dict[str, dict[str, object]] = {}
        related_link = link_by_child.get(source_dataset.dataset_id)
        if related_link is not None:
            parent_model = selected_model_by_dataset.get(
                related_link.parent_dataset_id
            )
            for field in all_relation_fields:
                if (
                    field.name not in identity_targets
                    and field.type in {"many2one", "many2many"}
                    and field.relation == parent_model
                ):
                    relation_recommendations[field.name] = {
                        "dataset_id": related_link.parent_dataset_id,
                        "source_columns": related_link.reference_column_keys,
                    }
        for link in derived_by_consumer.get(source_dataset.dataset_id, ()):
            derived_model = selected_model_by_dataset.get(link.derived_dataset_id)
            matches = tuple(
                field
                for field in all_relation_fields
                if field.name not in identity_targets
                and field.type == "many2one"
                and field.relation == derived_model
            )
            if len(matches) == 1:
                relation_recommendations[matches[0].name] = {
                    "dataset_id": link.derived_dataset_id,
                    "source_columns": (link.source_column_key,),
                    "kind": "extracted_lookup",
                }
        normalized_relation_query = relation_query.casefold()
        relation_candidates = sorted(
            (
                (relation_index, field)
                for relation_index, field in enumerate(all_relation_fields)
                if field.name not in identity_targets
                and (
                    not normalized_relation_query
                    or normalized_relation_query
                    in (
                        f"{field.label} {field.name} {field.type} "
                        f"{field.relation or ''}"
                    ).casefold()
                )
            ),
            key=lambda item: (
                0
                if item[1].name in relation_by_target
                else (
                    1
                    if item[1].name in relation_recommendations
                    else 2
                ),
                item[1].label.casefold(),
                item[1].name,
            ),
        )
        relation_page_count = max(
            1,
            (len(relation_candidates) + relation_page_size - 1)
            // relation_page_size,
        )
        current_relation_page = min(max(relation_page, 1), relation_page_count)
        relation_start = (current_relation_page - 1) * relation_page_size
        visible_relations = (
            relation_candidates[
                relation_start : relation_start + relation_page_size
            ]
            if active
            else ()
        )
        relation_rows: list[dict[str, object]] = []
        for relation_index, field in visible_relations:
            mapping = relation_by_target.get(field.name)
            recommendation = relation_recommendations.get(field.name)
            related_keys = _related_business_keys(
                confirmed,
                field.relation,
            )
            standard_related_key = _standard_reference_business_key(
                field.relation
            )
            row: dict[str, object] = {
                "index": relation_index,
                "metadata": field,
                "mapping": mapping,
                "related_keys": related_keys,
                "selected_key": _resolver_business_key(
                    mapping.resolver if mapping else None,
                    related_keys,
                ),
                "recommended_key_id": (
                    standard_related_key.key_id
                    if standard_related_key is not None
                    and standard_related_key in related_keys
                    else ""
                ),
                "value_mappings_json": _value_mappings_json(
                    mapping.resolver.value_mappings if mapping else ()
                ),
                "matched_choice_count": len(
                    mapping.resolver.value_mappings if mapping else ()
                ),
            }
            if recommendation is not None:
                row["recommended_dataset_id"] = recommendation["dataset_id"]
                row["recommended_source_columns"] = recommendation[
                    "source_columns"
                ]
                if recommendation.get("kind"):
                    row["recommendation_kind"] = recommendation["kind"]
            relation_rows.append(row)
        result.append(
            {
                "index": dataset_index,
                "active": active,
                "source": source_dataset,
                "source_by_key": {
                    item.stable_key: item for item in source_dataset.columns
                },
                "source_samples": source_samples,
                "mapping": existing,
                "selected_model": selected_model_name,
                "model": model,
                "models": schema.models,
                "business_keys": model_keys,
                "selected_key": selected_key,
                "identity_rows": tuple(identity_rows),
                "scalar_rows": scalar_rows,
                "numeric_fields": numeric_fields,
                "control_total_slots": control_total_slots,
                "scalar_catalog_total": len(scalar_candidates),
                "scalar_matching_total": len(matching_scalars),
                "scalar_mapped_total": len(scalar_by_target),
                "scalar_page": current_scalar_page,
                "scalar_page_count": scalar_page_count,
                "scalar_page_size": scalar_page_size,
                "relation_rows": tuple(relation_rows),
                "relation_catalog_total": len(all_relation_fields),
                "relation_matching_total": len(relation_candidates),
                "relation_mapped_total": len(relation_by_target),
                "relation_page": current_relation_page,
                "relation_page_count": relation_page_count,
                "relation_page_size": relation_page_size,
                "relation_query": relation_query,
                "field_query": field_query,
                "mapped_only": mapped_only,
                "other_datasets": tuple(
                    item
                    for item in selection.datasets
                    if item.dataset_id != source_dataset.dataset_id
                ),
                "related_role": (
                    "lookup"
                    if source_dataset.dataset_id in derived_by_dataset
                    else (
                        "child"
                        if source_dataset.dataset_id in link_by_child
                        else (
                            "parent"
                            if source_dataset.dataset_id in parent_ids
                            else None
                        )
                    )
                ),
                "recommended_source_identity": (
                    derived_link.canonical_key_column_key,
                )
                if derived_link is not None
                else (
                    link_by_child[
                        source_dataset.dataset_id
                    ].child_identity_column_keys
                    if source_dataset.dataset_id in link_by_child
                    else (
                        link_by_parent[
                            source_dataset.dataset_id
                        ].reference_column_keys
                        if source_dataset.dataset_id in link_by_parent
                        else ()
                    )
                ),
            }
        )
    return tuple(result)


def _quality_check_view(
    definition,
    selection,
    schema,
    dataset_index,
    current_ruleset,
) -> dict[str, object] | None:
    source = selection.datasets[dataset_index]
    mapping = next(
        (
            item
            for item in definition.datasets
            if item.dataset_id == source.dataset_id
        ),
        None,
    )
    if mapping is None:
        return None
    model = next(
        (item for item in schema.models if item.name == mapping.target_model),
        None,
    )
    if model is None:
        return None
    mapped = {item.target_field for item in mapping.fields}
    field_choices = tuple(
        {"name": field.name, "label": field.label or field.name}
        for field in model.fields
        if field.name in mapped
        and field.type not in {"many2one", "many2many", "one2many"}
    )
    rules_current = bool(
        current_ruleset is not None
        and current_ruleset.mapping_hash == definition.content_hash
        and current_ruleset.schema_hash == definition.schema_hash
    )
    existing = (
        tuple(
            item
            for item in current_ruleset.manager_rules
            if item.dataset == source.name
        )
        if rules_current
        else ()
    )
    return {
        "dataset_id": source.dataset_id,
        "dataset_name": source.name,
        "field_choices": field_choices,
        "slots": tuple(
            {
                "index": index,
                "rule": existing[index] if index < len(existing) else None,
            }
            for index in range(MAX_MANAGER_RULES_PER_DATASET)
        ),
        "automatic_checks": (
            "Required values",
            "Valid types, formats and choices",
            "Known lookup values",
            "Ready linked records",
            "Duplicate Odoo matches after preparation",
        ),
        "rules_current": rules_current,
    }


def _manager_quality_rules_from_form(
    form,
    *,
    project_id: str,
    dataset: str,
    allowed_fields: set[str],
):
    rules = []
    allowed_families = {
        QualityRuleFamily.REQUIRED_IF,
        QualityRuleFamily.EXACTLY_ONE_OF,
        QualityRuleFamily.ORDERED_COMPARISON,
        QualityRuleFamily.EQUALITY,
        QualityRuleFamily.INEQUALITY,
    }
    for index in range(MAX_MANAGER_RULES_PER_DATASET):
        name = _text(form, f"quality_name_{index}")
        family_value = _text(form, f"quality_family_{index}")
        field_a = _text(form, f"quality_field_a_{index}")
        field_b = _text(form, f"quality_field_b_{index}")
        equals = _text(form, f"quality_equals_{index}")
        outcome_value = _text(form, f"quality_outcome_{index}")
        owner_value = _text(form, f"quality_owner_{index}")
        if not any(
            (
                name,
                family_value,
                field_a,
                field_b,
                equals,
                outcome_value,
                owner_value,
            )
        ):
            continue
        if not name or len(name) > 80:
            raise WorkspaceError("Give each optional data check a short name")
        try:
            family = QualityRuleFamily(family_value)
        except ValueError as error:
            raise WorkspaceError(
                "Choose a supported business data check"
            ) from error
        if family not in allowed_families:
            raise WorkspaceError("Choose a supported business data check")
        if field_a not in allowed_fields or field_b not in allowed_fields:
            raise WorkspaceError("Choose two currently mapped Odoo fields")
        if field_a == field_b:
            raise WorkspaceError(
                "Choose two different fields for a business check"
            )
        if family is QualityRuleFamily.REQUIRED_IF and not equals:
            raise WorkspaceError(
                "Enter the condition value for the required-field check"
            )
        try:
            outcome = QualityOutcomePolicy(outcome_value)
        except ValueError as error:
            raise WorkspaceError(
                "Choose what should happen when a check fails"
            ) from error
        if outcome not in {
            QualityOutcomePolicy.WARNING,
            QualityOutcomePolicy.QUARANTINE,
            QualityOutcomePolicy.BLOCK,
        }:
            raise WorkspaceError(
                "Choose a supported outcome for the data check"
            )
        try:
            owner = QualityOwnerRole(owner_value)
        except ValueError as error:
            raise WorkspaceError(
                "Choose who should review the data check"
            ) from error
        rules.append(
            manager_quality_rule(
                project_id=project_id,
                dataset=dataset,
                family=family,
                name=name,
                input_fields=(field_a, field_b),
                parameters=(
                    {"equals": equals}
                    if family is QualityRuleFamily.REQUIRED_IF
                    else {}
                ),
                outcome=outcome,
                owner_role=owner,
            )
        )
    if len({item.rule_id for item in rules}) != len(rules):
        raise WorkspaceError(
            "Optional data checks must have unique names and choices"
        )
    return tuple(rules)


def _value_mappings_json(mappings) -> str:
    return json.dumps(
        [
            {
                "source_value": item.source_value,
                "target_value": item.target_value,
            }
            for item in mappings
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )


def _mapping_source_samples(
    source_dataset,
    source_catalogs,
) -> dict[str, tuple[str | None, ...]]:
    if source_dataset.dataset_id.startswith("derived:"):
        return {}
    catalog = next(
        (
            item
            for item in source_catalogs
            if item.file_id == source_dataset.file_id
            and item.source_sha256 == source_dataset.source_sha256
            and item.content_hash == source_dataset.catalog_hash
        ),
        None,
    )
    table = next(
        (
            item
            for item in (catalog.tables if catalog is not None else ())
            if item.table_key == source_dataset.table_key
        ),
        None,
    )
    if table is None:
        return {}
    result: dict[str, tuple[str | None, ...]] = {}
    for column in source_dataset.columns:
        values = tuple(
            row[column.ordinal - 1]
            for row in table.preview_rows
            if column.ordinal > 0 and column.ordinal <= len(row)
        )
        result[column.stable_key] = values[:3]
    return result


def _scalar_mapping_preview(
    mapping: ScalarFieldMapping | None,
    source_samples: dict[str, tuple[str | None, ...]],
    source_columns=(),
) -> dict[str, str] | None:
    if mapping is None:
        return None
    if mapping.value_source is ScalarValueSource.ODOO_DEFAULT:
        return {
            "raw": "Not sent",
            "proposed": "Odoo runtime default",
            "status": "deferred",
        }
    raw: object = None
    if mapping.value_source is ScalarValueSource.CONSTANT:
        raw = mapping.literal_value
    elif mapping.source_column_key:
        samples = source_samples.get(mapping.source_column_key, ())
        raw = samples[0] if samples else None
    try:
        proposed = evaluate_scalar_mapping_value(
            mapping,
            raw,
            source_values_by_ordinal={
                column.ordinal: (
                    source_samples.get(column.stable_key, (None,))[0]
                    if source_samples.get(column.stable_key)
                    else None
                )
                for column in source_columns
            },
        )
    except ScalarValueError as error:
        return {
            "raw": _display_mapping_value(raw),
            "proposed": str(error),
            "status": "error",
        }
    return {
        "raw": _display_mapping_value(raw),
        "proposed": _display_mapping_value(proposed),
        "status": "ok",
    }


def _display_mapping_value(value: object) -> str:
    if value is None:
        return "∅"
    if isinstance(value, bool):
        return "true" if value else "false"
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _safe_spreadsheet_text(value: object) -> object:
    """Prevent CSV values from becoming formulas when opened in Excel."""

    if not isinstance(value, str):
        return value
    if value.lstrip("\t\r\n").startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value
