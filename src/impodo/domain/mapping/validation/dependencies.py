"""Incoming-dataset dependency graph validation."""

from __future__ import annotations

from typing import Mapping

from ..contracts import DatasetMapping
from .common import _issue
from .evidence import MappingValidationIssue


def _validate_dependencies(
    dependencies: Mapping[str, set[str]],
    datasets_by_id: Mapping[str, DatasetMapping],
    issues: list[MappingValidationIssue],
) -> None:
    known = set(datasets_by_id)
    for owner, targets in dependencies.items():
        for target in targets:
            if target not in known:
                issues.append(
                    _issue(
                        "MAPPING_DATASET_UNKNOWN",
                        "/datasets",
                        "An incoming relationship references an unknown dataset.",
                        "Choose a configured source dataset.",
                        dataset=datasets_by_id.get(owner),
                    )
                )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            issues.append(
                _issue(
                    "MAPPING_DEPENDENCY_CYCLE",
                    "/datasets",
                    "Incoming relationships contain a dependency cycle.",
                    "Remove the cycle or defer a reviewed multi-pass strategy.",
                    dataset=datasets_by_id.get(node),
                )
            )
            return
        if node in visited:
            return
        visiting.add(node)
        for child in sorted(dependencies.get(node, ())):
            if child in known:
                visit(child)
        visiting.remove(node)
        visited.add(node)

    for item in sorted(known):
        visit(item)
