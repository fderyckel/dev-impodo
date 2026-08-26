"""Environment-specific target record indexing and relation decoding.

`planner.py` decides which Odoo records must be captured. Connector
implementations return those records as :class:`TargetRecord` objects.
`PreflightEngine` then builds a :class:`TargetCatalog` to perform in-memory,
batched business-key and numeric-ID lookups without issuing an Odoo request per
source row.

Numeric Odoo IDs exist only inside this target-database-specific catalog. Resolved
relationships leave the catalog as portable :class:`BusinessReference`
objects.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from impodo.domain.shared.models import BusinessReference, TargetRecord


class TargetCatalog:
    """Index a captured target snapshot without hiding duplicate keys.

    Records are sorted by Odoo ID for deterministic output. Business-key
    indexes are built lazily for the exact field combinations requested by the
    engine. Each index value remains a tuple because duplicate keys are
    evidence that must produce an ambiguous result, not a last-record-wins
    lookup.
    """

    def __init__(self, records: Mapping[str, Iterable[TargetRecord]]) -> None:
        """Build deterministic per-model record and numeric-ID indexes."""

        self._records = {
            model: tuple(sorted(items, key=lambda item: item.odoo_id))
            for model, items in records.items()
        }
        self._by_id = {
            model: {record.odoo_id: record for record in model_records}
            for model, model_records in self._records.items()
        }
        self._field_indexes: dict[
            tuple[str, tuple[str, ...]],
            dict[tuple[Any, ...], tuple[TargetRecord, ...]],
        ] = {}

    def records(self, model: str) -> tuple[TargetRecord, ...]:
        """Return all captured records for `model` in numeric-ID order."""

        return self._records.get(model, ())

    def by_id(self, model: str, odoo_id: int) -> TargetRecord | None:
        """Return one captured target record by its target-database-specific ID."""

        return self._by_id.get(model, {}).get(odoo_id)

    def find_by_fields(
        self,
        model: str,
        fields: tuple[str, ...],
        key: tuple[Any, ...],
    ) -> tuple[TargetRecord, ...]:
        """Find every record whose ordered field values equal `key`.

        The first call for a `(model, fields)` pair builds and caches the
        complete index. Later source rows reuse it, avoiding N+1 scans and
        preserving all duplicate matches.
        """

        index_key = (model, fields)
        if index_key not in self._field_indexes:
            buckets: dict[tuple[Any, ...], list[TargetRecord]] = defaultdict(list)
            for record in self.records(model):
                record_key = tuple(record.values.get(field) for field in fields)
                buckets[record_key].append(record)
            self._field_indexes[index_key] = {
                bucket_key: tuple(items) for bucket_key, items in buckets.items()
            }
        return self._field_indexes[index_key].get(key, ())

    def reference_from_id(
        self,
        model: str,
        raw_relation: Any,
        identity_fields: tuple[str, ...],
        scope_fields: tuple[str, ...] = (),
    ) -> BusinessReference | None:
        """Convert an Odoo relation value into a portable business reference.

        Args:
            model: Related Odoo model.
            raw_relation: JSON-2 many2one value, either an ID or `[id, label]`.
            identity_fields: Ordered fields forming the portable business key.
            scope_fields: Optional company/site/parent scope fields.

        Raises:
            KeyError: If the relation ID was not included in the captured
                target snapshot.
            ValueError: If `raw_relation` has an unsupported shape.
        """

        odoo_id = relation_id(raw_relation)
        if odoo_id is None:
            return None
        record = self.by_id(model, odoo_id)
        if record is None:
            raise KeyError(f"{model} id {odoo_id} is absent from target catalog")
        return BusinessReference(
            model=model,
            key=tuple(record.values.get(field) for field in identity_fields),
            scope=tuple(record.values.get(field) for field in scope_fields),
        )


def relation_id(value: Any) -> int | None:
    """Decode an Odoo many2one value while rejecting unsafe shapes.

    Odoo may return `False`, an integer ID, or a pair such as `[id,
    display_name]`. Booleans are rejected as IDs even though `bool` subclasses
    `int` in Python.
    """

    if value in (None, False, ""):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)) and value:
        candidate = value[0]
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return candidate
    raise ValueError(f"unsupported many2one target value {value!r}")


def relation_ids(value: Any) -> tuple[int, ...]:
    """Decode an Odoo many2many value into validated integer IDs."""

    if value in (None, False, ""):
        return ()
    if isinstance(value, list | tuple):
        result = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"unsupported many2many target value {value!r}")
            result.append(item)
        return tuple(result)
    raise ValueError(f"unsupported many2many target value {value!r}")

