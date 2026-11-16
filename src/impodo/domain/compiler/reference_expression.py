"""Describe the native text subset of a prepared related-record reference."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from impodo.domain.mapping.contracts import ReferenceKeyMapping
from impodo.domain.recipe.value_rules import validate_formula


class UnsupportedReferenceExpression(ValueError):
    """A safe expression requires the bounded reference interpreter."""


@dataclass(frozen=True, slots=True)
class ReferenceTextExpression:
    """One text operation whose inputs retain their source representations."""

    operation: str
    text: str | None = None
    arguments: tuple[ReferenceTextExpression, ...] = ()

    @classmethod
    def from_portable_dict(
        cls, payload: Mapping[str, object]
    ) -> ReferenceTextExpression:
        """Restore a compiler-owned expression tree from saved native evidence."""

        return cls(
            operation=str(payload["operation"]),
            text=str(payload["text"]) if payload.get("text") is not None else None,
            arguments=tuple(
                cls.from_portable_dict(cast(Mapping[str, object], item))
                for item in cast(Sequence[object], payload.get("arguments", ()))
            ),
        )


@dataclass(frozen=True, slots=True)
class ColumnarReferencePreparation:
    """A native reference value and the actual source inputs it requires."""

    expression: ReferenceTextExpression
    source_column_keys: tuple[str, ...]

    @classmethod
    def from_portable_dict(
        cls, payload: Mapping[str, object]
    ) -> ColumnarReferencePreparation:
        """Restore one prepared-reference projection for a native reader."""

        return cls(
            expression=ReferenceTextExpression.from_portable_dict(
                cast(Mapping[str, object], payload["expression"])
            ),
            source_column_keys=tuple(
                str(item)
                for item in cast(Sequence[object], payload["source_column_keys"])
            ),
        )


def compile_reference_preparation(
    mapping: ReferenceKeyMapping,
) -> ColumnarReferencePreparation | None:
    """Compile text joining and case cleanup without choosing a table backend.

    The regular safe formula validator remains authoritative. Expressions
    outside the proven native text subset explicitly request bounded fallback.
    """

    preparation = mapping.preparation
    if preparation is None:
        return None
    if preparation.parts:
        parts = tuple(
            ReferenceTextExpression(
                "source" if part.source_column_key is not None else "literal",
                part.source_column_key
                if part.source_column_key is not None
                else part.literal,
            )
            for part in preparation.parts
        )
        expression = ReferenceTextExpression("concat", arguments=parts)
    else:
        bindings = {"value": mapping.source_column_key, **dict(preparation.bindings)}
        parsed = validate_formula(preparation.formula, allowed_names=set(bindings))
        expression = _compile_node(parsed.body, bindings)
    inputs = tuple(dict.fromkeys(_source_keys(expression)))
    return ColumnarReferencePreparation(expression, inputs)


def _compile_node(
    node: ast.AST, bindings: Mapping[str, str]
) -> ReferenceTextExpression:
    if isinstance(node, ast.Name) and node.id in bindings:
        return ReferenceTextExpression("source", bindings[node.id])
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return ReferenceTextExpression("literal", node.value)
    # Unlike concat(), '+' rejects mixed numeric and text operands. Source
    # snapshots keep these types separately; do not silently stringify them.
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        if (
            name == "concat"
            or name in {"strip", "upper", "lower"}
            and len(node.args) == 1
        ):
            return ReferenceTextExpression(
                name,
                arguments=tuple(_compile_node(item, bindings) for item in node.args),
            )
    raise UnsupportedReferenceExpression(
        "This reference formula requires the bounded interpreter"
    )


def _source_keys(expression: ReferenceTextExpression) -> tuple[str, ...]:
    if expression.operation == "source":
        if expression.text is None:
            raise ValueError("A compiled reference source requires its column key")
        return (expression.text,)
    return tuple(
        key for argument in expression.arguments for key in _source_keys(argument)
    )
