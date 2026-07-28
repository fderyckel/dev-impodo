"""Environment-specific target record catalog."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from .models import BusinessReference, TargetRecord


class TargetCatalog:
    """Indexes target records without collapsing duplicate business keys."""

    def __init__(self, records: Mapping[str, Iterable[TargetRecord]]) -> None:
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
        return self._records.get(model, ())

    def by_id(self, model: str, odoo_id: int) -> TargetRecord | None:
        return self._by_id.get(model, {}).get(odoo_id)

    def find_by_fields(
        self,
        model: str,
        fields: tuple[str, ...],
        key: tuple[Any, ...],
    ) -> tuple[TargetRecord, ...]:
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

