"""Qualify explicit blank relationships and historic mapping hash boundaries."""

import unittest
from dataclasses import replace

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    MappingDefinition,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)


def relationship(**changes):
    return replace(
        RelationshipMapping(
            target_field="x_list_id",
            kind="many2one",
            source_column_keys=("choice",),
            resolver=RelationshipResolver(
                origin=ResolverOrigin.TARGET_CATALOG,
                model="x_list",
                key_mappings=(ReferenceKeyMapping("choice", "name"),),
            ),
            categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH,
            blank_source_values=("TO_DEFINE",),
        ),
        **changes,
    )


class RelationshipBlankValuesTests(unittest.TestCase):
    def test_old_recipe_blank_action_does_not_override_current_requiredness(self):
        from impodo.application.recipe_application_compilation import RecipeApplicationCompiler

        legacy = {"target_field": "x_link_id", "kind": "many2one", "source_column_ids": ["old-choice"],
            "target_model": "x.label", "blank_source_action": "leave_blank"}
        for required in (False, True):
            rebound = RecipeApplicationCompiler()._relationship({**legacy, "required": required}, {"old-choice": "current-choice"})
            self.assertEqual(rebound.required, required)
            self.assertEqual(rebound.source_column_keys, ("current-choice",))
            self.assertEqual(rebound.blank_source_action, "source_rules")

    def test_blank_source_action_is_versioned_and_does_not_change_old_hashes(self):
        relation = relationship(blank_source_values=(), blank_source_action="leave_blank")
        definition = MappingDefinition("mapping", "source", "schema", datasets=(DatasetMapping("records", "x_record", relationships=(relation,)),), contract_version=19)
        self.assertEqual(MappingDefinition.from_dict(definition.to_dict()), definition)
        with self.assertRaisesRegex(ValueError, "v19"):
            replace(definition, contract_version=18)
        old = replace(definition, datasets=(replace(definition.datasets[0], relationships=(replace(relation, blank_source_action="source_rules"),)),), contract_version=18)
        self.assertNotIn("blank_source_action", str(old.to_dict()))
        self.assertEqual(MappingDefinition.from_dict(old.to_dict()).content_hash, old.content_hash)
        self.assertNotEqual(old.content_hash, definition.content_hash)
        current = replace(definition, contract_version=20)
        self.assertNotIn("blank_source_action", str(current.to_dict()))
        self.assertEqual(current.datasets[0].relationships[0].blank_source_action, "source_rules")
        self.assertEqual(MappingDefinition.from_dict(current.to_dict()), current)
        required = replace(relation, required=True)
        upgraded = replace(current, datasets=(replace(current.datasets[0], relationships=(required,)),))
        self.assertTrue(upgraded.datasets[0].relationships[0].required)

    def test_blank_source_action_rejects_required_list_constant_or_partial_keys(self):
        for changes in ({"required": True}, {"required_on_create": True}, {"kind": "many2many"}, {"source_column_keys": ("choice", "scope")}, {"null_policy": "ignore_source_null"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "optional single-column many2one"):
                relation = relationship(blank_source_values=(), blank_source_action="leave_blank", **changes)
                MappingDefinition("mapping", "source", "schema", datasets=(DatasetMapping("records", "x_record", relationships=(relation,)),), contract_version=19)

    def test_captured_required_field_rejects_a_blank_despite_optional_authored_flags(
        self,
    ):
        from impodo.domain.mapping.validation.validator import MappingSemanticValidator
        from tests.domain.mapping import test_validation as fixtures

        selection = fixtures._source_selection()
        schema = fixtures._schema_catalog()
        schema = replace(
            schema,
            models=tuple(
                replace(
                    model,
                    fields=tuple(
                        replace(field, required=True)
                        if model.name == "res.partner" and field.name == "category_id"
                        else field
                        for field in model.fields
                    ),
                )
                for model in schema.models
            ),
        )
        governance = fixtures._schema_governance(schema)
        definition = fixtures._valid_definition(selection, governance)
        partner = definition.datasets[1]
        for changes in ({"blank_source_values": ("TO_DEFINE",)},):
            relation = replace(partner.relationships[0], categorical_policy=CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH, **changes)
            modified = replace(definition, datasets=(definition.datasets[0], replace(partner, relationships=(relation,))))
            result = MappingSemanticValidator().validate(modified, selection, schema, governance)
            self.assertIn("MAPPING_RELATION_BLANK_NOT_ALLOWED", {item.code for item in result.issues})

    def test_blank_decision_roundtrips_and_changes_semantic_hash(self):
        mapping = MappingDefinition(
            mapping_id="mapping",
            source_selection_hash="source",
            schema_hash="schema",
            datasets=(
                DatasetMapping("records", "x_record", relationships=(relationship(),)),
            ),
        )
        self.assertEqual(MappingDefinition.from_dict(mapping.to_dict()), mapping)
        empty = replace(
            mapping,
            datasets=(
                replace(
                    mapping.datasets[0],
                    relationships=(relationship(blank_source_values=()),),
                ),
            ),
        )
        self.assertNotEqual(mapping.content_hash, empty.content_hash)
        with self.assertRaisesRegex(ValueError, "v18"):
            replace(mapping, contract_version=17)
        legacy = replace(empty, contract_version=17)
        self.assertNotIn("blank_source_values", str(legacy.to_dict()))
        self.assertEqual(MappingDefinition.from_dict(legacy.to_dict()), legacy)

    def test_required_or_list_relationships_cannot_leave_blank(self):
        for changes in (
            {"required": True},
            {"required_on_create": True},
            {"kind": "many2many"},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "optional many2one"),
            ):
                relationship(**changes)

    def test_choice_cannot_both_match_and_leave_blank(self):
        resolver = replace(
            relationship().resolver,
            value_mappings=(ValueMapping("TO_DEFINE", "Standard"),),
        )
        with self.assertRaisesRegex(ValueError, "cannot match a record"):
            relationship(resolver=resolver)

    def test_intentional_blank_cannot_silently_ignore_an_existing_value(self):
        with self.assertRaisesRegex(ValueError, "Ignore blank source values"):
            relationship(null_policy="ignore_source_null")

    def test_malformed_blank_lists_are_rejected_instead_of_reinterpreted(self):
        for value in ("TO_DEFINE", {"TO_DEFINE": True}, (2,)):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "list of text"):
                relationship(blank_source_values=value)

    def test_blank_decisions_are_normalized_without_treating_other_values_as_blank(
        self,
    ):
        self.assertEqual(
            relationship(
                blank_source_values=("TO_DEFINE", "UNKNOWN", "TO_DEFINE")
            ).blank_source_values,
            ("TO_DEFINE", "UNKNOWN"),
        )
        for value in ("", "   ", " TO_DEFINE "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                relationship(blank_source_values=(value,))
