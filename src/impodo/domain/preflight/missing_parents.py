"""Group confirmed missing relationships from saved comparison evidence."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json


@dataclass(frozen=True, slots=True)
class MissingParentValue:
    value: str
    affected_count: int


@dataclass(frozen=True, slots=True)
class MissingParentGroup:
    group_id: str
    dataset: str
    field: str
    related_model: str
    key_field: str
    scope_fields: tuple[str, ...]
    scope_values: tuple[str, ...]
    values: tuple[MissingParentValue, ...]

    @property
    def affected_count(self) -> int:
        return sum(item.affected_count for item in self.values)

    @property
    def stage3_list_supported(self) -> bool:
        values = tuple(item.value for item in self.values)
        return (
            1 <= len(values) <= 500
            and all(value == value.strip() and "," not in value for value in values)
            and len(",".join(values)) <= 10_000
        )

    @property
    def copy_text(self) -> str:
        separator = ", " if self.stage3_list_supported else "\n"
        return separator.join(item.value for item in self.values)


def missing_parent_groups(
    resolutions: Sequence[Mapping[str, object]],
) -> tuple[MissingParentGroup, ...]:
    """Use only exact, zero-match target references; never parse issue text."""

    grouped: dict[
        tuple[str, str, str, str, tuple[str, ...], tuple[str, ...]],
        dict[str, int],
    ] = defaultdict(lambda: defaultdict(int))
    for item in resolutions:
        if item.get("status") != "NOT_FOUND" or item.get("match_count") != 0:
            continue
        reference = item.get("reference")
        if not isinstance(reference, Mapping) or reference.get("origin") not in {
            "target", "target_then_incoming",
        }:
            continue
        key = reference.get("key")
        fields = reference.get("target_fields")
        scope = reference.get("scope", [])
        scope_fields = reference.get("target_scope_fields", [])
        if (
            not isinstance(key, list) or len(key) != 1
            or not isinstance(key[0], str) or not key[0]
            or not isinstance(fields, list) or len(fields) != 1
            or not isinstance(fields[0], str)
            or not isinstance(scope, list)
            or not isinstance(scope_fields, list)
            or len(scope) != len(scope_fields)
            or any(not isinstance(value, str) for value in scope)
            or any(not isinstance(value, str) for value in scope_fields)
        ):
            continue
        incoming_key = reference.get("incoming_key")
        source_value = (
            incoming_key[0]
            if reference.get("origin") == "target_then_incoming"
            and isinstance(incoming_key, list)
            and incoming_key
            and isinstance(incoming_key[0], str)
            else key[0]
        )
        dataset = item.get("dataset")
        field = item.get("field")
        model = reference.get("model")
        count = item.get("affected_count")
        if (
            not isinstance(dataset, str) or not dataset
            or not isinstance(field, str) or not field
            or not isinstance(model, str) or not model
            or not isinstance(count, int) or isinstance(count, bool) or count < 1
        ):
            continue
        if field.startswith(("target_identity:", "scope:")):
            field = field.split(":", 1)[1]
        group_key = (
            dataset, field, model, fields[0], tuple(scope_fields), tuple(scope)
        )
        # An identity component and its many2one field can resolve the same
        # source reference twice. Count the affected records once.
        grouped[group_key][source_value] = max(
            grouped[group_key][source_value], count
        )
    result = []
    for group_key, values in sorted(grouped.items()):
        identity = json.dumps(group_key, ensure_ascii=False, separators=(",", ":"))
        result.append(
            MissingParentGroup(
                group_id=sha256(identity.encode("utf-8")).hexdigest()[:24],
                dataset=group_key[0],
                field=group_key[1],
                related_model=group_key[2],
                key_field=group_key[3],
                scope_fields=group_key[4],
                scope_values=group_key[5],
                values=tuple(
                    MissingParentValue(value, count)
                    for value, count in sorted(values.items())
                ),
            )
        )
    return tuple(result)
