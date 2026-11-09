"""Present verified Recipe meaning for a data manager."""

from __future__ import annotations

from collections.abc import Mapping

_PROVIDER_LABELS = {
    "CONCATENATE": "Combine source columns",
    "CONDITIONAL_RULES": "Decide using rules",
    "CONSTANT": "Use one value",
    "ODOO_DEFAULT": "Let Odoo choose",
    "REFERENCE_LOOKUP": "Look up reference data",
    "SOURCE": "Use one source column",
    "SOURCE_WITH_FALLBACK": "Use source value or backup",
}
_MAX_REVIEW_ITEMS = 20


def build_recipe_change_view(
    baseline: Mapping[str, object], current: Mapping[str, object],
) -> Mapping[str, object]:
    """Summarize reusable field rules and source requirements by logical identity."""

    old_columns, new_columns = _source_columns(baseline), _source_columns(current)
    old_datasets, new_datasets = _mapped_datasets(baseline), _mapped_datasets(current)
    old_fields, new_fields = _scalar_fields(old_datasets), _scalar_fields(new_datasets)
    field_keys = sorted(set(old_fields) | set(new_fields))
    changes = []
    counts = {"added": 0, "changed": 0, "removed": 0}
    for key in field_keys:
        before, after = old_fields.get(key), new_fields.get(key)
        if before == after:
            continue
        change = "added" if before is None else "removed" if after is None else "changed"
        counts[change] += 1
        if len(changes) < _MAX_REVIEW_ITEMS:
            dataset, model, name = key
            field = after if after is not None else old_fields[key]
            mapping = (new_datasets if after is not None else old_datasets)[(dataset, model)]
            provider = _mapping(field.get("provider"))
            columns = new_columns if after is not None else old_columns
            changes.append({
                "change": change.title(),
                "model": model,
                "field": name,
                "provider": _provider_label(provider),
                "columns": tuple(
                    columns.get((dataset, str(column)), _logical_column_label(str(column)))
                    for column in _sequence(provider.get("source_column_ids"))
                ),
                "write_behavior": _write_behavior(mapping, field, provider),
                "blank_behavior": _blank_behavior(field, provider),
            })
    added = sorted(new_columns.keys() - old_columns.keys())
    removed = sorted(old_columns.keys() - new_columns.keys())
    return {
        "fields": changes,
        "field_counts": counts,
        "more_fields": sum(counts.values()) - len(changes),
        "added_columns": tuple(new_columns[key] for key in added[:_MAX_REVIEW_ITEMS]),
        "removed_columns": tuple(old_columns[key] for key in removed[:_MAX_REVIEW_ITEMS]),
        "added_column_count": len(added),
        "removed_column_count": len(removed),
        "other_changes": _other_changes(baseline, current, old_datasets, new_datasets),
        "has_changes": baseline != current,
    }


def _mapped_datasets(definition: Mapping[str, object]):
    return {
        (str(dataset.get("logical_dataset_id", "")), str(dataset.get("target_model", ""))): dataset
        for item in _sequence(_mapping(definition.get("mapping")).get("datasets"))
        for dataset in (_mapping(item),)
    }


def _scalar_fields(datasets: Mapping[tuple[str, str], Mapping[str, object]]):
    return {
        (*key, str(field.get("target_field", ""))): field
        for key, dataset in datasets.items()
        for item in _sequence(dataset.get("fields"))
        for field in (_mapping(item),)
    }


def _source_columns(definition: Mapping[str, object]):
    return {
        (str(dataset.get("logical_dataset_id", "")), str(column.get("logical_column_id", ""))):
        str(column.get("source_name") or "Source column")
        for item in _sequence(_mapping(definition.get("source_shape")).get("datasets"))
        for dataset in (_mapping(item),)
        for raw in _sequence(dataset.get("columns"))
        for column in (_mapping(raw),)
    }


def _other_changes(baseline, current, old_datasets, new_datasets) -> tuple[str, ...]:
    labels = {
        "source_preparation": "Source preparation",
        "quality": "Data checks",
        "reference_dependencies": "Reference data",
        "parameter_definitions": "Inputs when reused",
        "control_definitions": "Controls",
        "contract_versions": "Recipe format",
    }
    changes = [label for key, label in labels.items() if baseline.get(key) != current.get(key)]
    old_rules = {key: {k: v for k, v in item.items() if k != "fields"} for key, item in old_datasets.items()}
    new_rules = {key: {k: v for k, v in item.items() if k != "fields"} for key, item in new_datasets.items()}
    if old_rules != new_rules:
        changes.append("Table, identity, relationship, or write rules")
    if any(baseline.get(key) != current.get(key) for key in ("odoo_target_contract", "target_governance")):
        changes.append("Odoo field requirements")
    return tuple(changes)


def _provider_label(provider: Mapping[str, object]) -> str:
    kind = provider.get("kind")
    return _PROVIDER_LABELS.get(str(kind), _label(kind, default="Saved rule"))


def _logical_column_label(column: str) -> str:
    return column.rsplit(".", 1)[-1].removeprefix("column:").replace("_", " ")


def _write_behavior(dataset, field, provider) -> str:
    if field.get("validate_only"):
        return "Check only"
    if provider.get("kind") == "ODOO_DEFAULT":
        return "Let Odoo supply the value on create"
    return {
        "CREATE": "Use on create",
        "UPSERT": "Use on create and eligible updates",
        "REFERENCE": "Use for reference only",
    }.get(dataset.get("mode"), "See the saved table rules")


