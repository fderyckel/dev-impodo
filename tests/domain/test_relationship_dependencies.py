from __future__ import annotations

from itertools import permutations
import unittest

from pydantic import ValidationError

from impodo.domain.compiler import compile_profile_document
from impodo.domain.execution.planner import plan_preflight_requirements
from impodo.domain.recipe.profile import (
    DatasetSpec,
    IdentityComponent,
    ProfileDocument,
    ProfileIdentity,
    RelationSpec,
    ResolveSpec,
    SourceIdentitySpec,
    SourceSpec,
    TargetIdentitySpec,
    TargetSpec,
)
from impodo.domain.relationship_dependencies import (
    DependencySource,
    DependencyStrength,
    dependency_sets_by_owner,
    extract_dataset_dependency_edges,
    required_cross_dataset_cycle,
)


def _dataset(
    name: str,
    *,
    scope: tuple[IdentityComponent, ...] = (),
    relations: dict[str, RelationSpec] | None = None,
) -> DatasetSpec:
    return DatasetSpec(
        name=name,
        source=SourceSpec(file=f"{name}.csv"),
        target=TargetSpec(model=f"x.{name}"),
        source_identity=SourceIdentitySpec(fields=("code",)),
        target_identity=TargetIdentitySpec(
            components=(
                IdentityComponent(
                    source_fields=("code",),
                    target_fields=("x_code",),
                ),
            ),
            scope=scope,
        ),
        relations=relations or {},
    )


def _incoming_relation(
    dataset: str,
    *,
    required_on_create: bool = False,
) -> RelationSpec:
    return RelationSpec(
        kind="many2one",
        source_fields=(f"{dataset}_code",),
        resolve=ResolveSpec(
            dataset=dataset,
            target_source_fields=("code",),
        ),
        required_on_create=required_on_create,
    )


class RelationshipDependencyTests(unittest.TestCase):
    def test_extractor_records_hard_deferrable_and_self_edges(self) -> None:
        support = _dataset("support")
        owner = _dataset(
            "owner",
            scope=(
                IdentityComponent(
                    source_fields=("support_code",),
                    target_fields=("support_id",),
                    resolve=ResolveSpec(
                        dataset="support",
                        target_source_fields=("code",),
                    ),
                ),
            ),
            relations={
                "optional_support_id": _incoming_relation("support"),
                "parent_id": _incoming_relation(
                    "owner",
                    required_on_create=True,
                ),
            },
        )

        edges = extract_dataset_dependency_edges((owner, support))

        self.assertEqual(len(edges), 3)
        self.assertEqual(
            {
                (edge.target_field, edge.source, edge.strength, edge.is_self_reference)
                for edge in edges
            },
            {
                (
                    "support_id",
                    DependencySource.TARGET_SCOPE,
                    DependencyStrength.HARD,
                    False,
                ),
                (
                    "optional_support_id",
                    DependencySource.RELATIONSHIP,
                    DependencyStrength.DEFERRABLE,
                    False,
                ),
                (
                    "parent_id",
                    DependencySource.RELATIONSHIP,
                    DependencyStrength.HARD,
                    True,
                ),
            },
        )
        self.assertEqual(
            dependency_sets_by_owner(edges),
            {"owner": ("owner", "support")},
        )
        self.assertIsNone(
            required_cross_dataset_cycle(edges, {"owner", "support"})
        )
        profile = ProfileDocument(
            profile=ProfileIdentity(id="self_reference"),
            datasets=(owner, support),
        )
        self.assertEqual(profile.datasets, (owner, support))

    def test_required_cross_dataset_cycle_is_deterministic(self) -> None:
        one = _dataset(
            "one",
            relations={"two_id": _incoming_relation("two", required_on_create=True)},
        )
        two = _dataset(
            "two",
            relations={"one_id": _incoming_relation("one", required_on_create=True)},
        )

        expected = ("one", "two", "one")
        for datasets in permutations((one, two)):
            edges = extract_dataset_dependency_edges(datasets)
            self.assertEqual(
                required_cross_dataset_cycle(edges, {"one", "two"}),
                expected,
            )
            with self.assertRaisesRegex(ValidationError, "cycle"):
                ProfileDocument(
                    profile=ProfileIdentity(id="cycle"),
                    datasets=datasets,
                )

    def test_compiler_and_preflight_edges_ignore_dataset_input_order(self) -> None:
        support = _dataset("support")
        owner = _dataset(
            "owner",
            relations={"support_id": _incoming_relation("support")},
        )
        expected = None
        for datasets in permutations((owner, support)):
            profile = ProfileDocument(
                profile=ProfileIdentity(id="dependency_order"),
                datasets=datasets,
            )
            plan = compile_profile_document(profile)
            preflight = plan_preflight_requirements(plan, ())
            current = tuple(edge.portable_dict() for edge in plan.dependency_edges)
            if expected is None:
                expected = current
            self.assertEqual(current, expected)
            self.assertEqual(preflight.dependency_edges, plan.dependency_edges)


if __name__ == "__main__":
    unittest.main()
