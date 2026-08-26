"""Verify pure integrated-run requirement and ordering decisions."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from impodo.domain.run.planning import (
    collect_write_collision_issues,
    order_recipe_applications,
    run_requirement_hash,
    union_model_requirements,
    union_reference_requirements,
)
from impodo.domain.run.contracts import (
    OdooModelRequirement,
    RecipeDependency,
    RecipeRevisionSelection,
    ReferenceRequirement,
)


RECIPE_A = "00000000-0000-0000-0000-000000000001"
RECIPE_B = "00000000-0000-0000-0000-000000000002"
RECIPE_C = "00000000-0000-0000-0000-000000000003"
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def _application(
    recipe_id: str,
    *,
    requirements: tuple[OdooModelRequirement, ...] = (),
    references: tuple[ReferenceRequirement, ...] = (),
    write_claims: tuple[tuple[str, str], ...] = (),
):
    return SimpleNamespace(
        selection=RecipeRevisionSelection(
            recipe_id=recipe_id,
            recipe_revision=1,
            semantic_hash=HASH_A,
        ),
        requirements=requirements,
        reference_requirements=references,
        write_claims=write_claims,
    )


class RunPlanningDomainTests(unittest.TestCase):
    def test_unions_models_and_reports_incompatible_reference_versions(self) -> None:
        applications = (
            _application(
                RECIPE_A,
                requirements=(
                    OdooModelRequirement(
                        model="res.partner",
                        fields=("name",),
                    ),
                ),
                references=(
                    ReferenceRequirement(name="countries", content_hash=HASH_A),
                ),
            ),
            _application(
                RECIPE_B,
                requirements=(
                    OdooModelRequirement(
                        model="res.partner",
                        fields=("email", "name"),
                    ),
                ),
                references=(
                    ReferenceRequirement(name="countries", content_hash=HASH_B),
                ),
            ),
        )

        self.assertEqual(
            union_model_requirements(applications),
            (
                OdooModelRequirement(
                    model="res.partner",
                    fields=("email", "name"),
                ),
            ),
        )
        references, issues = union_reference_requirements(applications)
        self.assertEqual(
            references,
            (ReferenceRequirement(name="countries", content_hash=HASH_A),),
        )
        self.assertEqual(
            [item.code for item in issues],
            ["RUN_REFERENCE_REQUIREMENT_COLLISION"],
        )

    def test_dependency_order_is_canonical_and_reports_cycles(self) -> None:
        order, issues = order_recipe_applications(
            (RECIPE_C, RECIPE_A, RECIPE_B),
            (
                RecipeDependency(RECIPE_A, RECIPE_B),
                RecipeDependency(RECIPE_B, RECIPE_C),
            ),
        )
        self.assertEqual(order, (RECIPE_A, RECIPE_B, RECIPE_C))
        self.assertEqual(issues, ())

        fallback, cycle_issues = order_recipe_applications(
            (RECIPE_A, RECIPE_B),
            (
                RecipeDependency(RECIPE_A, RECIPE_B),
                RecipeDependency(RECIPE_B, RECIPE_A),
            ),
        )
        self.assertEqual(fallback, (RECIPE_A, RECIPE_B))
        self.assertEqual(
            [item.code for item in cycle_issues],
            ["RUN_RECIPE_DEPENDENCY_CYCLE"],
        )

    def test_write_collision_and_requirement_hash_are_semantic(self) -> None:
        applications = (
            _application(RECIPE_A, write_claims=(("res.partner", "name"),)),
            _application(RECIPE_B, write_claims=(("res.partner", "name"),)),
        )
        self.assertEqual(
            [item.code for item in collect_write_collision_issues(applications)],
            ["RUN_RECIPE_WRITE_COLLISION"],
        )

        review = SimpleNamespace(
            application_order=(RECIPE_A, RECIPE_B),
            dependencies=(RecipeDependency(RECIPE_A, RECIPE_B),),
            model_requirements=(
                OdooModelRequirement(model="res.partner", fields=("name",)),
            ),
            reference_requirements=(),
            applications=applications,
        )
        reversed_review = SimpleNamespace(
            application_order=(RECIPE_B, RECIPE_A),
            dependencies=review.dependencies,
            model_requirements=review.model_requirements,
            reference_requirements=review.reference_requirements,
            applications=review.applications,
        )
        self.assertEqual(run_requirement_hash(review), run_requirement_hash(review))
        self.assertNotEqual(
            run_requirement_hash(review),
            run_requirement_hash(reversed_review),
        )


if __name__ == "__main__":
    unittest.main()
