"""Build sparse correction candidates from native prepared Parquet intents.

Both prepared artifacts are scanned once with Polars.  Typed output columns
are compared with null-aware expressions, then only changed fields are written
to a transient long-form Parquet artifact.  Python adapts those sparse rows at
the application boundary; it never classifies every source row and never
hashes individual rows or values.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Iterator, Mapping

from .polars_transformation import (
    PREPARED_ISSUE_COLUMN,
    PREPARED_PARQUET_COMPRESSION,
    PREPARED_PARQUET_ROW_GROUP_ROWS,
    PreparedIntentColumn,
    prepared_intent_columns,
    validate_prepared_intent_path,
)
from ..domain.compiler.columnar_transformation import ColumnarTransformationProgram
from ..domain.correction import CorrectionCandidate, CorrectionValueKind
from ..domain.prepared_snapshot import PreparedSnapshot
from ..domain.source_snapshot import SOURCE_ROW_COLUMN
from impodo.application.shared.columnar_runtime import configure_columnar_runtime


configure_columnar_runtime()

import polars as pl  # noqa: E402


CORRECTION_CANDIDATE_BATCH_ROWS = 1_000
_FIELD_INDEX_COLUMN = "__impodo_correction_field_index"
_PREVIOUS_INVALID_COLUMN = "__impodo_previous_intent_invalid"
_CORRECTED_INVALID_COLUMN = "__impodo_corrected_intent_invalid"
_PAYLOAD_COLUMN = "__impodo_correction_intent_json"
_CANDIDATE_COLUMNS = (
    SOURCE_ROW_COLUMN,
    _FIELD_INDEX_COLUMN,
    _PREVIOUS_INVALID_COLUMN,
    _CORRECTED_INVALID_COLUMN,
    _PAYLOAD_COLUMN,
)


class CorrectionComparisonError(ValueError):
    """Raised when prepared intents cannot be compared safely."""


@dataclass(frozen=True, slots=True)
class CorrectionCandidateArtifact:
    """Describe one transient sparse candidate artifact."""

    path: Path
    dataset_name: str
    target_model: str
    fields: tuple[PreparedIntentColumn, ...]
    candidate_count: int
    candidate_counts_by_field: tuple[tuple[str, int], ...]


def write_polars_correction_candidates(
    previous_path: str | Path,
    previous_snapshot: PreparedSnapshot,
    previous_program: ColumnarTransformationProgram,
    corrected_path: str | Path,
    corrected_snapshot: PreparedSnapshot,
    corrected_program: ColumnarTransformationProgram,
    destination: str | Path,
) -> CorrectionCandidateArtifact:
    """Write sparse A-to-C differences without materializing source rows."""

    _validate_comparison_scope(
        previous_snapshot,
        previous_program,
        corrected_snapshot,
        corrected_program,
    )
    previous_artifact = validate_prepared_intent_path(
        previous_path,
        previous_snapshot,
        previous_program,
    )
    corrected_artifact = validate_prepared_intent_path(
        corrected_path,
        corrected_snapshot,
        corrected_program,
    )
    previous_by_field = {
        item.target_field: item for item in prepared_intent_columns(previous_program)
    }
    corrected_by_field = {
        item.target_field: item for item in prepared_intent_columns(corrected_program)
    }
    if set(previous_by_field) != set(corrected_by_field):
        raise CorrectionComparisonError(
            "Correction must preserve the writable target-field scope"
        )
    fields = tuple(previous_by_field[name] for name in sorted(previous_by_field))
    if not fields:
        raise CorrectionComparisonError(
            "Correction has no writable prepared fields to compare"
        )
    for field in fields:
        corrected = corrected_by_field[field.target_field]
        if (
            field.value_kind is not corrected.value_kind
            or field.value_type != corrected.value_type
            or len(field.value_aliases) != len(corrected.value_aliases)
            or field.relationship_model != corrected.relationship_model
            or field.relationship_key_fields
            != corrected.relationship_key_fields
            or field.relationship_scope_fields
            != corrected.relationship_scope_fields
        ):
            raise CorrectionComparisonError(
                "Correction changes the contract of target field "
                f"{field.target_field!r}"
            )

    target = Path(destination)
    if target.is_symlink() or target.exists() or not target.parent.is_dir():
        raise CorrectionComparisonError("Correction candidate destination is invalid")
    target = target.resolve()
    previous_lazy = _intent_projection(
        previous_artifact,
        fields,
        prefix="previous",
        issue_alias=_PREVIOUS_INVALID_COLUMN,
    )
    corrected_fields = tuple(
        corrected_by_field[field.target_field] for field in fields
    )
    corrected_lazy = _intent_projection(
        corrected_artifact,
        corrected_fields,
        prefix="corrected",
        issue_alias=_CORRECTED_INVALID_COLUMN,
    )
    joined = previous_lazy.join(
        corrected_lazy,
        on=SOURCE_ROW_COLUMN,
        how="inner",
        validate="1:1",
        maintain_order="left",
    )
    payload_aliases: list[str] = []
    payload_expressions: list[pl.Expr] = []
    for index, (previous, corrected) in enumerate(
        zip(fields, corrected_fields, strict=True)
    ):
        alias = f"{index:06d}"
        payload_aliases.append(alias)
        previous_aliases = _projected_aliases("previous", index, previous)
        corrected_aliases = _projected_aliases("corrected", index, corrected)
        equal = pl.lit(True)
        for previous_alias, corrected_alias in zip(
            previous_aliases,
            corrected_aliases,
            strict=True,
        ):
            equal = equal & pl.col(previous_alias).eq_missing(
                pl.col(corrected_alias)
            )
        payload_expressions.append(
            pl.when(~equal)
            .then(
                pl.struct(
                    _intent_json_expression(previous_aliases, previous).alias(
                        "previous"
                    ),
                    _intent_json_expression(corrected_aliases, corrected).alias(
                        "corrected"
                    ),
                ).struct.json_encode()
            )
            .otherwise(pl.lit(None, dtype=pl.String))
            .alias(alias)
        )
    candidates = (
        joined.with_columns(payload_expressions)
        .unpivot(
            on=payload_aliases,
            index=(
                SOURCE_ROW_COLUMN,
                _PREVIOUS_INVALID_COLUMN,
                _CORRECTED_INVALID_COLUMN,
            ),
            variable_name=_FIELD_INDEX_COLUMN,
            value_name=_PAYLOAD_COLUMN,
        )
        .filter(pl.col(_PAYLOAD_COLUMN).is_not_null())
        .with_columns(pl.col(_FIELD_INDEX_COLUMN).cast(pl.UInt32))
        .select(_CANDIDATE_COLUMNS)
    )
    try:
        candidates.sink_parquet(
            target,
            compression=PREPARED_PARQUET_COMPRESSION,
            statistics=True,
            row_group_size=PREPARED_PARQUET_ROW_GROUP_ROWS,
            maintain_order=True,
            mkdir=False,
            engine="streaming",
        )
        counts = (
            pl.scan_parquet(target, glob=False, cache=False, rechunk=False)
            .group_by(_FIELD_INDEX_COLUMN)
            .len()
            .collect(engine="streaming")
        )
    except (OSError, pl.exceptions.PolarsError) as error:
        raise CorrectionComparisonError(
            "Correction candidates could not be written"
        ) from error
    counts_by_index = {
        int(field_index): int(count)
        for field_index, count in counts.iter_rows(named=False)
    }
    counts_by_field = tuple(
        (field.target_field, counts_by_index.get(index, 0))
        for index, field in enumerate(fields)
    )
    return CorrectionCandidateArtifact(
        path=target,
        dataset_name=previous_program.dataset_name,
        target_model=previous_program.target_model,
        fields=fields,
        candidate_count=sum(counts_by_index.values()),
        candidate_counts_by_field=counts_by_field,
    )


def iter_polars_correction_candidate_batches(
    artifact: CorrectionCandidateArtifact,
    *,
    batch_size: int = CORRECTION_CANDIDATE_BATCH_ROWS,
) -> Iterator[tuple[CorrectionCandidate, ...]]:
    """Adapt sparse candidates in bounded batches after native comparison."""

    if batch_size < 1:
        raise ValueError("Correction candidate batch size must be positive")
    path = artifact.path
    if path.is_symlink() or not path.resolve().is_file():
        raise CorrectionComparisonError("Correction candidate artifact is unavailable")
    try:
        schema = pl.read_parquet_schema(path.resolve())
    except Exception as error:
        raise CorrectionComparisonError(
            "Correction candidate schema is unreadable"
        ) from error
    if tuple(schema) != _CANDIDATE_COLUMNS:
        raise CorrectionComparisonError("Correction candidate schema is invalid")
    try:
        batches = pl.scan_parquet(
            path.resolve(),
            glob=False,
            low_memory=True,
            rechunk=False,
            cache=False,
            parallel="none",
        ).collect_batches(
            chunk_size=batch_size,
            maintain_order=True,
            engine="streaming",
        )
        for frame in batches:
            candidates: list[CorrectionCandidate] = []
            for row in frame.iter_rows(named=True):
                if bool(row[_PREVIOUS_INVALID_COLUMN]) or bool(
                    row[_CORRECTED_INVALID_COLUMN]
                ):
                    raise CorrectionComparisonError(
                        "A changed prepared intent contains validation issues"
                    )
                field_index = int(row[_FIELD_INDEX_COLUMN])
                if not 0 <= field_index < len(artifact.fields):
                    raise CorrectionComparisonError(
                        "Correction candidate field index is invalid"
                    )
                field = artifact.fields[field_index]
                payload = _read_payload(str(row[_PAYLOAD_COLUMN]))
                candidates.append(
                    CorrectionCandidate(
                        dataset=artifact.dataset_name,
                        source_row=int(row[SOURCE_ROW_COLUMN]),
                        target_model=artifact.target_model,
                        target_field=field.target_field,
                        value_kind=field.value_kind,
                        previous=_restore_intent(payload["previous"], field),
                        corrected=_restore_intent(payload["corrected"], field),
                        relationship_model=field.relationship_model,
                        relationship_key_fields=field.relationship_key_fields,
                        relationship_scope_fields=field.relationship_scope_fields,
                    )
                )
            if candidates:
                yield tuple(candidates)
    except CorrectionComparisonError:
        raise
    except (OSError, pl.exceptions.PolarsError) as error:
        raise CorrectionComparisonError(
            "Correction candidate artifact is unreadable"
        ) from error


def _validate_comparison_scope(
    previous_snapshot: PreparedSnapshot,
    previous_program: ColumnarTransformationProgram,
    corrected_snapshot: PreparedSnapshot,
    corrected_program: ColumnarTransformationProgram,
) -> None:
    if (
        previous_snapshot.dataset_id != corrected_snapshot.dataset_id
        or previous_snapshot.dataset_name != corrected_snapshot.dataset_name
        or previous_snapshot.source_snapshot_hash
        != corrected_snapshot.source_snapshot_hash
        or previous_snapshot.row_count != corrected_snapshot.row_count
        or previous_program.dataset_id != corrected_program.dataset_id
        or previous_program.dataset_name != corrected_program.dataset_name
        or previous_program.target_model != corrected_program.target_model
        or previous_program.target_mode != corrected_program.target_mode
        or previous_program.source_selection_hash
        != corrected_program.source_selection_hash
        or previous_program.source_identity != corrected_program.source_identity
        or previous_program.target_identity != corrected_program.target_identity
        or previous_program.target_scope != corrected_program.target_scope
    ):
        raise CorrectionComparisonError(
            "Correction must preserve source rows, target model, and record identity"
        )


def _intent_projection(
    path: Path,
    fields: tuple[PreparedIntentColumn, ...],
    *,
    prefix: str,
    issue_alias: str,
) -> pl.LazyFrame:
    expressions: list[pl.Expr] = [pl.col(SOURCE_ROW_COLUMN)]
    for index, field in enumerate(fields):
        for component_index, (alias, projected) in enumerate(
            zip(
                field.value_aliases,
                _projected_aliases(prefix, index, field),
                strict=True,
            )
        ):
            value = pl.col(alias)
            if component_index == 0 and field.value_mappings:
                value = value.replace_strict(
                    [item[0] for item in field.value_mappings],
                    [item[1] for item in field.value_mappings],
                    default=value,
                )
            expressions.append(value.alias(projected))
    expressions.append(
        (pl.col(PREPARED_ISSUE_COLUMN).list.len() > 0).alias(issue_alias)
    )
    return pl.scan_parquet(
        path,
        glob=False,
        low_memory=True,
        rechunk=False,
        cache=False,
        parallel="auto",
    ).select(expressions)


def _projected_aliases(
    prefix: str,
    field_index: int,
    field: PreparedIntentColumn,
) -> tuple[str, ...]:
    return tuple(
        f"__impodo_{prefix}_{field_index:06d}_{component_index:06d}"
        for component_index in range(len(field.value_aliases))
    )


def _intent_json_expression(
    aliases: tuple[str, ...],
    field: PreparedIntentColumn,
) -> pl.Expr:
    if field.value_kind is CorrectionValueKind.SCALAR:
        return pl.col(aliases[0])
    return pl.struct(
        *(
            pl.col(alias).alias(f"component_{index:06d}")
            for index, alias in enumerate(aliases)
        )
    )


def _read_payload(value: str) -> Mapping[str, object]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise CorrectionComparisonError(
            "Correction candidate payload is invalid"
        ) from error
    if not isinstance(payload, dict) or set(payload) != {"previous", "corrected"}:
        raise CorrectionComparisonError("Correction candidate payload is invalid")
    return payload


def _restore_intent(value: object, field: PreparedIntentColumn):
    if field.value_kind is CorrectionValueKind.SCALAR:
        return _restore_typed_value(value, field.value_type)
    if not isinstance(value, dict) or len(value) != len(field.value_aliases):
        raise CorrectionComparisonError(
            "Correction relationship payload is invalid"
        )
    return tuple(
        _restore_typed_value(value.get(f"component_{index:06d}"), field.value_type)
        for index in range(len(field.value_aliases))
    )


def _restore_typed_value(value: object, value_type: str):
    if value is None:
        return None
    try:
        if value_type == "string":
            return str(value)
        if value_type == "integer":
            return int(value)
        if value_type == "decimal":
            return Decimal(str(value))
        if value_type == "boolean":
            if not isinstance(value, bool):
                raise TypeError
            return value
        if value_type == "date":
            return date.fromisoformat(str(value))
        if value_type == "datetime":
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return (
                parsed
                if parsed.tzinfo is not None
                else parsed.replace(tzinfo=timezone.utc)
            )
    except (TypeError, ValueError, ArithmeticError) as error:
        raise CorrectionComparisonError(
            "Correction candidate typed value is invalid"
        ) from error
    raise CorrectionComparisonError(
        f"Correction candidate value type {value_type!r} is unsupported"
    )


__all__ = [
    "CORRECTION_CANDIDATE_BATCH_ROWS",
    "CorrectionCandidateArtifact",
    "CorrectionComparisonError",
    "iter_polars_correction_candidate_batches",
    "write_polars_correction_candidates",
]
