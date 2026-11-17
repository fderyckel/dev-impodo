"""Find repeated prepared scalar values independently of record identity.

Rules apply across incoming tables for the same Odoo model. Empty prepared
values are ignored; case and spacing of nonempty text remain significant.
Callers may supply temporary disk-backed counts for large preparation runs.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping, Sequence
from decimal import Decimal
from hashlib import sha256

from impodo.domain.mapping.contracts import MappingDefinition
from impodo.domain.preparation.staging_contracts import CanonicalRow, StagingDisposition
from impodo.domain.shared.models import canonical_json_bytes, portable_value


def mapped_unique_fields(definition: MappingDefinition) -> tuple[tuple[str, str], ...]:
    """Return each explicitly enabled model/field rule once, in stable order."""

    return tuple(
        sorted(
            {
                (dataset.target_model, field.target_field)
                for dataset in definition.datasets
                for field in dataset.fields
                if field.must_be_unique
            }
        )
    )


def unique_value_key(value: object) -> str | None:
    """Hash exact nonblank prepared values, equating numeric decimal scales."""

    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (Decimal, int)) and not isinstance(value, bool):
        number = Decimal(value)
        sign, digits, exponent = number.as_tuple()
        if isinstance(exponent, int):
            normalized = list(digits)
            while normalized and normalized[-1] == 0:
                normalized.pop()
                exponent += 1
            value = (
                "number",
                sign if normalized else 0,
                tuple(normalized),
                exponent if normalized else 0,
            )
    return sha256(canonical_json_bytes(portable_value(value))).hexdigest()


def duplicate_field_values(
    rows: Sequence[CanonicalRow],
    fields: tuple[tuple[str, str], ...],
    *,
    counts: MutableMapping[str, int] | None = None,
) -> Iterator[tuple[CanonicalRow, str, int]]:
    """Replay every repeated member using two scans and compact value counts.

    Excluded rows do not participate. Blocked rows still participate when their
    prepared value exists, so repairing another finding cannot hide a duplicate.
    No source values or complete duplicate groups are retained in the index.
    """

    if not fields:
        return
    counter: MutableMapping[str, int] = {} if counts is None else counts
    fields_by_model: dict[str, list[str]] = {}
    for model, field in fields:
        fields_by_model.setdefault(model, []).append(field)

    def keys(row: CanonicalRow) -> Iterator[tuple[str, str]]:
        if row.disposition is StagingDisposition.EXCLUDED:
            return
        for field in fields_by_model.get(row.target_model, ()):
            key = unique_value_key(row.proposed_values.get(field))
            if key is not None:
                yield field, str((row.target_model, field, key))

    for row in rows:
        for _field, key in keys(row):
            counter[key] = counter.get(key, 0) + 1
    for row in rows:
        for field, key in keys(row):
            count = counter[key]
            if count > 1:
                yield row, field, count


def duplicate_field_message(field: str, count: int) -> str:
    """Explain a duplicate beside the protected prepared value and source row."""

    return (
        f"{count} incoming records use the same prepared value for {field}. "
        "Correct the repeated values or review which source rows to include, "
        "then prepare again."
    )
