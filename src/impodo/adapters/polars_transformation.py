"""Execute supported direct transformation programs with native Polars.

The adapter scans one hash-verified source Parquet snapshot, evaluates only
domain-compiled native expressions, and yields bounded ``PreparedRecord`` and
sparse transformation-impact batches.  It never calls Python from a Polars
expression, materializes the complete dataset, or changes row order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Iterator

import polars as pl

from ..domain.compiler.columnar_transformation import (
    ColumnarExpressionStep,
    ColumnarIdentityComponentProgram,
    ColumnarOperationKind,
    ColumnarScalarFieldProgram,
    ColumnarTransformationProgram,
)
from ..domain.source_snapshot import (
    EncodedSourceCell,
    SOURCE_ROW_COLUMN,
    SourceCellKind,
    SourceSnapshot,
    source_kind_column,
    source_value_column,
)
from ..domain.staging.transformation_impact import (
    TransformationImpactCounts,
    TransformationImpactRow,
    _display_value,
)
from ..models import Issue, PreparedRecord, canonical_json_bytes, portable_value
from ..source import SourceLoadError
from ..source_snapshot_io import validate_source_snapshot_path
from ..domain.staging.fields import synthetic_field


POLARS_TRANSFORMATION_BATCH_ROWS = 1_000
_ISSUE_COLUMN = "__impodo_columnar_issues"
_ERROR_REQUIRED = "__required__"
_ERROR_PREPARED_REQUIRED = "__prepared_required__"
_ERROR_PARSE = "__parse__"


@dataclass(frozen=True, slots=True)
class ColumnarTransformationBatch:
    """One bounded native result adapted to the current canonical boundary."""

    records: tuple[PreparedRecord, ...]
    impacts: tuple[TransformationImpactRow, ...]
    impact_counts: TransformationImpactCounts
    source_rows: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _ScalarLayout:
    field: ColumnarScalarFieldProgram
    prepared_alias: str
    value_alias: str
    fallback_alias: str | None


@dataclass(frozen=True, slots=True)
class _IdentityValueLayout:
    role: str
    error_index: int
    source_stable_key: str
    source_ordinal: int
    value_type: str
    normalized_alias: str
    value_alias: str


@dataclass(frozen=True, slots=True)
class _IdentityComponentLayout:
    component: ColumnarIdentityComponentProgram
    values: tuple[_IdentityValueLayout, ...]


@dataclass(frozen=True, slots=True)
class _ExecutionLayout:
    scalars: tuple[_ScalarLayout, ...]
    source_identity: tuple[_IdentityComponentLayout, ...]
    target_identity: tuple[_IdentityComponentLayout, ...]
    target_scope: tuple[_IdentityComponentLayout, ...]
    output_columns: tuple[str, ...]


def iter_polars_transformation_batches(
    path: str | Path,
    snapshot: SourceSnapshot,
    program: ColumnarTransformationProgram,
    *,
    batch_size: int = POLARS_TRANSFORMATION_BATCH_ROWS,
) -> Iterator[ColumnarTransformationBatch]:
    """Yield native direct results while proving count and source-row order."""

    if batch_size < 1:
        raise ValueError("Columnar transformation batch size must be positive")
    if (
        snapshot.dataset_id != program.dataset_id
        or snapshot.dataset_name != program.dataset_name
    ):
        raise SourceLoadError("Columnar program does not match the source snapshot")
    snapshot_path = validate_source_snapshot_path(path, snapshot)
    lazy, layout = _compile_lazy_transformation(snapshot_path, program)
    row_count = 0
    previous_source_row = 0
    for frame in lazy.collect_batches(
        chunk_size=batch_size,
        maintain_order=True,
        engine="streaming",
    ):
        if frame.height > batch_size:
            raise SourceLoadError("Columnar transformation batch exceeded its bound")
        batch = _adapt_frame(frame, program, layout)
        for source_row in batch.source_rows:
            if source_row <= previous_source_row:
                raise SourceLoadError("Columnar source row order is invalid")
            previous_source_row = source_row
        row_count += len(batch.source_rows)
        if row_count > snapshot.row_count:
            raise SourceLoadError("Columnar source row count is invalid")
        yield batch
    if row_count != snapshot.row_count:
        raise SourceLoadError("Columnar source row count is invalid")


def _compile_lazy_transformation(
    path: Path,
    program: ColumnarTransformationProgram,
) -> tuple[pl.LazyFrame, _ExecutionLayout]:
    raw_columns = [SOURCE_ROW_COLUMN]
    for item in program.inputs:
        raw_columns.extend(
            (source_value_column(item.ordinal), source_kind_column(item.ordinal))
        )
    lazy = pl.scan_parquet(
        path,
        glob=False,
        low_memory=True,
        rechunk=False,
        cache=False,
        parallel="auto",
    ).select(raw_columns)

    prepared_expressions: list[pl.Expr] = []
    value_expressions: list[pl.Expr] = []
    scalar_issue_expressions: list[pl.Expr] = []
    final_columns = list(raw_columns)
    scalar_layouts: list[_ScalarLayout] = []
    for index, field in enumerate(program.scalar_fields):
        layout, prepared, value, error = _compile_scalar(index, field)
        scalar_layouts.append(layout)
        prepared_expressions.append(prepared.alias(layout.prepared_alias))
        if layout.value_alias != layout.prepared_alias:
            value_expressions.append(value.alias(layout.value_alias))
        final_columns.append(layout.prepared_alias)
        if layout.value_alias != layout.prepared_alias:
            final_columns.append(layout.value_alias)
        if layout.fallback_alias is not None:
            prepared_expressions.append(
                _fallback_used_expression(field).alias(layout.fallback_alias)
            )
            final_columns.append(layout.fallback_alias)
        scalar_issue_expressions.append(_issue_expression("scalar", index, error))

    source_identity, source_prepared, source_values, source_issues = (
        _compile_identity_group(program.source_identity, "source_identity")
    )
    target_identity, target_prepared, target_values, target_issues = (
        _compile_identity_group(program.target_identity, "target_identity")
    )
    target_scope, scope_prepared, scope_values, scope_issues = (
        _compile_identity_group(program.target_scope, "target_scope")
    )
    for expressions in (source_prepared, target_prepared, scope_prepared):
        prepared_expressions.extend(expressions)
    for expressions in (source_values, target_values, scope_values):
        value_expressions.extend(expressions)
    for groups in (source_identity, target_identity, target_scope):
        for component in groups:
            for item in component.values:
                final_columns.append(item.normalized_alias)
                if item.value_alias != item.normalized_alias:
                    final_columns.append(item.value_alias)

    if prepared_expressions:
        lazy = lazy.with_columns(prepared_expressions)
    if value_expressions:
        lazy = lazy.with_columns(value_expressions)
    issue_expressions = [
        *source_issues,
        *scalar_issue_expressions,
        *target_issues,
        *scope_issues,
    ]
    lazy = lazy.with_columns(
        pl.concat_list(issue_expressions).list.drop_nulls().alias(_ISSUE_COLUMN)
    )
    final_columns.append(_ISSUE_COLUMN)
    return (
        lazy.select(final_columns),
        _ExecutionLayout(
            scalars=tuple(scalar_layouts),
            source_identity=source_identity,
            target_identity=target_identity,
            target_scope=target_scope,
            output_columns=tuple(final_columns),
        ),
    )


def _compile_scalar(
    index: int,
    field: ColumnarScalarFieldProgram,
) -> tuple[_ScalarLayout, pl.Expr, pl.Expr, pl.Expr]:
    prepared_alias = f"__impodo_scalar_prepared_{index:06d}"
    typed_alias = f"__impodo_scalar_value_{index:06d}"
    value_alias = (
        prepared_alias
        if field.conversion_step.operation
        in {
            ColumnarOperationKind.PARSE_INTEGER,
        }
        else typed_alias
    )
    fallback_alias = (
        f"__impodo_scalar_fallback_{index:06d}"
        if field.provider.operation is ColumnarOperationKind.SOURCE_FALLBACK
        else None
    )
    provider, matched = _provider_expression(field)
    transformed, output_too_long = _text_expression(
        provider,
        field.transform_steps,
    )
    mapped = _mapped_value_expression(field, matched)
    prepared = pl.when(matched).then(mapped).otherwise(transformed)
    output_too_long = (~matched) & output_too_long
    prepared_column = pl.col(prepared_alias)
    value, parse_invalid = _conversion_expression(
        prepared_column,
        field.conversion_step,
    )
    error = pl.when(output_too_long).then(
        pl.lit("SOURCE_RULE_OUTPUT_TOO_LONG")
    )
    if field.required_step is not None:
        error = error.when(prepared_column.is_null()).then(pl.lit(_ERROR_REQUIRED))
    error = error.when(parse_invalid).then(pl.lit(_ERROR_PARSE))
    typed_for_validation = (
        prepared_column
        if (
            field.conversion_step.operation is ColumnarOperationKind.PARSE_STRING
            or value_alias == prepared_alias
        )
        else pl.col(value_alias)
    )
    for step in field.validation_steps:
        invalid = _validation_invalid_expression(typed_for_validation, step)
        assert step.error_code is not None
        error = error.when(invalid).then(pl.lit(step.error_code))
    if (
        field.required_step is not None
        and field.conversion_step.operation is ColumnarOperationKind.PARSE_STRING
    ):
        error = error.when(prepared_column == "").then(
            pl.lit(_ERROR_PREPARED_REQUIRED)
        )
    error = error.otherwise(pl.lit(None, dtype=pl.String))
    return (
        _ScalarLayout(
            field=field,
            prepared_alias=prepared_alias,
            value_alias=value_alias,
            fallback_alias=fallback_alias,
        ),
        prepared,
        value,
        error,
    )


def _provider_expression(
    field: ColumnarScalarFieldProgram,
) -> tuple[pl.Expr, pl.Expr]:
    provider = field.provider
    raw = (
        pl.col(source_value_column(provider.source.ordinal))
        if provider.source is not None
        else pl.lit(None, dtype=pl.String)
    )
    if provider.operation is ColumnarOperationKind.READ_SOURCE:
        proposed = raw
    elif provider.operation is ColumnarOperationKind.USE_CONSTANT:
        proposed = _bound_string_literal(provider.literal_value)
    elif provider.operation is ColumnarOperationKind.SOURCE_FALLBACK:
        probe, _ = _text_expression(raw, provider.fallback_probe_steps)
        proposed = (
            pl.when(probe.is_null())
            .then(_bound_string_literal(provider.literal_value))
            .otherwise(probe)
        )
    else:
        raise ValueError(f"Unsupported native provider {provider.operation.value}")
    matched = pl.lit(False)
    if provider.value_mappings:
        choice = raw.str.strip_chars()
        for source_value, _target_value in provider.value_mappings:
            matched = matched | (choice == source_value)
    return proposed, matched.fill_null(False)


def _mapped_value_expression(
    field: ColumnarScalarFieldProgram,
    matched: pl.Expr,
) -> pl.Expr:
    provider = field.provider
    if not provider.value_mappings or provider.source is None:
        return pl.lit(None, dtype=pl.String)
    choice = pl.col(source_value_column(provider.source.ordinal)).str.strip_chars()
    mapped = pl.lit(None, dtype=pl.String)
    for source_value, target_value in provider.value_mappings:
        mapped = (
            pl.when(choice == source_value)
            .then(_bound_string_literal(target_value))
            .otherwise(mapped)
        )
    return pl.when(matched).then(mapped).otherwise(None)


def _fallback_used_expression(field: ColumnarScalarFieldProgram) -> pl.Expr:
    provider = field.provider
    assert provider.source is not None
    raw = pl.col(source_value_column(provider.source.ordinal))
    probe, _ = _text_expression(raw, provider.fallback_probe_steps)
    return probe.is_null()


def _text_expression(
    expression: pl.Expr,
    steps: tuple[ColumnarExpressionStep, ...],
) -> tuple[pl.Expr, pl.Expr]:
    result = expression
    output_too_long = pl.lit(False)
    for step in steps:
        operation = step.operation
        if operation is ColumnarOperationKind.RENDER_TEXT:
            continue
        if operation is ColumnarOperationKind.TRIM:
            result = result.str.strip_chars()
        elif operation is ColumnarOperationKind.COLLAPSE_WHITESPACE:
            result = result.str.replace_all(r"\s+", " ")
        elif operation is ColumnarOperationKind.REPLACE_LITERAL:
            assert step.text is not None and step.replacement is not None
            result = (
                result.str.replace_all(
                    step.text,
                    step.replacement,
                    literal=True,
                )
                if step.flag
                else result.str.replace(
                    step.text,
                    step.replacement,
                    literal=True,
                    n=1,
                )
            )
        elif operation is ColumnarOperationKind.CASE_UPPER:
            result = result.str.to_uppercase()
        elif operation is ColumnarOperationKind.CASE_LOWER:
            result = result.str.to_lowercase()
        elif operation is ColumnarOperationKind.EMPTY_AS_NULL:
            result = (
                pl.when(result == "")
                .then(pl.lit(None, dtype=pl.String))
                .otherwise(result)
            )
        elif operation is ColumnarOperationKind.VALIDATE_RULE_OUTPUT_LENGTH:
            assert step.integer is not None
            output_too_long = result.str.len_chars() > step.integer
        else:
            raise ValueError(f"Unsupported native text operation {operation.value}")
    return result, output_too_long.fill_null(False)


def _conversion_expression(
    prepared: pl.Expr,
    step: ColumnarExpressionStep,
) -> tuple[pl.Expr, pl.Expr]:
    operation = step.operation
    if operation is ColumnarOperationKind.PARSE_STRING:
        return (
            pl.when(prepared == "")
            .then(pl.lit(None, dtype=pl.String))
            .otherwise(prepared),
            pl.lit(False),
        )
    if operation is ColumnarOperationKind.PARSE_INTEGER:
        valid = prepared.str.contains(r"^[+-]?\d+$").fill_null(False)
        return (
            pl.when(valid).then(prepared).otherwise(pl.lit(None, dtype=pl.String)),
            prepared.is_not_null() & ~valid,
        )
    if operation is ColumnarOperationKind.PARSE_DECIMAL:
        normalized, valid = _decimal_expression(prepared, step.text)
        return (
            pl.when(valid)
            .then(normalized)
            .otherwise(pl.lit(None, dtype=pl.String)),
            prepared.is_not_null() & ~valid,
        )
    if operation is ColumnarOperationKind.PARSE_BOOLEAN:
        token = prepared.str.to_lowercase()
        true_value = token.is_in(["true", "1", "yes", "y"])
        false_value = token.is_in(["false", "0", "no", "n"])
        value = (
            pl.when(true_value)
            .then(pl.lit(True))
            .when(false_value)
            .then(pl.lit(False))
            .otherwise(pl.lit(None, dtype=pl.Boolean))
        )
        return value, prepared.is_not_null() & ~(true_value | false_value)
    if operation is ColumnarOperationKind.PARSE_DATE:
        formats = {
            "iso": "%Y-%m-%d",
            "dmy_slash": "%d/%m/%Y",
            "mdy_slash": "%m/%d/%Y",
            "dmy_dot": "%d.%m.%Y",
        }
        value = prepared.str.strptime(
            pl.Date,
            formats[step.text],
            strict=False,
            exact=True,
        )
        return value, prepared.is_not_null() & value.is_null()
    if operation is ColumnarOperationKind.PARSE_DATETIME:
        formats = {
            "dmy_slash": "%d/%m/%Y %H:%M:%S",
            "mdy_slash": "%m/%d/%Y %H:%M:%S",
            "dmy_dot": "%d.%m.%Y %H:%M:%S",
        }
        value = prepared.str.strptime(
            pl.Datetime,
            formats[step.text],
            strict=False,
            exact=True,
        ).dt.replace_time_zone("UTC")
        return value, prepared.is_not_null() & value.is_null()
    raise ValueError(f"Unsupported native conversion {operation.value}")


def _decimal_expression(
    prepared: pl.Expr,
    locale: str | None,
) -> tuple[pl.Expr, pl.Expr]:
    patterns = {
        "invariant": r"^[+-]?\d+(?:\.\d+)?$",
        "en_US": r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$",
        "de_DE": r"^[+-]?(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?$",
        "fr_FR": (
            r"^[+-]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:,\d+)?$"
        ),
    }
    if locale not in patterns:
        raise ValueError("Unsupported native decimal locale")
    valid = prepared.str.contains(patterns[locale]).fill_null(False)
    normalized = prepared
    if locale == "en_US":
        normalized = normalized.str.replace_all(",", "", literal=True)
    elif locale == "de_DE":
        normalized = normalized.str.replace_all(".", "", literal=True)
        normalized = normalized.str.replace_all(",", ".", literal=True)
    elif locale == "fr_FR":
        normalized = normalized.str.replace_all(r"[ \u00a0\u202f]", "")
        normalized = normalized.str.replace_all(",", ".", literal=True)
    return normalized, valid


def _validation_invalid_expression(
    value: pl.Expr,
    step: ColumnarExpressionStep,
) -> pl.Expr:
    if step.operation is ColumnarOperationKind.VALIDATE_EXACT_LENGTH:
        assert step.integer is not None
        return value.is_not_null() & (value.str.len_chars() != step.integer)
    if step.operation is ColumnarOperationKind.VALIDATE_CHARACTER_CLASS:
        assert step.character_class is not None
        assert step.segment_location is not None
        patterns = {
            "digits": r"^[0-9]+$",
            "uppercase": r"^[A-Z]+$",
            "lowercase": r"^[a-z]+$",
        }
        segment = value
        too_short = pl.lit(False)
        if step.segment_location in {"first", "last"}:
            assert step.segment_length is not None
            too_short = value.str.len_chars() < step.segment_length
            segment = value.str.slice(
                0 if step.segment_location == "first" else -step.segment_length,
                step.segment_length,
            )
        mismatch = ~segment.str.contains(patterns[step.character_class])
        return value.is_not_null() & (too_short | mismatch.fill_null(True))
    raise ValueError(f"Unsupported native validation {step.operation.value}")


def _compile_identity_group(
    components: tuple[ColumnarIdentityComponentProgram, ...],
    role: str,
) -> tuple[
    tuple[_IdentityComponentLayout, ...],
    list[pl.Expr],
    list[pl.Expr],
    list[pl.Expr],
]:
    layouts: list[_IdentityComponentLayout] = []
    normalized_expressions: list[pl.Expr] = []
    value_expressions: list[pl.Expr] = []
    issue_expressions: list[pl.Expr] = []
    error_index = 0
    for component_index, component in enumerate(components):
        component_values: list[_IdentityValueLayout] = []
        conversion = next(
            (
                step
                for step in component.normalization_steps
                if step.operation
                in {
                    ColumnarOperationKind.PARSE_STRING,
                    ColumnarOperationKind.PARSE_INTEGER,
                    ColumnarOperationKind.PARSE_DECIMAL,
                    ColumnarOperationKind.PARSE_BOOLEAN,
                    ColumnarOperationKind.PARSE_DATE,
                    ColumnarOperationKind.PARSE_DATETIME,
                }
            ),
            ColumnarExpressionStep(ColumnarOperationKind.PARSE_STRING),
        )
        text_steps = tuple(
            step
            for step in component.normalization_steps
            if step is not conversion
        )
        for source_index, source in enumerate(component.source_columns):
            normalized_alias = (
                f"__impodo_{role}_{component_index:04d}_{source_index:04d}_normalized"
            )
            typed_alias = (
                f"__impodo_{role}_{component_index:04d}_{source_index:04d}_value"
            )
            value_alias = (
                normalized_alias
                if conversion.operation
                in {
                    ColumnarOperationKind.PARSE_STRING,
                    ColumnarOperationKind.PARSE_INTEGER,
                }
                else typed_alias
            )
            raw = pl.col(source_value_column(source.ordinal))
            normalized, _ = _text_expression(raw, text_steps)
            normalized_expressions.append(normalized.alias(normalized_alias))
            value, parse_invalid = _conversion_expression(
                pl.col(normalized_alias),
                conversion,
            )
            if value_alias != normalized_alias:
                value_expressions.append(value.alias(value_alias))
            error = (
                pl.when(pl.col(normalized_alias).is_null())
                .then(pl.lit(_ERROR_REQUIRED))
                .when(parse_invalid)
                .then(pl.lit(_ERROR_PARSE))
                .otherwise(pl.lit(None, dtype=pl.String))
            )
            issue_expressions.append(_issue_expression(role, error_index, error))
            component_values.append(
                _IdentityValueLayout(
                    role=role,
                    error_index=error_index,
                    source_stable_key=source.stable_key,
                    source_ordinal=source.ordinal,
                    value_type=component.value_type,
                    normalized_alias=normalized_alias,
                    value_alias=value_alias,
                )
            )
            error_index += 1
        layouts.append(
            _IdentityComponentLayout(
                component=component,
                values=tuple(component_values),
            )
        )
    return (
        tuple(layouts),
        normalized_expressions,
        value_expressions,
        issue_expressions,
    )


def _issue_expression(kind: str, index: int, error: pl.Expr) -> pl.Expr:
    return (
        pl.when(error.is_not_null())
        .then(
            pl.struct(
                pl.lit(kind).alias("kind"),
                pl.lit(index, dtype=pl.Int32).alias("index"),
                error.alias("error"),
            )
        )
        .otherwise(pl.lit(None))
    )


def _bound_string_literal(value: str | None) -> pl.Expr:
    literal = pl.lit(value, dtype=pl.String)
    source_row = pl.col(SOURCE_ROW_COLUMN)
    return pl.when(source_row.is_not_null()).then(literal).otherwise(literal)


def _adapt_frame(
    frame: pl.DataFrame,
    program: ColumnarTransformationProgram,
    layout: _ExecutionLayout,
) -> ColumnarTransformationBatch:
    indexes = {name: index for index, name in enumerate(frame.columns)}
    records: list[PreparedRecord] = []
    impacts: list[TransformationImpactRow] = []
    counts = {
        "changed": 0,
        "fallback": 0,
        "null": 0,
        "invalid": 0,
        "provided": 0,
        "unchanged": 0,
    }
    source_rows: list[int] = []
    scalar_by_index = {index: item for index, item in enumerate(layout.scalars)}
    identity_by_kind = {
        "source_identity": _flatten_identity(layout.source_identity),
        "target_identity": _flatten_identity(layout.target_identity),
        "target_scope": _flatten_identity(layout.target_scope),
    }
    for row in frame.iter_rows(named=False):
        source_row = int(row[indexes[SOURCE_ROW_COLUMN]])
        source_rows.append(source_row)
        raw_by_ordinal = {
            item.ordinal: (
                row[indexes[source_value_column(item.ordinal)]],
                int(row[indexes[source_kind_column(item.ordinal)]]),
            )
            for item in program.inputs
        }
        errors = {
            (str(item["kind"]), int(item["index"])): str(item["error"])
            for item in row[indexes[_ISSUE_COLUMN]]
        }

        source_identity = tuple(
            _identity_value(row, indexes, item, errors)
            for item in identity_by_kind["source_identity"]
        )
        scalar_values = {
            item.field.target_field: _scalar_value(
                row,
                indexes,
                index,
                item,
                errors,
            )
            for index, item in scalar_by_index.items()
        }
        target_identity = tuple(
            _identity_value(row, indexes, item, errors)
            for item in identity_by_kind["target_identity"]
        )
        target_scope = tuple(
            _identity_value(row, indexes, item, errors)
            for item in identity_by_kind["target_scope"]
        )
        issues = _row_issues(
            row,
            indexes,
            source_row,
            program,
            layout,
            errors,
            raw_by_ordinal,
        )
        record = PreparedRecord(
            dataset=program.dataset_name,
            source_row=source_row,
            target_model=program.target_model,
            source_identity=source_identity,
            target_identity=target_identity,
            target_scope=target_scope,
            scalar_values=scalar_values,
            references={},
            source_trace_id="sha256:"
            + sha256(
                canonical_json_bytes(
                    {
                        "dataset": program.dataset_name,
                        "source_row": source_row,
                        "target_model": program.target_model,
                        "source_identity": portable_value(source_identity),
                    }
                )
            ).hexdigest(),
            issues=issues,
        )
        records.append(record)
        identity_impacts = _identity_impacts(
            row,
            indexes,
            source_row,
            program,
            layout,
            raw_by_ordinal,
        )
        impacts.extend(identity_impacts)
        counts["changed"] += len(identity_impacts)
        for index, scalar_layout in scalar_by_index.items():
            impact, outcome = _scalar_impact(
                row,
                indexes,
                source_row,
                index,
                scalar_layout,
                errors,
                raw_by_ordinal,
                program.dataset_name,
            )
            counts[outcome] += 1
            if impact is not None:
                impacts.append(impact)
    evaluated = sum(counts.values())
    return ColumnarTransformationBatch(
        records=tuple(records),
        impacts=tuple(impacts),
        impact_counts=TransformationImpactCounts(
            evaluated_count=evaluated,
            changed_count=counts["changed"],
            fallback_count=counts["fallback"],
            null_count=counts["null"],
            invalid_count=counts["invalid"],
            provided_count=counts["provided"],
            unchanged_count=counts["unchanged"],
        ),
        source_rows=tuple(source_rows),
    )


def _flatten_identity(
    components: tuple[_IdentityComponentLayout, ...],
) -> tuple[_IdentityValueLayout, ...]:
    return tuple(item for component in components for item in component.values)


def _identity_value(
    row: tuple[object, ...],
    indexes: dict[str, int],
    layout: _IdentityValueLayout,
    errors: dict[tuple[str, int], str],
):
    if (layout.role, layout.error_index) in errors:
        return None
    value = row[indexes[layout.value_alias]]
    return _canonical_adapter_value(value, layout.value_type)


def _scalar_value(
    row: tuple[object, ...],
    indexes: dict[str, int],
    index: int,
    layout: _ScalarLayout,
    errors: dict[tuple[str, int], str],
):
    if ("scalar", index) in errors:
        return None
    value = row[indexes[layout.value_alias]]
    return _canonical_adapter_value(value, layout.field.value_type)


def _canonical_adapter_value(value: object, value_type: str):
    if value is None:
        return None
    if value_type == "integer":
        return int(str(value), 10)
    if value_type == "decimal":
        return Decimal(str(value))
    return value


def _row_issues(
    row: tuple[object, ...],
    indexes: dict[str, int],
    source_row: int,
    program: ColumnarTransformationProgram,
    layout: _ExecutionLayout,
    errors: dict[tuple[str, int], str],
    raw_by_ordinal: dict[int, tuple[object, int]],
) -> tuple[Issue, ...]:
    issues: list[Issue] = []
    for item in _flatten_identity(layout.source_identity):
        error = errors.get((item.role, item.error_index))
        if error is not None:
            issues.append(
                Issue(
                    code="SOURCE_IDENTITY_INVALID",
                    message=_identity_error_message(
                        error,
                        item,
                        raw_by_ordinal[item.source_ordinal],
                    ),
                    dataset=program.dataset_name,
                    row=source_row,
                    field=item.source_stable_key,
                )
            )
    for index, scalar in enumerate(layout.scalars):
        error = errors.get(("scalar", index))
        if error is None:
            continue
        issue_code, issue_message, _impact_message = _scalar_error_messages(
            error,
            scalar.field,
            row[indexes[scalar.prepared_alias]],
        )
        issues.append(
            Issue(
                code=issue_code,
                message=issue_message,
                dataset=program.dataset_name,
                row=source_row,
                field=synthetic_field(scalar.field.output_ordinal),
            )
        )
    for groups in (layout.target_identity, layout.target_scope):
        for item in _flatten_identity(groups):
            error = errors.get((item.role, item.error_index))
            if error is not None:
                issues.append(
                    Issue(
                        code="SOURCE_IDENTITY_INVALID",
                        message=_identity_error_message(
                            error,
                            item,
                            raw_by_ordinal[item.source_ordinal],
                        ),
                        dataset=program.dataset_name,
                        row=source_row,
                        field=item.source_stable_key,
                    )
                )
    return tuple(issues)


def _identity_error_message(
    error: str,
    layout: _IdentityValueLayout,
    raw: tuple[object, int],
) -> str:
    if error == _ERROR_REQUIRED:
        return "required value is empty"
    if error == _ERROR_PARSE:
        return f"cannot parse {_source_repr(*raw)} as {layout.value_type}"
    raise ValueError("Unsupported native identity error")


def _scalar_error_messages(
    error: str,
    field: ColumnarScalarFieldProgram,
    prepared: object,
) -> tuple[str, str, str]:
    if error in {_ERROR_REQUIRED, _ERROR_PREPARED_REQUIRED}:
        return (
            "SOURCE_REQUIRED_VALUE_MISSING",
            "required value is empty",
            "Required value is empty after transformation",
        )
    if error == _ERROR_PARSE:
        return (
            "SOURCE_TYPE_INVALID",
            f"cannot parse '__impodo_invalid_value__' as {field.value_type}",
            f"Cannot parse {prepared!r} as {field.value_type}.",
        )
    if error == "SOURCE_RULE_OUTPUT_TOO_LONG":
        message = "A value rule produced more than 1000000 characters"
        return error, message, message
    if error == "SOURCE_TEXT_LENGTH_INVALID":
        step = next(
            item
            for item in field.validation_steps
            if item.operation is ColumnarOperationKind.VALIDATE_EXACT_LENGTH
        )
        message = (
            f"Expected exactly {step.integer} characters; "
            f"found {len(str(prepared))}"
        )
        return error, message, message
    if error == "SOURCE_TEXT_SEGMENT_INVALID":
        step = next(
            item
            for item in field.validation_steps
            if item.operation is ColumnarOperationKind.VALIDATE_CHARACTER_CLASS
        )
        assert step.segment_location is not None
        assert step.character_class is not None
        rendered = str(prepared)
        if (
            step.segment_location in {"first", "last"}
            and step.segment_length is not None
            and len(rendered) < step.segment_length
        ):
            message = (
                f"Expected at least {step.segment_length} characters to check "
                f"the {step.segment_location} part"
            )
        else:
            label = {
                "digits": "digits 0-9",
                "uppercase": "capital letters A-Z",
                "lowercase": "lowercase letters a-z",
            }[step.character_class]
            area = {
                "entire": "The whole value",
                "first": f"The first {step.segment_length} characters",
                "last": f"The last {step.segment_length} characters",
            }[step.segment_location]
            message = f"{area} must contain only {label}"
        return error, message, message
    raise ValueError("Unsupported native scalar error")


def _identity_impacts(
    row: tuple[object, ...],
    indexes: dict[str, int],
    source_row: int,
    program: ColumnarTransformationProgram,
    layout: _ExecutionLayout,
    raw_by_ordinal: dict[int, tuple[object, int]],
) -> tuple[TransformationImpactRow, ...]:
    impacts: list[TransformationImpactRow] = []
    for component_layout in (*layout.target_identity, *layout.target_scope):
        raw_values = tuple(
            _source_display(*raw_by_ordinal[item.source_ordinal])
            for item in component_layout.values
        )
        proposed_values = tuple(
            _display_value(row[indexes[item.normalized_alias]])
            for item in component_layout.values
        )
        if raw_values == proposed_values:
            continue
        raw_display = " | ".join(raw_values)
        proposed_display = " | ".join(proposed_values)
        for target_field in component_layout.component.target_fields:
            impacts.append(
                TransformationImpactRow(
                    dataset=program.dataset_name,
                    source_row=source_row,
                    source_column=component_layout.component.source_label,
                    target_field=target_field,
                    raw_value=raw_display,
                    proposed_value=proposed_display,
                    rules="Identity preparation",
                    outcome="changed",
                )
            )
    return tuple(impacts)


def _scalar_impact(
    row: tuple[object, ...],
    indexes: dict[str, int],
    source_row: int,
    index: int,
    layout: _ScalarLayout,
    errors: dict[tuple[str, int], str],
    raw_by_ordinal: dict[int, tuple[object, int]],
    dataset_name: str,
) -> tuple[TransformationImpactRow | None, str]:
    field = layout.field
    raw = (
        raw_by_ordinal[field.provider.source.ordinal]
        if field.provider.source is not None
        else (None, int(SourceCellKind.NULL))
    )
    raw_display = _source_display(*raw)
    error = errors.get(("scalar", index))
    if error is not None and error != _ERROR_PREPARED_REQUIRED:
        _issue_code, _issue_message, message = _scalar_error_messages(
            error,
            field,
            row[indexes[layout.prepared_alias]],
        )
        outcome = "invalid"
        proposed_display = "Invalid"
    else:
        impact_value_alias = (
            layout.prepared_alias
            if field.conversion_step.operation
            is ColumnarOperationKind.PARSE_STRING
            else layout.value_alias
        )
        proposed = _canonical_adapter_value(
            row[indexes[impact_value_alias]],
            field.value_type,
        )
        proposed_display = _display_value(proposed)
        message = ""
        if field.provider.operation is ColumnarOperationKind.USE_CONSTANT:
            outcome = "provided"
        elif (
            layout.fallback_alias is not None
            and bool(row[indexes[layout.fallback_alias]])
        ):
            outcome = "fallback"
        elif proposed is None and raw[1] != int(SourceCellKind.NULL):
            outcome = "null"
        elif raw_display != proposed_display:
            outcome = "changed"
        else:
            outcome = "unchanged"
    if outcome == "unchanged":
        return None, outcome
    return (
        TransformationImpactRow(
            dataset=dataset_name,
            source_row=source_row,
            source_column=field.source_label,
            target_field=field.target_field,
            raw_value=raw_display,
            proposed_value=proposed_display,
            rules=field.transformation_rules,
            outcome=outcome,
            message=message,
        ),
        outcome,
    )


def _source_display(text: object, kind_value: int) -> str:
    kind = SourceCellKind(kind_value)
    if kind is SourceCellKind.NULL:
        return "—"
    if text is None:
        raise SourceLoadError("Columnar source value is missing its text")
    rendered = str(text)
    if kind is SourceCellKind.DECIMAL:
        return format(Decimal(rendered), "f")
    if kind is SourceCellKind.DATETIME:
        parsed = datetime.fromisoformat(rendered)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return rendered


def _source_repr(text: object, kind_value: int) -> str:
    cell = EncodedSourceCell(
        kind=SourceCellKind(kind_value),
        text=(str(text) if text is not None else None),
    )
    return repr(cell.to_python())


__all__ = [
    "POLARS_TRANSFORMATION_BATCH_ROWS",
    "ColumnarTransformationBatch",
    "iter_polars_transformation_batches",
]
