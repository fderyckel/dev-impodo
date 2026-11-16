"""Project prepared reference text through native Polars expressions."""

from __future__ import annotations

from collections.abc import Callable

from impodo.application.shared.columnar_runtime import configure_columnar_runtime
from impodo.domain.compiler.reference_expression import (
    ColumnarReferencePreparation,
    ReferenceTextExpression,
)

configure_columnar_runtime()

import polars as pl


def prepared_reference_expressions(
    preparation: ColumnarReferencePreparation,
    source: Callable[[str], pl.Expr],
) -> tuple[pl.Expr, pl.Expr]:
    """Return a calculated value and its bounded, row-local failure message.

    Callers supply their own source-column addressing. This lets Stage 3
    distinct-value checks and preparation use the same native semantics.
    """

    value = _text_expression(preparation.expression, source)
    missing = [
        source(key).is_null() | (source(key).str.strip_chars() == "")
        for key in preparation.source_column_keys
    ]
    all_blank = pl.all_horizontal(missing) if missing else pl.lit(False)
    partial_blank = (
        (pl.any_horizontal(missing) & ~all_blank) if missing else pl.lit(False)
    )
    error = (
        pl.when(partial_blank)
        .then(pl.lit("A source value needed for this reference is blank"))
        .when(~all_blank & (value.str.strip_chars() == ""))
        .then(pl.lit("The prepared reference is blank"))
        .when(~all_blank & (value.str.len_chars() > 10_000))
        .then(pl.lit("The prepared reference is too long"))
        .otherwise(pl.lit(None, dtype=pl.String))
    )
    return pl.when(all_blank | error.is_not_null()).then(None).otherwise(value), error


def _text_expression(
    expression: ReferenceTextExpression, source: Callable[[str], pl.Expr]
) -> pl.Expr:
    operation = expression.operation
    if operation == "source":
        if expression.text is None:
            raise ValueError("A compiled reference source requires its column key")
        return source(expression.text)
    if operation == "literal":
        return pl.lit(expression.text, dtype=pl.String)
    values = [_text_expression(item, source) for item in expression.arguments]
    if operation == "concat":
        return (
            pl.concat_str(values, separator="", ignore_nulls=True)
            if values
            else pl.lit("")
        )
    if operation == "strip":
        return values[0].str.strip_chars()
    if operation == "upper":
        return values[0].str.to_uppercase()
    if operation == "lower":
        return values[0].str.to_lowercase()
    raise ValueError("Unsupported compiled reference text operation")
