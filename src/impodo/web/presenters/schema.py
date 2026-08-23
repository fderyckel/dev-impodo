"""Schema web helpers."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re

from fastapi import Request
from starlette.datastructures import FormData

from ...business_keys import (
    describe_business_key,
    recommend_business_key,
    selectable_business_key_fields,
)
from ...derived_entities import DerivedEntityRule
from ...workspace_state import WorkspaceState
from ...workspace_contracts import (
    OdooModelCatalog,
    OdooModelSummary,
    SchemaField,
    SchemaModel,
    SchemaOrigin,
)
from ...workspace_errors import WorkspaceError
from ..constants import (
    _APPLICATION_MODULE_PREFIXES,
    _MANUAL_FIELD_NAME,
    _MANUAL_FIELD_TYPE,
)
from ..context import WebContext
from ..forms import _text
from .common import _render


def _manual_schema_models(
    project: WorkspaceState,
    form: FormData,
) -> tuple[SchemaModel, ...]:
    """Parse the explicitly entered local-development schema contract."""

    return tuple(
        SchemaModel(
            name=model_name,
            label=(
                _text(form, f"manual_model_label_{index}").strip()
                or model_name
            ),
            fields=_manual_schema_fields(
                model_name,
                _text(form, f"manual_fields_{index}"),
            ),
        )
        for index, model_name in enumerate(project.intended_models)
    )


def _manual_schema_fields(
    model_name: str,
    value: str,
) -> tuple[SchemaField, ...]:
    """Parse ``name | label | type | required | readonly | relation | inverse``."""

    fields: list[SchemaField] = []
    for line_number, raw_line in enumerate(value.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if not 3 <= len(parts) <= 7:
            raise WorkspaceError(
                f"{model_name} line {line_number} must contain name, label, "
                "and type separated by |"
            )
        name, label, field_type, *optional = parts
        if not _MANUAL_FIELD_NAME.fullmatch(name):
            raise WorkspaceError(
                f"{model_name} line {line_number} has an invalid field name"
            )
        if not label or len(label) > 200:
            raise WorkspaceError(
                f"{model_name} line {line_number} needs a field label"
            )
        if not _MANUAL_FIELD_TYPE.fullmatch(field_type):
            raise WorkspaceError(
                f"{model_name} line {line_number} has an invalid field type"
            )
        required, readonly, relation, relation_field = (
            optional + ["", "", "", ""]
        )[:4]
        fields.append(
            SchemaField(
                name=name,
                label=label,
                type=field_type,
                required=_manual_schema_boolean(
                    required,
                    model_name,
                    line_number,
                    "required",
                ),
                readonly=_manual_schema_boolean(
                    readonly,
                    model_name,
                    line_number,
                    "readonly",
                ),
                relation=relation or None,
                relation_field=relation_field or None,
                selection=(),
            )
        )
    return tuple(fields)


def _manual_schema_boolean(
    value: str,
    model_name: str,
    line_number: int,
    label: str,
) -> bool:
    normalized = value.casefold()
    if normalized in {"", "0", "false", "no"}:
        return False
    if normalized in {"1", "true", "yes"}:
        return True
    raise WorkspaceError(
        f"{model_name} line {line_number} has an invalid {label} value"
    )


def _decode_delimiter(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if cleaned.casefold() == "tab":
        return "\t"
    return cleaned


def _dataset_choices(
    context: WebContext,
    project_id: str,
) -> tuple[dict[str, str], ...]:
    catalogs = {
        item.file_id: item
        for item in context.queries.get_source_catalogs(project_id)
    }
    choices: list[dict[str, str]] = []
    for configuration in context.queries.get_source_configurations(project_id):
        catalog = catalogs.get(configuration.file_id)
        if catalog is None or catalog.content_hash != configuration.catalog_hash:
            continue
        tables = {table.table_key: table for table in catalog.tables}
        for table_key in configuration.selected_table_keys:
            table = tables.get(table_key)
            if table is None:
                continue
            default_name = re.sub(
                r"[^a-z0-9]+",
                "_",
                f"{Path(catalog.display_name).stem}_{table.name}".casefold(),
            ).strip("_")[:63]
            if not default_name or not default_name[0].isalpha():
                default_name = f"dataset_{len(choices) + 1}"
            choices.append(
                {
                    "file_id": catalog.file_id,
                    "file_name": catalog.display_name,
                    "table_key": table.table_key,
                    "table_name": table.name,
                    "table_label": "" if table.kind == "CSV" else table.name,
                    "default_name": default_name,
                    "row_count": str(table.row_count),
                    "column_count": str(table.column_count),
                }
            )
    return tuple(choices)


def _render_derived_entities(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
    pending_related: dict[str, object] | None = None,
    pending_lookup: dict[str, object] | None = None,
):
    project = context.queries.get(project_id)
    selection = context.queries.get_source_selection(project_id)
    plan = context.queries.get_derived_entity_plan(project_id)
    model_catalog = context.queries.get_odoo_model_catalog(project_id)
    model_choices = tuple(
        sorted(
            (
                {"name": item.name, "label": item.label}
                for item in (model_catalog.models if model_catalog else ())
            ),
            key=lambda item: (
                str(item["label"]).casefold(),
                str(item["name"]),
            ),
        )
    )
    source_choices = tuple(
        {
            "value": f"{dataset.dataset_id}|{column.stable_key}",
            "dataset_name": dataset.name,
            "column_name": column.source_name,
            "candidate_type": column.candidate_type,
        }
        for dataset in (selection.datasets if selection else ())
        for column in dataset.columns
    )
    rule_views: list[dict[str, object]] = []
    related_rule_views: list[dict[str, object]] = []
    for rule in (plan.rules if plan else ()):
        try:
            preview = (
                context.derived_entities.preview(project_id, rule)
                if isinstance(rule, DerivedEntityRule)
                else context.derived_entities.preview_related(project_id, rule)
            )
            preview_error = None
        except WorkspaceError as preview_failure:
            preview = None
            preview_error = str(preview_failure)
        target = (
            rule_views
            if isinstance(rule, DerivedEntityRule)
            else related_rule_views
        )
        target.append(
            {
                "rule": rule,
                "preview": preview,
                "preview_error": preview_error,
            }
        )
    split_sources = {
        view["rule"].source_dataset_id for view in related_rule_views
    }
    related_source_views = tuple(
        {
            "dataset": dataset,
            "columns": dataset.columns,
            "has_split": dataset.dataset_id in split_sources,
            "parent_name_default": _related_dataset_name_default(
                dataset.name,
                "parents",
            ),
            "child_name_default": _related_dataset_name_default(
                dataset.name,
                "lines",
            ),
        }
        for dataset in (selection.datasets if selection else ())
    )
    namespace = re.sub(
        r"[^a-z0-9]+",
        "_",
        project.source_system.casefold(),
    ).strip("_")[:40]
    if not namespace or not namespace[0].isalpha():
        namespace = "imported_data"
    return _render(
        request,
        "project_derived_entities.html",
        project=project,
        selection=selection,
        plan=plan,
        model_catalog=model_catalog,
        source_choices=source_choices,
        model_choices=model_choices,
        rule_views=tuple(rule_views),
        related_rule_views=tuple(related_rule_views),
        related_source_views=related_source_views,
        pending_related=pending_related,
        pending_lookup=pending_lookup,
        namespace_default=namespace,
        error=error,
        status_code=status_code,
    )


def _related_dataset_name_default(source_name: str, suffix: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", source_name.casefold()).strip("_")
    if not base or not base[0].isalpha():
        base = "source"
    return f"{base[:54]}_{suffix}"


def _schema_model_choices(
    project: WorkspaceState,
    catalog: OdooModelCatalog | None,
) -> tuple[dict[str, object], ...]:
    selected = set(project.intended_models)
    models = list(catalog.models) if catalog else []
    known = {model.name for model in models}
    models.extend(
        OdooModelSummary(
            name=name,
            label=name,
            modules=(),
            state="unknown",
        )
        for name in sorted(selected - known)
    )
    choices = [
        {
            "name": model.name,
            "label": model.label,
            "modules": model.modules,
            "state": model.state,
            "selected": model.name in selected,
            "in_focus": _model_matches_application_scope(
                model,
                project.intended_applications,
            ),
        }
        for model in models
    ]
    return tuple(
        sorted(
            choices,
            key=lambda item: (
                not bool(item["selected"]),
                not bool(item["in_focus"]),
                str(item["label"]).casefold(),
                str(item["name"]),
            ),
        )
    )


def _model_matches_application_scope(
    model: OdooModelSummary,
    applications: tuple[str, ...],
) -> bool:
    if not applications or "Custom applications" in applications:
        return True
    if "Contacts" in applications and (
        model.name.startswith(("res.partner", "res.country", "res.lang"))
        or "contacts" in model.modules
    ):
        return True
    for application in applications:
        for prefix in _APPLICATION_MODULE_PREFIXES.get(application, ()):
            if any(
                module == prefix or module.startswith(f"{prefix}_")
                for module in model.modules
            ):
                return True
    return False


def _render_schema(
    request: Request,
    context: WebContext,
    project_id: str,
    *,
    error: str | None = None,
    support_error: str | None = None,
    status_code: int = 200,
    schema_load_failed: bool = False,
    key_drafts: Mapping[
        str,
        tuple[tuple[str, ...], tuple[str, ...], str],
    ]
    | None = None,
    key_errors: Mapping[str, str] | None = None,
):
    project = context.queries.get(project_id)
    model_catalog = context.queries.get_odoo_model_catalog(project_id)
    model_choices = _schema_model_choices(project, model_catalog)
    schema = context.queries.get_odoo_schema_catalog(project_id)
    governance = context.queries.get_schema_governance(project_id)
    governed_by_model = (
        {item.model: item for item in governance.business_keys}
        if governance
        else {}
    )
    key_views = _schema_key_views(
        schema,
        governed_by_model,
        key_drafts=key_drafts,
        key_errors=key_errors,
    )
    return _render(
        request,
        "project_schema.html",
        project=project,
        selection=context.queries.get_source_selection(project_id),
        model_catalog=model_catalog,
        model_choices=model_choices,
        focus_model_count=sum(
            1 for choice in model_choices if choice["in_focus"]
        ),
        schema=schema,
        schema_field_count=(
            sum(len(model.fields) for model in schema.models)
            if schema is not None
            else 0
        ),
        governance=governance,
        governed_by_model=governed_by_model,
        key_views=key_views,
        local_stack=context.local_stack.get(project_id),
        manual_schema_by_model=(
            {model.name: model for model in schema.models}
            if schema and schema.origin is SchemaOrigin.LOCAL_MANUAL
            else {}
        ),
        error=error,
        support_error=support_error,
        schema_load_failed=schema_load_failed,
        status_code=status_code,
    )


def _schema_key_views(
    schema,
    governed_by_model,
    *,
    key_drafts=None,
    key_errors=None,
):
    if schema is None:
        return ()
    drafts = key_drafts or {}
    errors = key_errors or {}
    views = []
    for model in schema.models:
        existing = governed_by_model.get(model.name)
        recommendation = recommend_business_key(model)
        draft = drafts.get(model.name)
        key_fields = (
            draft[0]
            if draft is not None
            else existing.key_fields if existing else ()
        )
        scope_fields = (
            draft[1]
            if draft is not None
            else existing.scope_fields if existing else ()
        )
        description = (
            draft[2]
            if draft is not None
            else existing.description if existing else ""
        )
        views.append(
            {
                "model": model,
                "existing": existing,
                "key_fields": key_fields,
                "scope_fields": scope_fields,
                "description": description,
                "key_error": errors.get(model.name),
                "existing_summary": (
                    describe_business_key(
                        model,
                        key_fields,
                        scope_fields,
                    )
                    if key_fields
                    else ""
                ),
                "recommendation": recommendation,
                "field_choices": selectable_business_key_fields(model),
                "field_labels": {
                    field.name: field.label for field in model.fields
                },
            }
        )
    return tuple(views)

