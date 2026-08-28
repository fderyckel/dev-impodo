"""Deterministic row scheduling for frozen incoming relationships.

The scheduler knows only stable row identifiers, integer ranks, and directed
dependency edges.  Snapshot construction owns reference resolution and Odoo
semantics; this module owns the compact graph algorithms.
"""

from __future__ import annotations

from dataclasses import dataclass
from heapq import heappop, heappush
from typing import Iterable


MAX_EXECUTION_COMPONENT_PAGE_ROWS = 500


@dataclass(frozen=True, slots=True)
class DependencyNode:
    """One actionable row with an order-independent stable rank."""

    row_id: str
    rank: int


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    """A create receipt required by an owner field."""

    dependency_row_id: str
    owner_row_id: str
    owner_field: str
    strength: str


@dataclass(frozen=True, slots=True)
class ScheduleBlocker:
    """One deterministic reason a row cannot enter the write schedule."""

    row_id: str
    code: str
    field: str = ""
    dependency_row_id: str = ""


@dataclass(frozen=True, slots=True)
class DependencySchedule:
    """A topological schedule plus the exact optional edges it cuts."""

    ordered_row_ids: tuple[str, ...]
    components: tuple[tuple[str, ...], ...]
    deferred_edges: tuple[DependencyEdge, ...]
    blockers: tuple[ScheduleBlocker, ...]


@dataclass(frozen=True, slots=True)
class DependencyComponentPage:
    """One bounded slice of a single topological component."""

    component_sequence: int
    page_sequence: int
    row_ids: tuple[str, ...]


def dependency_component_pages(
    components: Iterable[tuple[int, Iterable[str]]],
    *,
    max_rows: int = MAX_EXECUTION_COMPONENT_PAGE_ROWS,
) -> Iterable[DependencyComponentPage]:
    """Yield deterministic bounded pages without mixing dependency levels."""

    if type(max_rows) is not int or max_rows <= 0:
        raise ValueError("dependency component page size is invalid")
    expected_component = 0
    for component_sequence, row_ids in components:
        if component_sequence != expected_component:
            raise ValueError("dependency component sequence is invalid")
        expected_component += 1
        rows = tuple(row_ids)
        if not rows:
            raise ValueError("dependency component is empty")
        for page_sequence, start in enumerate(range(0, len(rows), max_rows)):
            yield DependencyComponentPage(
                component_sequence=component_sequence,
                page_sequence=page_sequence,
                row_ids=rows[start : start + max_rows],
            )


