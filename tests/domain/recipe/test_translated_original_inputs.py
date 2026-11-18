"""Rebind reviewed scoped choices to one fresh physical Recipe input."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from unittest import TestCase
from uuid import uuid4

from impodo.application.recipe_application_compilation import RecipeApplicationCompiler
from impodo.application.recipe_compilation_service import RecipeCompiler
from impodo.domain.mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    ReferenceKeyMapping,
    RelationshipMapping,
    RelationshipResolver,
    ResolverOrigin,
    ValueMapping,
)
from impodo.domain.preflight.relationship_inputs import relationship_original_inputs
from impodo.domain.workspace.contracts import SourceSelection
from tests.domain.recipe.test_representative_shapes import _column, _dataset


class TranslatedOriginalRecipeTests(TestCase):
    def test_scoped_choice_rebinds_every_component_to_one_reordered_file_column(self):
        source = _dataset(
            "Documents",
            (("reference", "string"), ("choice", "string"), ("notes", "string")),
            "a",
        )
        choice_key = _column(source, "choice")
        relationship = RelationshipMapping(
            "lookup_id",
            "many2one",
            (choice_key,),
            RelationshipResolver(
                ResolverOrigin.TARGET_CATALOG,
                model="x.lookup",
                key_mappings=(ReferenceKeyMapping(choice_key, "code"),),
                scope_mappings=(
                    ReferenceKeyMapping(choice_key, "company_id"),
                    ReferenceKeyMapping(choice_key, "region"),
                ),
                value_mappings=(
                    ValueMapping("000745", '["REVIEWED","Company A","North"]'),
                ),
            ),
            blank_source_values=("Skip",),
        )
        mapping = DatasetMapping(
            source.dataset_id,
            "x.document",
            source_identity_column_keys=(_column(source, "reference"),),
            target_identity=(
                IdentityComponentMapping((_column(source, "reference"),), ("ref",)),
            ),
            relationships=(relationship,),
        )
        compiler = object.__new__(RecipeCompiler)
        _, dataset_ids, column_ids = compiler._source_identity((source,))
        portable = compiler._mapping((mapping,), dataset_ids, column_ids, {})
        original_portable = deepcopy(portable)
        fresh = replace(
            source,
            dataset_id="fresh-documents",
            columns=tuple(
                replace(column, ordinal=index, stable_key=f"fresh:{column.source_name}")
                for index, column in enumerate(reversed(source.columns), start=1)
            ),
        )
        selection = SourceSelection(
            str(uuid4()),
            1,
            str(uuid4()),
            datetime.now(UTC),
            "Manager",
            (fresh,),
            "sha256:" + "b" * 64,
        )
        _, fresh_datasets, fresh_columns = compiler._source_identity((fresh,))
        bindings = {logical: physical for physical, logical in fresh_datasets.items()}
        bindings.update(
            {logical: physical[1] for physical, logical in fresh_columns.items()}
        )
        (rebound,) = RecipeApplicationCompiler()._mapping_datasets(
            {"mapping": portable, "control_definitions": {}},
            bindings,
            selection,
            None,
            None,
        )
        (actual,) = rebound.relationships
        proof = relationship_original_inputs(actual)
        self.assertIsNotNone(proof)
        self.assertEqual(proof.saved_keys, ("fresh:choice",))
        self.assertEqual(proof.input_keys, ("fresh:choice",))
        self.assertEqual(actual.source_column_keys, ("fresh:choice",))
        self.assertEqual(
            tuple(
                item.source_column_key
                for item in (
                    *actual.resolver.key_mappings,
                    *actual.resolver.scope_mappings,
                )
            ),
            ("fresh:choice",) * 3,
        )
        self.assertEqual(
            actual.resolver.value_mappings, relationship.resolver.value_mappings
        )
        self.assertEqual(actual.blank_source_values, ("Skip",))
        self.assertEqual(portable, original_portable)
