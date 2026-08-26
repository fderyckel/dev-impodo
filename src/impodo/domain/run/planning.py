"""Make deterministic, persistence-free decisions for integrated runs."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..serialization import content_hash
from impodo.domain.run.contracts import (
    MigrationRunPlanIssue,
    MigrationRunPlanIssueLevel,
    OdooModelRequirement,
    RecipeDependency,
    RecipeRevisionSelection,
    ReferenceRequirement,
)


class RunPlanningApplication(Protocol):
    """Expose only the Recipe evidence used by pure run planning."""

    selection: RecipeRevisionSelection
    requirements: tuple[OdooModelRequirement, ...]
    reference_requirements: tuple[ReferenceRequirement, ...]
    write_claims: tuple[tuple[str, str], ...]


class RunRequirementReview(Protocol):
    """Expose the immutable requirement meaning stored for reuse."""

    application_order: tuple[str, ...]
    dependencies: tuple[RecipeDependency, ...]
    model_requirements: tuple[OdooModelRequirement, ...]
    reference_requirements: tuple[ReferenceRequirement, ...]
    applications: tuple[RunPlanningApplication, ...]


def blocking_run_issue(
    code: str,
    message: str,
    recovery_action: str,
    recipe_ids: Iterable[str],
) -> MigrationRunPlanIssue:
    """Build one consistently shaped planning blocker."""

    return MigrationRunPlanIssue(
        code=code,
        level=MigrationRunPlanIssueLevel.BLOCKER,
        message=message,
        recovery_action=recovery_action,
        recipe_ids=tuple(recipe_ids),
    )


def union_model_requirements(
    applications: Iterable[RunPlanningApplication],
) -> tuple[OdooModelRequirement, ...]:
    """Union required Odoo fields once per model in canonical order."""

    by_model: dict[str, set[str]] = {}
    for application in applications:
        for requirement in application.requirements:
            by_model.setdefault(requirement.model, set()).update(requirement.fields)
    return tuple(
        OdooModelRequirement(model=model, fields=tuple(fields))
        for model, fields in sorted(by_model.items())
    )


def union_reference_requirements(
    applications: Iterable[RunPlanningApplication],
) -> tuple[tuple[ReferenceRequirement, ...], tuple[MigrationRunPlanIssue, ...]]:
    """Union compatible reference inputs and report semantic collisions."""

    by_name: dict[str, ReferenceRequirement] = {}
    owners: dict[str, str] = {}
    issues: list[MigrationRunPlanIssue] = []
    for application in applications:
        for requirement in application.reference_requirements:
            current = by_name.get(requirement.name)
            if current is None:
                by_name[requirement.name] = requirement
                owners[requirement.name] = application.selection.recipe_id
                continue
            if current.content_hash != requirement.content_hash:
                issues.append(
                    blocking_run_issue(
                        "RUN_REFERENCE_REQUIREMENT_COLLISION",
                        (
                            "Two Recipes require different versions of "
                            f"reference data {requirement.name}."
                        ),
                        (
                            "Publish compatible Recipe revisions or use one "
                            "shared reviewed reference version."
                        ),
                        (
                            owners[requirement.name],
                            application.selection.recipe_id,
                        ),
                    )
                )
    return tuple(sorted(by_name.values())), tuple(issues)


def collect_write_collision_issues(
    applications: Iterable[RunPlanningApplication],
) -> tuple[MigrationRunPlanIssue, ...]:
    """Reject two Recipes claiming ownership of the same Odoo field."""

    owners: dict[tuple[str, str], str] = {}
    issues: list[MigrationRunPlanIssue] = []
    for application in applications:
        for claim in application.write_claims:
            previous = owners.setdefault(claim, application.selection.recipe_id)
            if previous != application.selection.recipe_id:
                issues.append(
                    blocking_run_issue(
                        "RUN_RECIPE_WRITE_COLLISION",
                        f"Two Recipes may both write {claim[0]}.{claim[1]}.",
                        (
                            "Choose one owning Recipe for this Odoo field or "
                            "publish non-overlapping Recipe meaning. Reordering "
                            "does not resolve the collision."
                        ),
                        (previous, application.selection.recipe_id),
                    )
                )
    return tuple(issues)


def order_recipe_applications(
    selected_recipe_ids: Iterable[str],
    dependencies: Iterable[RecipeDependency],
) -> tuple[tuple[str, ...], tuple[MigrationRunPlanIssue, ...]]:
    """Return a canonical topological order and all dependency blockers."""

    selected_ids = frozenset(selected_recipe_ids)
    following = {recipe_id: set() for recipe_id in selected_ids}
    indegree = {recipe_id: 0 for recipe_id in selected_ids}
    seen: set[tuple[str, str]] = set()
    issues: list[MigrationRunPlanIssue] = []
    for edge in dependencies:
        key = (edge.before_recipe_id, edge.after_recipe_id)
        if key in seen:
            issues.append(
                blocking_run_issue(
                    "RUN_DEPENDENCY_DUPLICATED",
                    "The same Recipe dependency was selected more than once.",
                    "Keep one copy of each dependency.",
                    key,
                )
            )
            continue
        seen.add(key)
        if not set(key).issubset(selected_ids):
            issues.append(
                blocking_run_issue(
                    "RUN_DEPENDENCY_RECIPE_MISSING",
                    "A dependency refers to a Recipe outside this Test run.",
                    "Select both Recipes or remove that dependency.",
                    key,
                )
            )
            continue
        following[edge.before_recipe_id].add(edge.after_recipe_id)
        indegree[edge.after_recipe_id] += 1
    ready = sorted(recipe_id for recipe_id, count in indegree.items() if count == 0)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for after in sorted(following[current]):
            indegree[after] -= 1
            if indegree[after] == 0:
                ready.append(after)
                ready.sort()
    if len(order) != len(selected_ids):
        cycle_ids = tuple(sorted(key for key, value in indegree.items() if value))
        issues.append(
            blocking_run_issue(
                "RUN_RECIPE_DEPENDENCY_CYCLE",
                "The selected Recipes form a dependency cycle.",
                "Remove one dependency so the applications have a clear order.",
                cycle_ids,
            )
        )
        return tuple(sorted(selected_ids)), tuple(issues)
    return tuple(order), tuple(issues)


def run_requirement_hash(review: RunRequirementReview) -> str:
    """Hash the reusable requirement meaning stored by a CutoverPlan."""

    return content_hash(
        {
            "application_order": list(review.application_order),
            "contract_version": 1,
            "dependencies": [item.to_dict() for item in review.dependencies],
            "model_requirements": [
                item.to_dict() for item in review.model_requirements
            ],
            "reference_requirements": [
                item.to_dict() for item in review.reference_requirements
            ],
            "selected_revisions": [
                item.selection.to_dict() for item in review.applications
            ],
        }
    )