def schedule_dependencies(
    nodes: Iterable[DependencyNode],
    edges: Iterable[DependencyEdge],
    blockers: Iterable[ScheduleBlocker] = (),
) -> DependencySchedule:
    """Schedule rows, cutting only optional edges required to break cycles."""

    node_tuple = tuple(nodes)
    by_id = {node.row_id: node for node in node_tuple}
    if len(by_id) != len(node_tuple):
        raise ValueError("row dependency graph contains duplicate nodes")
    rank = {node.row_id: node.rank for node in node_tuple}
    if len(set(rank.values())) != len(rank):
        raise ValueError("row dependency graph contains duplicate ranks")
    edge_tuple = tuple(
        sorted(
            set(edges),
            key=lambda item: (
                rank.get(item.dependency_row_id, -1),
                rank.get(item.owner_row_id, -1),
                item.owner_field,
                item.strength,
            ),
        )
    )
    if any(
        edge.dependency_row_id not in by_id or edge.owner_row_id not in by_id
        for edge in edge_tuple
    ):
        raise ValueError("row dependency edge refers to an unknown node")
    if any(edge.strength not in {"hard", "deferrable"} for edge in edge_tuple):
        raise ValueError("row dependency edge has an invalid strength")

    adjacency = _adjacency(by_id, edge_tuple)
    deferred: set[DependencyEdge] = set()
    hard_cycle_rows: set[str] = set()
    unresolved = _unresolved_nodes(tuple(by_id), edge_tuple, rank)
    for component in _strong_components(unresolved, adjacency, rank):
        internal = tuple(
            edge
            for edge in edge_tuple
            if edge.dependency_row_id in component
            and edge.owner_row_id in component
        )
        cyclic = len(component) > 1 or any(
            edge.dependency_row_id == edge.owner_row_id for edge in internal
        )
        if not cyclic:
            continue
        hard_internal = tuple(edge for edge in internal if edge.strength == "hard")
        hard_adjacency = _adjacency(component, hard_internal)
        hard_cycles = tuple(
            candidate
            for candidate in _strong_components(component, hard_adjacency, rank)
            if len(candidate) > 1
            or any(
                edge.dependency_row_id == edge.owner_row_id
                and edge.dependency_row_id in candidate
                for edge in hard_internal
            )
        )
        if hard_cycles:
            hard_cycle_rows.update(
                row_id for candidate in hard_cycles for row_id in candidate
            )
            continue
        hard_order = _topological_order(component, hard_internal, rank)
        position = {row_id: index for index, row_id in enumerate(hard_order)}
        deferred.update(
            edge
            for edge in internal
            if edge.strength == "deferrable"
            and position[edge.dependency_row_id] >= position[edge.owner_row_id]
        )

    blocker_by_row: dict[str, ScheduleBlocker] = {}
    for blocker in sorted(blockers, key=lambda item: _blocker_key(item, rank)):
        if blocker.row_id not in by_id:
            raise ValueError("row dependency blocker refers to an unknown node")
        blocker_by_row.setdefault(blocker.row_id, blocker)
    for row_id in sorted(hard_cycle_rows, key=rank.__getitem__):
        blocker_by_row.setdefault(
            row_id,
            ScheduleBlocker(row_id=row_id, code="HARD_DEPENDENCY_CYCLE"),
        )

    queue = list(sorted(blocker_by_row, key=rank.__getitem__))
    cursor = 0
    while cursor < len(queue):
        blocked_dependency = queue[cursor]
        cursor += 1
        for edge in adjacency[blocked_dependency]:
            if edge.owner_row_id in blocker_by_row:
                continue
            blocker_by_row[edge.owner_row_id] = ScheduleBlocker(
                row_id=edge.owner_row_id,
                code="BLOCKED_DEPENDENCY",
                field=edge.owner_field,
                dependency_row_id=blocked_dependency,
            )
            queue.append(edge.owner_row_id)

    usable = tuple(
        row_id
        for row_id in sorted(by_id, key=rank.__getitem__)
        if row_id not in blocker_by_row
    )
    usable_set = set(usable)
    deferred = {
        edge
        for edge in deferred
        if edge.dependency_row_id in usable_set
        and edge.owner_row_id in usable_set
    }
    retained = tuple(
        edge
        for edge in edge_tuple
        if edge not in deferred
        and edge.dependency_row_id in usable_set
        and edge.owner_row_id in usable_set
    )
    ordered, levels = _topological_order_with_levels(usable, retained, rank)
    rows_by_level: dict[int, list[str]] = {}
    for row_id in ordered:
        rows_by_level.setdefault(levels[row_id], []).append(row_id)
    components = tuple(
        tuple(rows_by_level[level]) for level in sorted(rows_by_level)
    )
    return DependencySchedule(
        ordered_row_ids=ordered,
        components=components,
        deferred_edges=tuple(
            sorted(deferred, key=lambda item: _edge_key(item, rank))
        ),
        blockers=tuple(
            sorted(blocker_by_row.values(), key=lambda item: _blocker_key(item, rank))
        ),
    )


def _adjacency(
    nodes: Iterable[str],
    edges: Iterable[DependencyEdge],
) -> dict[str, tuple[DependencyEdge, ...]]:
    result: dict[str, list[DependencyEdge]] = {row_id: [] for row_id in nodes}
    for edge in edges:
        result[edge.dependency_row_id].append(edge)
    return {
        row_id: tuple(values)
        for row_id, values in result.items()
    }


