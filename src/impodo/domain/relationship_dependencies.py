"""Canonical incoming-dataset dependency evidence.

The extractor accepts both browser ``DatasetMapping`` values and compiled
``DatasetSpec`` values.  It deliberately uses their small shared structural
surface so authoring validation, profile validation, preflight planning, and
execution-snapshot construction cannot assign different meanings to the same
incoming relationship.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Collection, Iterable, Mapping


class DependencyStrength(StrEnum):
    """Whether an incoming dependency may be completed after owner creation."""

    HARD = "hard"
    DEFERRABLE = "deferrable"


class DependencySource(StrEnum):
    """Compiled location that declares an incoming dependency."""

    TARGET_IDENTITY = "target_identity"
    TARGET_SCOPE = "target_scope"
    RELATIONSHIP = "relationship"


@dataclass(frozen=True, slots=True)
class DatasetDependencyEdge:
    """One canonical dataset dependency declared by an incoming resolver."""

    owner_dataset: str
    dependency_dataset: str
    target_field: str
    source: DependencySource
    strength: DependencyStrength

    @property
    def is_self_reference(self) -> bool:
        """Return whether row-level analysis must resolve this dataset edge."""

        return self.owner_dataset == self.dependency_dataset

    def portable_dict(self) -> dict[str, str | bool]:
        """Return deterministic evidence suitable for hashes and reports."""

        return {
            "owner_dataset": self.owner_dataset,
            "dependency_dataset": self.dependency_dataset,
            "target_field": self.target_field,
            "source": self.source.value,
            "strength": self.strength.value,
            "self_reference": self.is_self_reference,
        }


def extract_dataset_dependency_edges(
    datasets: Iterable[object],
    *,
    required_relationship_fields: Mapping[str, Collection[str]] | None = None,
) -> tuple[DatasetDependencyEdge, ...]:
    """Extract one order-independent meaning for every incoming resolver.

    ``required_relationship_fields`` carries captured Odoo create requirements
    by target model.  Browser semantic validation supplies it from the schema;
    compiled profiles express the same constraint through
    ``required_on_create`` until their target metadata is checked.
    """

    captured_required = required_relationship_fields or {}
    edges: set[DatasetDependencyEdge] = set()
    for dataset in datasets:
        owner = _dataset_name(dataset)
        target_model = _target_model(dataset)
        identity, scope = _identity_groups(dataset)
        for source, components in (
            (DependencySource.TARGET_IDENTITY, identity),
            (DependencySource.TARGET_SCOPE, scope),
        ):
            for component in components:
                dependency = _incoming_dataset(_resolver(component))
                if dependency is None:
                    continue
                edges.add(
                    DatasetDependencyEdge(
                        owner_dataset=owner,
                        dependency_dataset=dependency,
                        target_field=_component_target_field(component),
                        source=source,
                        strength=DependencyStrength.HARD,
                    )
                )
        for target_field, relationship in _relationships(dataset):
            dependency = _incoming_dataset(_resolver(relationship))
            if dependency is None:
                continue
            hard = bool(getattr(relationship, "required_on_create", False)) or (
                target_field in captured_required.get(target_model, ())
            )
            edges.add(
                DatasetDependencyEdge(
                    owner_dataset=owner,
                    dependency_dataset=dependency,
                    target_field=target_field,
                    source=DependencySource.RELATIONSHIP,
                    strength=(
                        DependencyStrength.HARD
                        if hard
                        else DependencyStrength.DEFERRABLE
                    ),
                )
            )
    return tuple(sorted(edges, key=_edge_sort_key))


def dependency_sets_by_owner(
    edges: Iterable[DatasetDependencyEdge],
) -> dict[str, tuple[str, ...]]:
    """Aggregate canonical edges without discarding self-references."""

    result: dict[str, set[str]] = {}
    for edge in edges:
        result.setdefault(edge.owner_dataset, set()).add(edge.dependency_dataset)
    return {
        owner: tuple(sorted(dependencies))
        for owner, dependencies in sorted(result.items())
    }


def required_cross_dataset_cycle(
    edges: Iterable[DatasetDependencyEdge],
    known_datasets: Collection[str],
) -> tuple[str, ...] | None:
    """Return one deterministic hard-edge cycle, excluding self-references."""

    known = set(known_datasets)
    graph: dict[str, set[str]] = {name: set() for name in known}
    for edge in edges:
        if (
            edge.strength is DependencyStrength.HARD
            and not edge.is_self_reference
            and edge.owner_dataset in known
            and edge.dependency_dataset in known
        ):
            graph[edge.owner_dataset].add(edge.dependency_dataset)

    visited: set[str] = set()
    active: dict[str, int] = {}
    path: list[str] = []

    def visit(name: str) -> tuple[str, ...] | None:
        if name in active:
            start = active[name]
            return (*path[start:], name)
        if name in visited:
            return None
        active[name] = len(path)
        path.append(name)
        for dependency in sorted(graph[name]):
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        path.pop()
        active.pop(name)
        visited.add(name)
        return None

    for name in sorted(known):
        cycle = visit(name)
        if cycle is not None:
            return cycle
    return None


def _dataset_name(dataset: object) -> str:
    value = getattr(dataset, "name", None)
    if value is None:
        value = getattr(dataset, "dataset_id", None)
    if not value:
        raise ValueError("dataset dependency owner is missing")
    return str(value)


def _target_model(dataset: object) -> str:
    value = getattr(dataset, "target_model", None)
    if value is None:
        target = getattr(dataset, "target", None)
        value = getattr(target, "model", None)
    return str(value or "")


def _identity_groups(dataset: object) -> tuple[tuple[object, ...], tuple[object, ...]]:
    target_identity = getattr(dataset, "target_identity")
    components = getattr(target_identity, "components", target_identity)
    scope = getattr(target_identity, "scope", None)
    if scope is None:
        scope = getattr(dataset, "target_scope", ())
    return tuple(components), tuple(scope)


def _relationships(dataset: object) -> tuple[tuple[str, object], ...]:
    values = getattr(dataset, "relations", None)
    if values is not None:
        return tuple((str(field), relation) for field, relation in values.items())
    return tuple(
        (str(getattr(relation, "target_field")), relation)
        for relation in getattr(dataset, "relationships", ())
    )


def _incoming_dataset(resolver: object | None) -> str | None:
    if resolver is None:
        return None
    value = getattr(resolver, "dataset", None)
    if value is None:
        value = getattr(resolver, "dataset_id", None)
    return str(value) if value else None


def _resolver(value: object) -> object | None:
    resolver = getattr(value, "resolver", None)
    if resolver is None:
        resolver = getattr(value, "resolve", None)
    return resolver


def _component_target_field(component: object) -> str:
    return ",".join(str(value) for value in getattr(component, "target_fields", ()))


def _edge_sort_key(edge: DatasetDependencyEdge) -> tuple[str, ...]:
    return (
        edge.owner_dataset,
        edge.dependency_dataset,
        edge.source.value,
        edge.target_field,
        edge.strength.value,
    )
