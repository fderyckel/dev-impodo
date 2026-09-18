"""Discover a bounded protected relationship closure for Odoo source capture."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

from ..domain.odoo_capture import OdooCaptureRole
from ..domain.odoo_provenance import OdooOriginBatch
from ..domain.odoo_source_capture import (
    OdooSourceCaptureConsistencyError,
    OdooSourceCaptureLimitError,
    OdooSourceCaptureRequest,
)


MAX_DEPENDENCY_DEPTH = 4
MAX_DEPENDENCY_TOTAL_ROWS = 50_000
MAX_DEPENDENCY_EDGES = 250_000

OriginFact = tuple[datetime | None, tuple[tuple[str, tuple[int, ...]], ...]]


@dataclass(slots=True)
class ProtectedDependencyClosure:
    """Instance-specific membership and facts; never persist or log this object."""

    ids_by_model: dict[str, tuple[int, ...]]
    facts_by_model: dict[str, dict[int, OriginFact]]
    discovery_facts_by_model: dict[str, dict[int, OriginFact]]


def discover_dependency_closure(
    requests: tuple[OdooSourceCaptureRequest, ...],
    scan: Callable[[OdooSourceCaptureRequest], tuple[OdooOriginBatch, ...]],
) -> ProtectedDependencyClosure:
    """Follow selected links with bounded depth, rows, edges, and ID sets."""

    by_model = {request.model: request for request in requests}
    if len(by_model) != len(requests):
        raise OdooSourceCaptureConsistencyError("Odoo dependency model scope is invalid")
    roots = tuple(
        request for request in requests
        if request.capture_role is OdooCaptureRole.ROOT
    )
    if not roots:
        raise OdooSourceCaptureConsistencyError(
            "Choose at least one root Odoo record type before checking linked records"
        )
    facts: dict[str, dict[int, OriginFact]] = {
        model: {} for model in by_model
    }
    discovery_facts: dict[str, dict[int, OriginFact]] = {
        model: {} for model in by_model
    }
    pending: deque[tuple[int, str, tuple[int, ...]]] = deque()
    enqueued: dict[str, set[int]] = defaultdict(set)
    root_references: dict[str, set[int]] = defaultdict(set)
    total_rows = 0
    total_edges = 0

    def record(
        request: OdooSourceCaptureRequest,
        batches: tuple[OdooOriginBatch, ...],
        depth: int,
    ) -> tuple[int, ...]:
        nonlocal total_rows, total_edges
        model_facts = facts[request.model]
        projections = tuple(sorted(
            (*request.relationship_projection, *request.discovery_relationship_projection),
            key=lambda item: item.name,
        ))
        expected_fields = tuple(item.name for item in projections)
        projection_by_name = {item.name: item for item in projections}
        captured_fields = {item.name for item in request.relationship_projection}
        found: list[int] = []
        for batch in batches:
            if tuple(item.field_name for item in batch.relationships) != expected_fields:
                raise OdooSourceCaptureConsistencyError(
                    "Odoo dependency relationship evidence is incomplete"
                )
            for offset, identifier in enumerate(batch.odoo_ids):
                if identifier in model_facts:
                    raise OdooSourceCaptureConsistencyError(
                        "Odoo dependency scan returned duplicate records"
                    )
                relations = tuple(
                    (column.field_name, column.values[offset])
                    for column in batch.relationships
                )
                model_facts[identifier] = (
                    batch.write_dates[offset],
                    tuple(item for item in relations if item[0] in captured_fields),
                )
                discovery_facts[request.model][identifier] = (
                    batch.write_dates[offset], relations,
                )
                found.append(identifier)
                total_rows += 1
                if (
                    len(model_facts) > request.maximum_rows
                    or total_rows > MAX_DEPENDENCY_TOTAL_ROWS
                ):
                    raise OdooSourceCaptureLimitError(
                        "Odoo linked-record closure exceeds the row limit"
                    )
                for field_name, members in relations:
                    projection = projection_by_name[field_name]
                    target = by_model[projection.relation_model]
                    if (
                        projection.kind == "one2many"
                        and target.capture_role is OdooCaptureRole.ROOT
                    ):
                        continue
                    total_edges += len(members)
                    if total_edges > MAX_DEPENDENCY_EDGES:
                        raise OdooSourceCaptureLimitError(
                            "Odoo linked-record closure exceeds the relationship limit"
                        )
                    if target.capture_role is OdooCaptureRole.ROOT:
                        root_references[target.model].update(members)
                        continue
                    new_members = tuple(
                        member for member in members
                        if member not in enqueued[target.model]
                    )
                    if not new_members:
                        continue
                    if depth >= MAX_DEPENDENCY_DEPTH:
                        raise OdooSourceCaptureLimitError(
                            "Odoo linked-record closure exceeds the depth limit"
                        )
                    enqueued[target.model].update(new_members)
                    if len(enqueued[target.model]) > target.maximum_rows:
                        raise OdooSourceCaptureLimitError(
                            "Odoo linked-record closure exceeds the model row limit"
                        )
                    pending.append((depth + 1, target.model, new_members))
        return tuple(found)

    for request in roots:
        record(request, scan(request), 0)
    for model, members in root_references.items():
        if not members.issubset(facts[model]):
            raise OdooSourceCaptureConsistencyError(
                f"A selected link points outside the root {model} records. "
                "Expand its root filter or review the relationship scope."
            )

    while pending:
        depth, model, members = pending.popleft()
        request = by_model[model]
        for start in range(0, len(members), 100):
            chunk = tuple(sorted(members[start:start + 100]))
            found = record(replace(request, member_ids=chunk), scan(
                replace(request, member_ids=chunk)
            ), depth)
            if set(found) != set(chunk):
                raise OdooSourceCaptureConsistencyError(
                    f"A linked {model} record is missing or inaccessible. "
                    "Review the source user's access and selected company scope."
                )
    for model, members in root_references.items():
        if not members.issubset(facts[model]):
            raise OdooSourceCaptureConsistencyError(
                f"A selected link points outside the root {model} records"
            )
    return ProtectedDependencyClosure(
        ids_by_model={model: tuple(sorted(rows)) for model, rows in facts.items()},
        facts_by_model=facts,
        discovery_facts_by_model=discovery_facts,
    )


def require_capture_matches_discovery(
    request: OdooSourceCaptureRequest,
    batches: tuple[OdooOriginBatch, ...],
    closure: ProtectedDependencyClosure,
) -> None:
    """Block publication if source membership or links changed after discovery."""

    actual: dict[int, OriginFact] = {}
    for batch in batches:
        for offset, identifier in enumerate(batch.odoo_ids):
            if identifier in actual:
                raise OdooSourceCaptureConsistencyError(
                    "Odoo capture returned a duplicate linked record"
                )
            actual[identifier] = (
                batch.write_dates[offset],
                tuple(
                    (column.field_name, column.values[offset])
                    for column in batch.relationships
                ),
            )
    if actual != closure.facts_by_model[request.model]:
        raise OdooSourceCaptureConsistencyError(
            f"Odoo {request.model} membership or relationships changed during capture. "
            "Check the source and freeze again."
        )
