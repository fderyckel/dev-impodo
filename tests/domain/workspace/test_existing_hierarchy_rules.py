"""Retain anchored hierarchy meaning through source and Recipe contracts."""

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from impodo.application.recipe_application_compilation import RecipeApplicationCompiler
from impodo.domain.hierarchy import HierarchyParentPolicy
from impodo.domain.recipe.models import RecipeIntegrityError
from impodo.domain.recipe_envelope import (
    CURRENT_RECIPE_CONTRACT_VERSIONS,
    SEMANTIC_FIELDS,
    validate_recipe_envelope,
)
from impodo.domain.serialization import content_hash
from impodo.domain.source_binding import source_binding_from_dict
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    HierarchicalLookupRule,
    HierarchyValuePolicy,
    evaluate_hierarchy_path,
    hierarchy_path_key,
    mapping_source_selection,
    preview_derived_entities,
)
from tests.application.workspace.test_derived_entities import _source_evidence


class ExistingHierarchyRuleTests(unittest.TestCase):
    def test_company_specific_nodes_have_distinct_ids_and_shared_ancestors_stay_shared(
        self,
    ):
        selection, catalog, rule = self.rule()
        previews = []
        for company in ("Example A", "Example B"):
            scoped = replace(
                rule,
                existing_parent=HierarchyParentPolicy(
                    "container_id",
                    ("Physical", "Stock"),
                    company_field="company_id",
                    company_name=company,
                    fixed_companies=(None, company),
                ),
            )
            previews.append(preview_derived_entities(scoped, selection, (catalog,)))
        first = {
            candidate.path_depth: candidate for candidate in previews[0].candidates
        }
        second = {
            candidate.path_depth: candidate for candidate in previews[1].candidates
        }
        self.assertEqual(first[1].entity_id, second[1].entity_id)
        self.assertNotEqual(first[2].entity_id, second[2].entity_id)
        self.assertNotEqual(first[3].odoo_external_id, second[3].odoo_external_id)
        self.assertIsNone(first[1].company_name)
        self.assertEqual(first[3].company_name, "Example A")

    def test_recipe_contract_requires_explicit_existing_parent_support(self):
        _selection, _catalog, rule = self.rule()

        def envelope(version, policy):
            recipe = {key: {} for key in SEMANTIC_FIELDS}
            recipe["contract_versions"] = {
                **CURRENT_RECIPE_CONTRACT_VERSIONS,
                "source_preparation_recipe": version,
            }
            recipe["source_preparation"] = {
                "rules": [{"existing_parent": policy}] if policy is not None else []
            }
            payload = {
                "recipe_contract_version": 2,
                "semantic_hash": content_hash(recipe),
                "recipe": recipe,
                "compatibility_hints": {},
                "provenance": {},
            }
            payload["payload_hash"] = content_hash(payload)
            return json.dumps(payload).encode()

        validate_recipe_envelope(envelope(1, None))
        validate_recipe_envelope(envelope(2, rule.existing_parent.to_dict()))
        with self.assertRaisesRegex(RecipeIntegrityError, "contract v2"):
            validate_recipe_envelope(envelope(1, rule.existing_parent.to_dict()))

    def rule(self):
        selection, catalog = _source_evidence(rows=(("P1", "A / B"), ("P2", "A")))
        dataset = selection.datasets[0]
        rule = HierarchicalLookupRule(
            str(uuid4()),
            "places",
            dataset.dataset_id,
            (dataset.columns[1].stable_key,),
            "x.place",
            "name",
            "fictional",
            HierarchyValuePolicy("block"),
            existing_parent=HierarchyParentPolicy(
                "container_id", ("Physical", "Stock")
            ),
        )
        return selection, catalog, rule

    def test_one_source_level_is_allowed_beneath_an_existing_second_level(self):
        selection, catalog, rule = self.rule()
        preview = preview_derived_entities(rule, selection, (catalog,))
        self.assertEqual(
            {candidate.path_depth for candidate in preview.candidates}, {1, 2, 3}
        )
        self.assertEqual(len(preview.candidates), 4)
        self.assertIn(
            '["physical","stock","a / b"]',
            {candidate.canonical_key for candidate in preview.candidates},
        )
        self.assertNotEqual(
            hierarchy_path_key(rule, ("a / b",)), hierarchy_path_key(rule, ("a", "b"))
        )

    def test_policy_survives_persisted_plan_effective_selection_and_recipe_rebinding(
        self,
    ):
        selection, catalog, rule = self.rule()
        plan = DerivedEntityPlan(
            str(uuid4()),
            1,
            str(uuid4()),
            selection.content_hash,
            (rule,),
            datetime.now(UTC),
            "Fictional operator",
        )
        restored = DerivedEntityPlan.from_json(plan.to_json())
        self.assertEqual(restored.content_hash, plan.content_hash)
        effective = mapping_source_selection(selection, restored, (catalog,))
        derived = next(
            dataset for dataset in effective.datasets if dataset.name == "places"
        )
        self.assertEqual(
            source_binding_from_dict(derived.source.to_dict()), derived.source
        )
        raw = json.loads(plan.to_json())["rules"][0]
        compiler = RecipeApplicationCompiler()
        rebound = compiler._preparation_rule(
            raw,
            {
                rule.source_dataset_id: "new-source",
                rule.source_level_column_keys[0]: "new-column",
            },
        )
        self.assertEqual(rebound.existing_parent, rule.existing_parent)
        self.assertEqual(rebound.source_level_column_keys, ("new-column",))
        self.assertEqual(rebound.source_dataset_id, "new-source")

    def test_all_blank_fixed_value_preserves_the_existing_anchor(self):
        _selection, _catalog, rule = self.rule()
        rule = replace(rule, all_blank=HierarchyValuePolicy("fixed", "Unassigned"))
        evaluation = evaluate_hierarchy_path(rule, {})
        self.assertEqual(evaluation.display_parts, ("Physical", "Stock", "Unassigned"))

    def test_older_plan_contract_cannot_silently_drop_existing_parent_semantics(self):
        selection, _catalog, rule = self.rule()
        with self.assertRaisesRegex(ValueError, "contract v9"):
            DerivedEntityPlan(
                str(uuid4()),
                1,
                str(uuid4()),
                selection.content_hash,
                (rule,),
                datetime.now(UTC),
                "Fictional operator",
                contract_version=8,
            )

    def test_promoting_missing_levels_cannot_change_a_required_existing_path(self):
        _selection, _catalog, rule = self.rule()
        with self.assertRaisesRegex(ValueError, "cannot be promoted"):
            replace(rule, missing_parent=HierarchyValuePolicy("promote"))