def _strong_components(
    nodes: Iterable[str],
    adjacency: dict[str, tuple[DependencyEdge, ...]],
    rank: dict[str, int],
) -> tuple[frozenset[str], ...]:
    """Return SCCs using iterative Kosaraju passes (no recursion depth risk)."""

    node_tuple = tuple(sorted(nodes, key=rank.__getitem__))
    allowed = set(node_tuple)
    following = {
        row_id: tuple(
            sorted(
                (
                    edge.owner_row_id
                    for edge in adjacency.get(row_id, ())
                    if edge.owner_row_id in allowed
                ),
                key=rank.__getitem__,
            )
        )
        for row_id in node_tuple
    }
    reverse: dict[str, list[str]] = {row_id: [] for row_id in node_tuple}
    for dependency, owners in following.items():
        for owner in owners:
            reverse[owner].append(dependency)
    for values in reverse.values():
        values.sort(key=rank.__getitem__)

    visited: set[str] = set()
    finish: list[str] = []
    for start in node_tuple:
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            row_id, expanded = stack.pop()
            if expanded:
                finish.append(row_id)
                continue
            if row_id in visited:
                continue
            visited.add(row_id)
            stack.append((row_id, True))
            for owner in reversed(following[row_id]):
                if owner not in visited:
                    stack.append((owner, False))

    assigned: set[str] = set()
    components: list[frozenset[str]] = []
    for start in reversed(finish):
        if start in assigned:
            continue
        members: set[str] = set()
        stack = [start]
        assigned.add(start)
        while stack:
            row_id = stack.pop()
            members.add(row_id)
            for dependency in reversed(reverse[row_id]):
                if dependency not in assigned:
                    assigned.add(dependency)
                    stack.append(dependency)
        components.append(frozenset(members))
    return tuple(
        sorted(components, key=lambda item: min(rank[row_id] for row_id in item))
    )


def _topological_order(
    nodes: Iterable[str],
    edges: Iterable[DependencyEdge],
    rank: dict[str, int],
) -> tuple[str, ...]:
    ordered, _levels = _topological_order_with_levels(nodes, edges, rank)
    return ordered


def _unresolved_nodes(
    nodes: Iterable[str],
    edges: Iterable[DependencyEdge],
    rank: dict[str, int],
) -> tuple[str, ...]:
    """Return only the cyclic residue after a stable Kahn pass."""

    node_tuple = tuple(nodes)
    following: dict[str, list[str]] = {row_id: [] for row_id in node_tuple}
    indegree = {row_id: 0 for row_id in node_tuple}
    for edge in edges:
        following[edge.dependency_row_id].append(edge.owner_row_id)
        indegree[edge.owner_row_id] += 1
    ready: list[tuple[int, str]] = []
    for row_id, count in indegree.items():
        if count == 0:
            heappush(ready, (rank[row_id], row_id))
    while ready:
        _row_rank, row_id = heappop(ready)
        for owner in following[row_id]:
            indegree[owner] -= 1
            if indegree[owner] == 0:
                heappush(ready, (rank[owner], owner))
    return tuple(
        row_id
        for row_id in sorted(node_tuple, key=rank.__getitem__)
        if indegree[row_id] > 0
    )


def _topological_order_with_levels(
    nodes: Iterable[str],
    edges: Iterable[DependencyEdge],
    rank: dict[str, int],
) -> tuple[tuple[str, ...], dict[str, int]]:
    node_tuple = tuple(nodes)
    allowed = set(node_tuple)
    following: dict[str, list[str]] = {row_id: [] for row_id in node_tuple}
    indegree = {row_id: 0 for row_id in node_tuple}
    for edge in edges:
        if edge.dependency_row_id not in allowed or edge.owner_row_id not in allowed:
            continue
        following[edge.dependency_row_id].append(edge.owner_row_id)
        indegree[edge.owner_row_id] += 1
    ready: list[tuple[int, str]] = []
    levels = {row_id: 0 for row_id in node_tuple}
    for row_id, count in indegree.items():
        if count == 0:
            heappush(ready, (rank[row_id], row_id))
    ordered: list[str] = []
    current_level = 0
    while ready:
        _row_rank, row_id = heappop(ready)
        levels[row_id] = max(levels[row_id], current_level)
        current_level = levels[row_id]
        ordered.append(row_id)
        for owner in sorted(following[row_id], key=rank.__getitem__):
            levels[owner] = max(levels[owner], levels[row_id] + 1)
            indegree[owner] -= 1
            if indegree[owner] == 0:
                heappush(ready, (rank[owner], owner))
    if len(ordered) != len(node_tuple):
        raise ValueError("row dependency schedule is cyclic")
    return tuple(ordered), levels


def _edge_key(
    edge: DependencyEdge,
    rank: dict[str, int],
) -> tuple[int, int, str, str]:
    return (
        rank[edge.owner_row_id],
        rank[edge.dependency_row_id],
        edge.owner_field,
        edge.strength,
    )


def _blocker_key(
    blocker: ScheduleBlocker,
    rank: dict[str, int],
) -> tuple[int, str, str, str]:
    return (
        rank.get(blocker.row_id, -1),
        blocker.code,
        blocker.field,
        blocker.dependency_row_id,
    )
