"""Present verified Recipe meaning for a data manager."""

from __future__ import annotations

from typing import Mapping


_PROVIDER_LABELS = {
    "CONCATENATE": "Combine source columns",
    "CONDITIONAL_RULES": "Decide using rules",
    "CONSTANT": "Use one value",
    "ODOO_DEFAULT": "Let Odoo choose",
    "REFERENCE_LOOKUP": "Look up reference data",
    "SOURCE": "Use one source column",
    "SOURCE_WITH_FALLBACK": "Use source value or backup",
}


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
            "provider": _PROVIDER_LABELS.get(
                str(provider.get("kind", "")),
                _label(provider.get("kind"), default="Saved rule"),
            ),
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
            "All source rows"
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
