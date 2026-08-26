from __future__ import annotations

import unittest
from types import SimpleNamespace

from impodo.access import Actor, ActorIdentity, Capability
from impodo.application.run.odoo_requirements import (
    TestRunOdooRequirementsUseCase,
)
from impodo.domain.recipe.models import RecipeError

ACTOR = Actor(
    identity=ActorIdentity(
        issuer="local",
        subject_id="operator",
        display_name="Local operator",
    ),
    capabilities=frozenset({Capability.PROJECT_VIEW}),
)


class _SelectionReader:
    def __init__(self, binding) -> None:
        self.binding = binding
        self.calls: list[str] = []

    def for_workspace(self, workspace_id: str):
        self.calls.append(workspace_id)
        return self.binding


class _RevisionReader:
    def __init__(self, revisions) -> None:
        self.revisions = revisions
        self.calls: list[tuple[str, tuple[tuple[str, int], ...], Actor]] = []

    def read_revisions(self, project_id, revisions, *, actor):
        self.calls.append((project_id, revisions, actor))
        return self.revisions


class _Authorization:
    def __init__(self) -> None:
        self.calls = []

    def require(self, actor, capability, *, project_id=None) -> None:
        self.calls.append((actor, capability, project_id))


def _selection(recipe_id: str, semantic_hash: str):
    return SimpleNamespace(
        recipe_id=recipe_id,
        recipe_revision=1,
        semantic_hash=semantic_hash,
    )


def _revision(display_name: str, semantic_hash: str, models):
    return SimpleNamespace(
        recipe=SimpleNamespace(display_name=display_name),
        envelope={
            "semantic_hash": semantic_hash,
            "recipe": {
                "odoo_target_contract": {
                    "models": models,
                }
            },
        },
    )


class TestRunOdooRequirementsUseCaseTests(unittest.TestCase):
    def test_bulk_reads_selected_revisions_once_and_merges_in_memory(self) -> None:
        selections = (
            _selection("recipe-product", "sha256:product"),
            _selection("recipe-customer", "sha256:customer"),
        )
        binding = SimpleNamespace(
            project_id="project-1",
            selected_revisions=selections,
        )
        revisions = {
            ("recipe-product", 1): _revision(
                "Products",
                "sha256:product",
                (
                    {
                        "model": "res.partner",
                        "fields": ({"name": "email"},),
                    },
                    {
                        "model": "res.country",
                        "fields": ({"name": "code"}, {"name": "name"}),
                        "reference_paths": (
                            {
                                "key_fields": ("code",),
                                "scope_fields": ("company_id",),
                                "parent_model": "res.partner",
                                "relationship_field": "country_id",
                                "relationship_type": "many2one",
                            },
                        ),
                    },
                ),
            ),
            ("recipe-customer", 1): _revision(
                "Customers",
                "sha256:customer",
                (
                    {
                        "model": "res.partner",
                        "fields": ({"name": "name"}, {"name": "email"}),
                    },
                ),
            ),
        }
        selections_reader = _SelectionReader(binding)
        revisions_reader = _RevisionReader(revisions)
        authorization = _Authorization()
        use_case = TestRunOdooRequirementsUseCase(
            test_runs=selections_reader,
            recipes=revisions_reader,
            authorization=authorization,
        )

        plan = use_case.for_workspace("workspace-1", actor=ACTOR)

        self.assertIsNotNone(plan)
        self.assertEqual(selections_reader.calls, ["workspace-1"])
        self.assertEqual(
            revisions_reader.calls,
            [
                (
                    "project-1",
                    (("recipe-product", 1), ("recipe-customer", 1)),
                    ACTOR,
                )
            ],
        )
        self.assertEqual(
            authorization.calls,
            [(ACTOR, Capability.PROJECT_VIEW, "project-1")],
        )
        self.assertEqual(
            tuple(
                (item.model_name, item.field_names, item.recipe_names)
                for item in plan.models
            ),
            (
                ("res.country", ("code", "name"), ("Products",)),
                (
                    "res.partner",
                    ("email", "name"),
                    ("Customers", "Products"),
                ),
            ),
        )
        self.assertEqual(len(plan.supporting_values), 1)
        supporting = plan.supporting_values[0]
        self.assertEqual(supporting.model_name, "res.country")
        self.assertEqual(supporting.key_fields, ("code",))
        self.assertEqual(supporting.scope_fields, ("company_id",))
        self.assertEqual(supporting.recipe_names, ("Products",))
        self.assertEqual(
            tuple(
                (
                    item.parent_model,
                    item.relationship_field,
                    item.relationship_type,
                )
                for item in supporting.relationships
            ),
            (("res.partner", "country_id", "many2one"),),
        )

    def test_missing_setup_does_not_authorize_or_read_recipes(self) -> None:
        selections_reader = _SelectionReader(None)
        revisions_reader = _RevisionReader({})
        authorization = _Authorization()
        use_case = TestRunOdooRequirementsUseCase(
            test_runs=selections_reader,
            recipes=revisions_reader,
            authorization=authorization,
        )

        self.assertIsNone(use_case.for_workspace("workspace-1", actor=ACTOR))
        self.assertEqual(revisions_reader.calls, [])
        self.assertEqual(authorization.calls, [])

    def test_changed_selected_recipe_fails_closed(self) -> None:
        selection = _selection("recipe-customer", "sha256:selected")
        binding = SimpleNamespace(
            project_id="project-1",
            selected_revisions=(selection,),
        )
        use_case = TestRunOdooRequirementsUseCase(
            test_runs=_SelectionReader(binding),
            recipes=_RevisionReader(
                {
                    ("recipe-customer", 1): _revision(
                        "Customers",
                        "sha256:changed",
                        (),
                    )
                }
            ),
            authorization=_Authorization(),
        )

        with self.assertRaisesRegex(
            RecipeError,
            "selected Recipe version has changed",
        ):
            use_case.for_workspace("workspace-1", actor=ACTOR)


if __name__ == "__main__":
    unittest.main()
