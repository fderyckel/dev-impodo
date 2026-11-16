"""Plan exact read domains for separate consumers of incoming hierarchies."""

from dataclasses import replace
from unittest import TestCase

from impodo.domain.compiler import compile_profile_document
from impodo.domain.execution.planner import plan_preflight_requirements
from impodo.domain.recipe.profile import ProfileDocument
from impodo.domain.shared.models import LogicalReference, PreparedRecord


def hierarchy_inputs(model, parent_field, child_model, child_parent_field):
    def dataset(name, target, parent, related, nullable):
        return {
            "name": name,
            "source": {"file": f"{name}.csv"},
            "target": {"model": target},
            "source_identity": {"fields": ["key"]},
            "target_identity": {
                "components": [{"source_fields": ["name"], "target_fields": ["name"]}],
                "scope": [
                    {
                        "source_fields": ["parent"],
                        "target_fields": [parent],
                        "resolve": {
                            "dataset": related,
                            "target_source_fields": ["key"],
                        },
                        "null_policy": "explicit_scope_null" if nullable else "reject",
                    }
                ],
            },
            "fields": {},
            "relations": {},
        }

    plan = compile_profile_document(
        ProfileDocument.model_validate(
            {
                "profile": {"id": "separate_hierarchy_consumers"},
                "datasets": [
                    dataset("parents", model, parent_field, "parents", True),
                    dataset(
                        "children", child_model, child_parent_field, "parents", False
                    ),
                ],
            }
        )
    )

    def record(dataset, target, key, name, parent, row):
        return PreparedRecord(
            dataset=dataset,
            source_row=row,
            target_model=target,
            source_identity=(key,),
            target_identity=(name,),
            target_scope=(
                LogicalReference("incoming", (parent,), dataset="parents")
                if parent
                else None,
            ),
            scalar_values={},
            references={},
        )

    rows = (
        record("parents", model, "root", "Root", None, 1),
        record("parents", model, "left", "Left", "root", 2),
        record("parents", model, "right", "Right", "root", 3),
        record("children", child_model, "first", "Repeated", "left", 1),
        record("children", child_model, "second", "Repeated", "right", 2),
    )
    return plan, rows


class HierarchyRequirementTests(TestCase):
    def test_separate_consumers_keep_exact_ancestry_for_unrelated_models(self):
        for model, parent, child_model, child_parent in (
            ("product.category", "parent_id", "product.category", "parent_id"),
            ("account.group", "parent_id", "account.account", "group_id"),
            ("x.tree.node", "ancestor_id", "x.tree.node", "ancestor_id"),
        ):
            with self.subTest(model=model):
                plan, rows = hierarchy_inputs(model, parent, child_model, child_parent)
                requirements = plan_preflight_requirements(
                    plan, rows, maximum_keys_per_request=1
                )
                self.assertTrue(
                    all(request.domain for request in requirements.record_requests)
                )
                child_reads = [
                    request
                    for request in requirements.record_requests
                    if ["name", "=", "Repeated"] in request.domain
                ]
                self.assertEqual(len(child_reads), 2)
                for name in ("Left", "Right"):
                    self.assertTrue(
                        any(
                            [f"{child_parent}.name", "=", name] in request.domain
                            and [f"{child_parent}.{parent}.name", "=", "Root"]
                            in request.domain
                            and [f"{child_parent}.{parent}.{parent}", "=", False]
                            in request.domain
                            for request in child_reads
                        )
                    )
                reversed_plan = plan_preflight_requirements(
                    plan, reversed(rows), maximum_keys_per_request=1
                )
                self.assertEqual(
                    requirements.semantic_hash, reversed_plan.semantic_hash
                )

    def test_missing_ambiguous_and_cyclic_parent_paths_never_widen_reads(self):
        plan, rows = hierarchy_inputs(
            "x.tree.node", "ancestor_id", "x.tree.node", "ancestor_id"
        )
        for changed in (
            rows[:1] + rows[2:],
            rows + (rows[1],),
            (
                replace(
                    rows[0],
                    target_scope=(
                        LogicalReference("incoming", ("left",), dataset="parents"),
                    ),
                ),
                *rows[1:],
            ),
        ):
            with self.subTest(rows=changed):
                requirements = plan_preflight_requirements(plan, changed)
                self.assertTrue(
                    all(request.domain for request in requirements.record_requests)
                )
                self.assertFalse(
                    any(
                        ["ancestor_id.name", "=", "Left"] in request.domain
                        and ["name", "=", "Repeated"] in request.domain
                        for request in requirements.record_requests
                    )
                )
