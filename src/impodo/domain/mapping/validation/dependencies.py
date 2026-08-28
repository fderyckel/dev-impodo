"""Validation projection for canonical incoming-dataset dependencies."""

from __future__ import annotations

from typing import Iterable, Mapping

from impodo.domain.relationship_dependencies import (
    DatasetDependencyEdge,
    required_cross_dataset_cycle,
)
from ..contracts import DatasetMapping
from .common import _issue
from .evidence import MappingValidationIssue


def _validate_dependencies(
    dependencies: Iterable[DatasetDependencyEdge],
    datasets_by_id: Mapping[str, DatasetMapping],
    issues: list[MappingValidationIssue],
) -> None:
    known = set(datasets_by_id)
    edges = tuple(dependencies)
    for edge in edges:
        if edge.dependency_dataset not in known:
            issues.append(
                _issue(
                    "MAPPING_DATASET_UNKNOWN",
                    "/datasets",
                    "An incoming relationship references an unknown dataset.",
                    "Choose a configured source dataset.",
                    dataset=datasets_by_id.get(edge.owner_dataset),
                )
            )
    cycle = required_cross_dataset_cycle(edges, known)
    if cycle is not None:
        issues.append(
            _issue(
                "MAPPING_DEPENDENCY_CYCLE",
                "/datasets",
                "Required-at-create relationships contain a dependency cycle.",
                "Make at least one relationship deferrable or remove the cycle.",
                dataset=datasets_by_id.get(cycle[0]),
            )
        )
