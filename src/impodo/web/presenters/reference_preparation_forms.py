"""Author and preview model-neutral values used to match linked records."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from impodo.domain.mapping.contracts import RelationshipResolver
from impodo.domain.mapping.reference_preparation import (
    ReferencePart,
    ReferencePreparation,
    preparation_source_keys,
    prepare_reference_value,
    validate_reference_preparation,
)
from impodo.domain.recipe.value_rules import FormulaValidationError, formula_input_names
from impodo.domain.source_binding import FileSourceBinding
from impodo.domain.workspace.contracts import SourceDataset

MAX_REFERENCE_COMPONENTS = 10
MAX_REFERENCE_PARTS = 20


def reference_preparation_fields(prefix: str) -> set[str]:
    """Allow only bounded controls for the selected reference components."""

    return {
        f"{prefix}_{index}_{suffix}"
        for index in range(MAX_REFERENCE_COMPONENTS)
        for suffix in ("mode", "formula", "bindings", "parts")
    }


def _binding_map(raw: str, columns: set[str]) -> dict[str, str]:
    payload = json.loads(raw or "[]")
    if not isinstance(payload, list) or len(payload) > 200:
        raise ValueError("The formula's source columns could not be read")
    result = {}
    for pair in payload:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(value, str) for value in pair)
            or not re.fullmatch(r"column_[1-9][0-9]*", pair[0])
            or pair[1] not in columns
            or pair[0] in result
        ):
            raise ValueError("Review the source columns used by this formula")
        result[pair[0]] = pair[1]
    return result


def apply_reference_preparations(
    form: Any,
    prefix: str,
    resolver: RelationshipResolver,
    source: SourceDataset,
) -> RelationshipResolver:
    """Attach browser preparation choices to each exact key and scope part."""

    columns = {column.stable_key for column in source.columns}
    ordinals = {
        f"column_{column.ordinal}": column.stable_key for column in source.columns
    }
    mappings = (*resolver.key_mappings, *resolver.scope_mappings)
    prepared = []
    for index, mapping in enumerate(mappings):
        field = f"{prefix}_{index}"
        mode = str(form.get(f"{field}_mode", "source"))
        preparation = None
        if mode == "formula":
            formula = str(form.get(f"{field}_formula", ""))
            retained = _binding_map(str(form.get(f"{field}_bindings", "[]")), columns)
            try:
                names = set(
                    formula_input_names(
                        formula,
                        allowed_names={"value", *ordinals, *retained},
                    )
                ) - {"value"}
            except FormulaValidationError:
                names = set(re.findall(r"\bcolumn_[1-9][0-9]*\b", formula))
            bindings = tuple(
                (name, retained.get(name, ordinals.get(name, "")))
                for name in sorted(names)
                if retained.get(name, ordinals.get(name, ""))
            )
            preparation = ReferencePreparation(formula=formula, bindings=bindings)
        elif mode == "combine":
            raw_parts = json.loads(str(form.get(f"{field}_parts", "[]")))
            if not isinstance(raw_parts, list) or len(raw_parts) > MAX_REFERENCE_PARTS:
                raise ValueError("Choose up to 20 source columns or fixed text parts")
            parts = []
            for part in raw_parts:
                if not isinstance(part, dict) or set(part) not in (
                    {"source_column_key"},
                    {"literal"},
                ):
                    raise ValueError(
                        "Choose a source column or fixed text for every part"
                    )
                if "source_column_key" in part:
                    key = part["source_column_key"]
                    if key not in columns:
                        raise ValueError(
                            "Choose a current source column for every reference part"
                        )
                    parts.append(ReferencePart(source_column_key=key))
                elif not isinstance(part["literal"], str):
                    raise ValueError("Fixed reference text must be text")
                else:
                    parts.append(ReferencePart(literal=part["literal"]))
            preparation = ReferencePreparation(parts=tuple(parts))
        elif mode != "source":
            raise ValueError("Choose how to prepare this matching value")
        prepared.append(replace(mapping, preparation=preparation))
    for index in range(len(mappings), MAX_REFERENCE_COMPONENTS):
        if str(form.get(f"{prefix}_{index}_mode", "source")) != "source":
            raise ValueError("Save the matching rule before preparing its values")
    key_count = len(resolver.key_mappings)
    return replace(
        resolver,
        key_mappings=tuple(prepared[:key_count]),
        scope_mappings=tuple(prepared[key_count:]),
    )


def reference_preparation_editors(
    resolver: RelationshipResolver | None,
    key: Any,
    source: SourceDataset,
    sample_values: Mapping[str, object] | None,
) -> tuple[dict[str, Any], ...]:
    """Project saved instructions and an existing frozen sample without I/O."""

    mappings = (*resolver.key_mappings, *resolver.scope_mappings) if resolver else ()
    fields = tuple(item.target_field for item in mappings) or (
        (*key.key_fields, *key.scope_fields) if key else ("matching value",)
    )
    columns = {column.stable_key: column for column in source.columns}
    editors = []
    for index, target_field in enumerate(fields[:MAX_REFERENCE_COMPONENTS]):
        mapping = mappings[index] if index < len(mappings) else None
        preparation = mapping.preparation if mapping else None
        formula = preparation.formula if preparation else ""
        parts = preparation.parts if preparation else ()
        mode = "combine" if parts else "formula" if preparation else "source"
        bindings = preparation.bindings if preparation else ()
        available_bindings = dict(bindings)
        next_ordinal = max((column.ordinal for column in source.columns), default=0) + 1
        for column in source.columns:
            if column.stable_key in available_bindings.values():
                continue
            name = f"column_{column.ordinal}"
            while name in available_bindings:
                name = f"column_{next_ordinal}"
                next_ordinal += 1
            available_bindings[name] = column.stable_key
        preview = error = ""
        if mapping is not None and preparation is not None:
            try:
                validate_reference_preparation(mapping)
                result = (
                    prepare_reference_value(mapping, sample_values)
                    if sample_values is not None
                    else None
                )
                preview = "" if result is None else str(result)
            except (ValueError, TypeError, KeyError) as issue:
                error = str(issue)
        editors.append(
            {
                "index": index,
                "target_field": target_field,
                "mode": mode,
                "formula": formula,
                "bindings_json": json.dumps(tuple(available_bindings.items())),
                "parts_json": json.dumps(
                    [
                        {"source_column_key": part.source_column_key}
                        if part.source_column_key is not None
                        else {"literal": part.literal}
                        for part in parts
                    ]
                ),
                "parts": tuple(
                    {
                        "kind": "source"
                        if part.source_column_key is not None
                        else "literal",
                        "source": part.source_column_key or "",
                        "literal": part.literal or "",
                    }
                    for part in parts
                ),
                "preview": preview,
                "error": error,
                "sample_inputs": tuple(
                    (columns[key].source_name, sample_values.get(key))
                    for key in preparation_source_keys(mapping)
                    if key in columns
                )
                if mapping and preparation and sample_values is not None
                else (),
                "source_label": columns[mapping.source_column_key].source_name
                if mapping and mapping.source_column_key in columns
                else "the selected source value",
                "formula_columns": tuple(
                    (name, columns[value].source_name)
                    for name, value in available_bindings.items()
                    if value in columns
                ),
            }
        )
    return tuple(editors)


def reference_preparation_preview_row(
    source: SourceDataset,
    catalogs: Any,
) -> dict[str, object] | None:
    """Read one aligned captured row; never combine independent value samples."""

    binding = source.source
    if not isinstance(binding, FileSourceBinding):
        return None
    catalog = next(
        (
            item
            for item in catalogs
            if item.file_id == binding.file_id
            and item.source_sha256 == binding.source_sha256
            and item.content_hash == binding.catalog_hash
        ),
        None,
    )
    table = next(
        (
            item
            for item in (catalog.tables if catalog else ())
            if item.table_key == binding.table_key
        ),
        None,
    )
    if table is None or not table.preview_rows:
        return None
    row = table.preview_rows[0]
    return {
        column.stable_key: row[column.ordinal - 1]
        if 0 < column.ordinal <= len(row)
        else None
        for column in source.columns
    }
