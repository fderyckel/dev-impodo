"""Deterministic dataset row-inclusion evaluation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..preparation.canonical import normalize_string
from ..recipe.profile import NormalizationSpec
from .contracts import (
    DatasetMapping,
    IdentityNullPolicy,
    RelationshipValueSource,
    ResolverOrigin,
    RowInclusionJoin,
    RowInclusionMode,
    RowInclusionPolicy,
)
from .source_conditions import SourceConditionValueError, source_condition_matches

_KEY_NORMALIZATION = NormalizationSpec(trim=True, empty_as_null=True)


@dataclass(frozen=True, slots=True)
class IncludedParentLink:
    """One direct incoming relationship and its ordered source reference."""

    target_field: str
    dataset_id: str
    source_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IncludedParentRows:
    """The admitted source identities of one parent, bound to child columns."""

    source_keys: tuple[str, ...]
    keys: frozenset[tuple[str | None, ...]]
    duplicate_key_count: int = 0


class IncludedParentRuleError(ValueError):
    """A row-selection link cannot be evaluated against the current mapping."""

    def __init__(self, dataset_id: str, message: str) -> None:
        super().__init__(message)
        self.dataset_id = dataset_id


def incoming_parent_links(mapping: DatasetMapping) -> tuple[IncludedParentLink, ...]:
    """Offer direct incoming references without aliases or special root values."""

    links = []
    for component in (*mapping.target_identity, *mapping.target_scope):
        resolver = component.resolver
        if (
            resolver is not None
            and resolver.origin is ResolverOrigin.DATASET
            and resolver.dataset_id is not None
            and resolver.dataset_projection_field is None
            and len(component.target_fields) == 1
            and component.null_policy is not IdentityNullPolicy.ZERO_AS_SCOPE_NULL
        ):
            links.append(IncludedParentLink(
                component.target_fields[0], resolver.dataset_id, component.source_column_keys,
            ))
    for relationship in mapping.relationships:
        resolver = relationship.resolver
        if (
            relationship.kind == "many2one"
            and relationship.value_source is RelationshipValueSource.SOURCE
            and not relationship.blank_source_values
            and resolver.origin is ResolverOrigin.DATASET
            and resolver.dataset_id is not None
            and resolver.dataset_projection_field is None
        ):
            links.append(IncludedParentLink(
                relationship.target_field, resolver.dataset_id, relationship.source_column_keys,
            ))
    return tuple(links)


def included_parent_bindings(
    datasets: Iterable[DatasetMapping],
    physical_columns: Mapping[str, frozenset[str]],
) -> dict[str, IncludedParentLink]:
    """Resolve each rule and reject missing keys, self-links and dependency cycles."""

    mappings = {item.dataset_id: item for item in datasets}
    links: dict[str, IncludedParentLink] = {}
    for dataset_id, mapping in mappings.items():
        field = mapping.row_inclusion.included_parent_field
        if field is None:
            continue
        matches = [link for link in incoming_parent_links(mapping) if link.target_field == field]
        link = matches[0] if len(matches) == 1 else None
        parent = mappings.get(link.dataset_id) if link is not None else None
        if (
            link is None
            or parent is None
            or not link.source_keys
            or len(link.source_keys) != len(parent.source_identity_column_keys)
            or not physical_columns.get(dataset_id, frozenset()).issuperset(link.source_keys)
            or not physical_columns.get(link.dataset_id, frozenset()).issuperset(
                parent.source_identity_column_keys
            )
        ):
            raise IncludedParentRuleError(
                dataset_id, "Choose a mapped incoming parent and matching source keys from two source tables."
            )
        links[dataset_id] = link

    ordered: dict[str, IncludedParentLink] = {}
    for dataset_id in links:
        chain: dict[str, IncludedParentLink] = {}
        current = dataset_id
        while current in links and current not in ordered:
            if current in chain:
                raise IncludedParentRuleError(
                    dataset_id, "Rows to use cannot follow a circular parent dependency."
                )
            link = links[current]
            chain[current] = link
            current = link.dataset_id
        for item in reversed(chain):
            ordered[item] = chain[item]
    return ordered


def compile_included_parent_rows(
    datasets: tuple[DatasetMapping, ...],
    physical_columns: Mapping[str, frozenset[str]],
    read_rows: Callable[[str, tuple[str, ...]], Iterable[Mapping[str, object]]],
) -> dict[str, IncludedParentRows]:
    """Read each participating parent's admitted source identities once."""

    mappings = {item.dataset_id: item for item in datasets}
    results: dict[str, IncludedParentRows] = {}
    identities: dict[str, tuple[frozenset[tuple[str | None, ...]], int]] = {}
    for dataset_id, link in included_parent_bindings(datasets, physical_columns).items():
        parent_id = link.dataset_id
        if parent_id not in identities:
            parent = mappings[parent_id]
            identity = parent.source_identity_column_keys
            gate = results.get(parent_id)
            columns = tuple(dict.fromkeys((
                *identity,
                *(condition.source_column_key for condition in parent.row_inclusion.conditions),
                *(gate.source_keys if gate is not None else ()),
            )))
            admitted: set[tuple[str | None, ...]] = set()
            duplicates: set[tuple[str | None, ...]] = set()
            for row in read_rows(parent_id, columns):
                if row_is_included(parent.row_inclusion, row, gate):
                    key = _source_key(identity, row)
                    if None not in key:
                        if key in admitted:
                            duplicates.add(key)
                        admitted.add(key)
            identities[parent_id] = (frozenset(admitted), len(duplicates))
        results[dataset_id] = IncludedParentRows(link.source_keys, *identities[parent_id])
    return results


def _source_key(
    keys: tuple[str, ...], row: Mapping[str, object],
) -> tuple[str | None, ...]:
    return tuple(
        normalize_string(str(value), _KEY_NORMALIZATION) if value is not None else None
        for value in (row.get(key) for key in keys)
    )


def row_is_included(
    policy: RowInclusionPolicy,
    source_values_by_key: Mapping[str, Any],
    included_parent: IncludedParentRows | None = None,
) -> bool:
    """Return whether one row belongs to the mapped dataset population."""

    if policy.mode is RowInclusionMode.ALL_ROWS:
        return True
    if policy.included_parent_field is not None:
        if included_parent is None:
            raise SourceConditionValueError("The included parent rows could not be checked.")
        return _source_key(included_parent.source_keys, source_values_by_key) in included_parent.keys
    matches: list[bool] = []
    for condition in policy.conditions:
        try:
            matches.append(
                source_condition_matches(
                    raw_value=source_values_by_key.get(
                        condition.source_column_key
                    ),
                    operator=condition.operator,
                    comparison_value=condition.comparison_value,
                    value_type=condition.value_type,
                )
            )
        except SourceConditionValueError as error:
            raise SourceConditionValueError(
                str(error),
                source_column_key=condition.source_column_key,
            ) from error
    if policy.join is RowInclusionJoin.ALL:
        return all(matches)
    return any(matches)