def _blank_behavior(field, provider) -> str:
    if provider.get("kind") == "SOURCE_WITH_FALLBACK":
        return "Use the saved backup value for a blank source cell"
    if provider.get("kind") != "SOURCE":
        return "Follow the saved value rule"
    if field.get("null_policy") == "ignore_source_null":
        return "Omit the field when its prepared value is blank"
    return "Send a blank prepared value to Odoo when the field allows it"


def build_recipe_definition_view(
    envelope: Mapping[str, object],
) -> Mapping[str, object]:
    """Return a bounded, business-readable projection of one verified revision."""

    definition = _mapping(envelope.get("recipe"))
    source_shape = _mapping(definition.get("source_shape"))
    source_datasets = tuple(
        _mapping(item) for item in _sequence(source_shape.get("datasets"))
    )
    source_by_id = {
        str(item.get("logical_dataset_id", "")): item for item in source_datasets
    }
    mapping = _mapping(definition.get("mapping"))
    datasets = tuple(
        _dataset_view(_mapping(item), source_by_id)
        for item in _sequence(mapping.get("datasets"))
    )
    preparation = _mapping(definition.get("source_preparation"))
    quality = _mapping(definition.get("quality"))
    controls = _mapping(definition.get("control_definitions"))
    references = _mapping(definition.get("reference_dependencies"))
    parameters = _mapping(definition.get("parameter_definitions"))
    return {
        "datasets": datasets,
        "preparation_rules": tuple(
            {
                "kind": _label(item.get("kind"), default="Preparation rule"),
                "output": str(
                    item.get("output_dataset_name")
                    or item.get("target_model")
                    or "Prepared data"
                ),
            }
            for raw in _sequence(preparation.get("rules"))
            for item in (_mapping(raw),)
        ),
        "quality_rules": tuple(
            {
                "dataset": _dataset_name(
                    str(item.get("dataset_id", "")),
                    source_by_id,
                ),
                "name": str(item.get("name") or "Data check"),
                "severity": _label(item.get("severity"), default="Check"),
            }
            for raw in _sequence(quality.get("rules"))
            for item in (_mapping(raw),)
        ),
        "controls": tuple(
            {
                "dataset": _dataset_name(
                    str(item.get("dataset_id", "")),
                    source_by_id,
                ),
                "name": str(item.get("name") or "Control"),
                "target_field": str(item.get("target_field") or ""),
            }
            for raw in _sequence(controls.get("controls"))
            for item in (_mapping(raw),)
        ),
        "reference_count": len(_sequence(references.get("references"))),
        "parameter_count": len(_sequence(parameters.get("parameters"))),
    }


def _dataset_view(
    dataset: Mapping[str, object],
    source_by_id: Mapping[str, Mapping[str, object]],
) -> Mapping[str, object]:
    logical_id = str(dataset.get("logical_dataset_id", ""))
    source = source_by_id.get(logical_id, {})
    row_inclusion = _mapping(dataset.get("row_inclusion"))
    row_condition_count = len(_sequence(row_inclusion.get("conditions")))
    columns = tuple(
        str(item.get("source_name") or "Source column")
        for raw in _sequence(source.get("columns"))
        for item in (_mapping(raw),)
    )
    fields = tuple(
        {
            "target_field": str(item.get("target_field") or "Odoo field"),
            "provider": _provider_label(provider),
            "condition_count": sum(
                len(_sequence(_mapping(rule).get("conditions")))
                for rule in _sequence(provider.get("rules"))
            ),
        }
        for raw in _sequence(dataset.get("fields"))
        for item in (_mapping(raw),)
        for provider in (_mapping(item.get("provider")),)
    )
    relationships = tuple(
        {
            "target_field": str(item.get("target_field") or "Relationship"),
            "target": str(
                item.get("target_model")
                or _dataset_name(
                    str(item.get("target_dataset_id", "")),
                    source_by_id,
                )
            ),
            "rule": _label(item.get("value_source"), default="Saved relationship"),
        }
        for raw in _sequence(dataset.get("relationships"))
        for item in (_mapping(raw),)
    )
    return {
        "name": str(source.get("logical_name") or logical_id or "Source table"),
        "target_model": str(dataset.get("target_model") or "Odoo record type"),
        "mode": _label(dataset.get("mode"), default="Saved behavior"),
        "row_selection": (
            "Rows whose linked parent is included"
            if row_inclusion.get("included_parent_field")
            else "All source rows"
            if row_inclusion.get("mode") in {None, "all_rows"}
            else (
                f"Rows matching {row_condition_count} saved "
                f"condition{'s' if row_condition_count != 1 else ''}"
            )
        ),
        "columns": columns,
        "fields": fields,
        "relationships": relationships,
    }


def _dataset_name(
    logical_id: str,
    source_by_id: Mapping[str, Mapping[str, object]],
) -> str:
    source = source_by_id.get(logical_id, {})
    return str(source.get("logical_name") or logical_id or "Source table")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> tuple[object, ...]:
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _label(value: object, *, default: str) -> str:
    text = str(value or "").strip()
    return text.replace("_", " ").title() if text else default
