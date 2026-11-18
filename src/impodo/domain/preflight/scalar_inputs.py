"""Conservative static scalar proof for rows without saved executed readsets.

Current preparation records actual provider reads in ``field_input_keys``.
When a saved row has no per-target proof, its declared scalar lineage names
only the authored anchor, so static formula proof requires every input to
resolve to that anchor without a conditional read.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from impodo.domain.mapping.contracts import ScalarFieldMapping, ScalarValueSource
from impodo.domain.recipe.value_rules import formula_input_names, validate_formula
from impodo.domain.workspace.contracts import SourceDatasetColumn


def scalar_formula_aliases(
    columns: Sequence[SourceDatasetColumn],
) -> Mapping[str, str] | None:
    """Build one unambiguous ordinal-to-key formula context for a frozen dataset."""

    if any(
        type(column.ordinal) is not int
        or column.ordinal < 1
        or not isinstance(column.stable_key, str)
        or not column.stable_key.strip()
        for column in columns
    ):
        return None
    if len({column.ordinal for column in columns}) != len(columns) or len(
        {column.stable_key for column in columns}
    ) != len(columns):
        return None
    return MappingProxyType(
        {f"column_{column.ordinal}": column.stable_key for column in columns}
    )


def scalar_original_inputs(
    field: ScalarFieldMapping,
    *,
    source_keys: frozenset[str],
    formula_aliases: Mapping[str, str] | None,
) -> tuple[str, ...] | None:
    """Provide a static fallback when executed per-target proof is absent.

    Direct providers and eager single-anchor formulas are provable from declared
    lineage alone. Other providers require saved executed readsets. Validation
    never evaluates a source value.
    """

    anchor = field.source_column_key
    if (
        field.value_source
        not in {ScalarValueSource.SOURCE, ScalarValueSource.SOURCE_WITH_FALLBACK}
        or anchor is None
        or field.concatenation is not None
        or field.selection_rules is not None
        or field.reference_lookup is not None
    ):
        return None
    if not field.transform.formula.strip():
        return (anchor,)
    if anchor not in source_keys or field.value_mappings or formula_aliases is None:
        return None
    try:
        allowed_names = {"value", *formula_aliases}
        parsed = validate_formula(field.transform.formula, allowed_names=allowed_names)
        if any(
            isinstance(node, (ast.IfExp, ast.BoolOp))
            or isinstance(node, ast.Compare)
            and len(node.ops) > 1
            for node in ast.walk(parsed)
        ):
            return None
        names = formula_input_names(
            field.transform.formula, allowed_names=allowed_names
        )
    except (TypeError, ValueError):
        return None
    if not names or any(
        (anchor if name == "value" else formula_aliases.get(name)) != anchor
        for name in names
    ):
        return None
    return (anchor,)
